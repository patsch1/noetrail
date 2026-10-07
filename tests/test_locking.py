from __future__ import annotations

import argparse
import ast
import concurrent.futures
import functools
import importlib
import inspect
import json
import pkgutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import noetrail
import noetrail.cli
import noetrail.commands.edit
from noetrail.errors import InternalError
from tests import CLI_COMMAND, temporary_root

# Direct filesystem mutations. A handler containing one of these must never run
# under a shared lock, which is the defect class that let `recipe` write without
# the exclusive vault lock.
DIRECT_WRITE_MARKERS = (
    "atomic_write_entry(",
    "atomic_write_blob(",
    "relocate_entry(",
    "os.replace(",
    ".unlink(",
    ".write_text(",
    ".write_bytes(",
)


def package_functions() -> dict[str, object]:
    """Every function `noetrail` defines, by the name callers write.

    The handlers used to sit in the same module as the code they call, so
    "does this handler write" could be answered by searching the handler's own
    source. They now live in `noetrail.commands.*` and call into
    `noetrail.store`, `noetrail.attachments`, and each other, so the same
    question has to be asked of the call graph instead. Collecting the whole
    package here is what makes the marker list follow a write that moves into
    a helper: a new `store.save_entry()` that calls `os.replace` is classified
    as writing, and every handler that calls it inherits that.
    """

    for info in pkgutil.walk_packages(
        noetrail.__path__, prefix="noetrail."
    ):
        importlib.import_module(info.name)
    found: dict[str, object] = {}
    for name, module in list(sys.modules.items()):
        if not name.startswith("noetrail"):
            continue
        for attribute, value in vars(module).items():
            if not inspect.isfunction(value):
                continue
            if not getattr(value, "__module__", "").startswith("noetrail"):
                continue
            found.setdefault(attribute, value)
    return found


def _called_names(source: str) -> set[str]:
    tree = ast.parse(textwrap.dedent(source))
    return {
        ast.unparse(node.func).rsplit(".", 1)[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }


@functools.lru_cache(maxsize=1)
def write_markers() -> tuple[str, ...]:
    """The direct markers plus every package function that reaches one."""

    functions = package_functions()
    sources = {}
    for name, function in functions.items():
        try:
            sources[name] = inspect.getsource(function)
        except OSError:  # pragma: no cover - source is always available here
            continue
    writes = {
        name
        for name, source in sources.items()
        if any(marker in source for marker in DIRECT_WRITE_MARKERS)
    }
    calls = {
        name: _called_names(source) & set(sources) for name, source in sources.items()
    }
    changed = True
    while changed:
        changed = False
        for name, targets in calls.items():
            if name not in writes and targets & writes:
                writes.add(name)
                changed = True
    return DIRECT_WRITE_MARKERS + tuple(sorted(f"{name}(" for name in writes))


# `args.index` is the one vault scan a command gets. It is a snapshot taken
# before the command's write, so reading it again *after* that write hands the
# handler pre-write state -- the defect class the shared index introduced, in
# the same place as the lock rule it sits next to.
def _terminates(block: list[ast.stmt]) -> bool:
    return bool(block) and isinstance(block[-1], (ast.Return, ast.Raise))


def _blocks(statement: ast.stmt) -> list[list[ast.stmt]]:
    blocks: list[list[ast.stmt]] = []
    for name in ("body", "orelse", "finalbody"):
        block = getattr(statement, name, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            blocks.append(block)
    for handler in getattr(statement, "handlers", []):
        blocks.append(handler.body)
    return blocks


def _own_nodes(statement: ast.stmt) -> list[ast.AST]:
    """Nodes belonging to the statement itself, not to a block it opens."""

    nested = {
        id(node)
        for block in _blocks(statement)
        for child in block
        for node in ast.walk(child)
    }
    own = [node for node in ast.walk(statement) if id(node) not in nested]
    return sorted(
        own,
        key=lambda node: (
            getattr(node, "lineno", 0),
            getattr(node, "col_offset", 0),
        ),
    )


def _writes(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = ast.unparse(node.func)
    return any(name.endswith(marker[:-1]) for marker in write_markers())


def _reads_index(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "index"
        and isinstance(node.value, ast.Name)
        and node.value.id == "args"
    )


def stale_index_reads(source: str) -> list[int]:
    """Lines where `args.index` is read on a path that already wrote.

    A block that ends in `return` or `raise` cannot precede the statements
    after it, so a write inside one does not taint them -- that is what makes
    `relate`, which writes and returns in its `--unresolved` branch and then
    resolves a relation target below it, correct rather than a false positive.
    """

    violations: list[int] = []

    def scan(body: list[ast.stmt], wrote: bool) -> bool:
        for statement in body:
            for node in _own_nodes(statement):
                if _writes(node):
                    wrote = True
                elif _reads_index(node) and wrote:
                    violations.append(getattr(node, "lineno", 0))
            for block in _blocks(statement):
                if scan(block, wrote) and not _terminates(block):
                    wrote = True
        return wrote

    tree = ast.parse(textwrap.dedent(source))
    scan(tree.body, False)
    return violations


def subcommand_parsers() -> dict[str, argparse.ArgumentParser]:
    found: dict[str, argparse.ArgumentParser] = {}

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, child in action.choices.items():
                label = f"{prefix} {name}".strip()
                if any(
                    isinstance(item, argparse._SubParsersAction)
                    for item in child._actions
                ):
                    walk(child, label)
                else:
                    found[label] = child

    walk(noetrail.cli.build_parser(), "")
    return found


class MutationDeclarationTest(unittest.TestCase):
    def test_every_subcommand_declares_a_handler_and_lock_mode(self) -> None:
        parsers = subcommand_parsers()
        self.assertGreaterEqual(len(parsers), 20)
        for label, parser in parsers.items():
            defaults = parser._defaults
            self.assertIn("handler", defaults, label)
            self.assertIn("mutating", defaults, label)
            declared = defaults["mutating"]
            self.assertTrue(
                callable(declared) or isinstance(declared, bool),
                f"{label} declares an unusable lock mode",
            )

    def test_every_writing_handler_requests_the_exclusive_lock(self) -> None:
        inspected = 0
        for label, parser in subcommand_parsers().items():
            handler = parser._defaults["handler"]
            declared = parser._defaults["mutating"]
            source = inspect.getsource(handler)
            # `def command_tag(` would otherwise match the marker derived from
            # the handler itself and make every handler look like a writer.
            own = f"{handler.__name__}("
            writes = [
                marker
                for marker in write_markers()
                if marker != own and marker in source
            ]
            if not writes:
                continue
            inspected += 1
            self.assertNotEqual(
                declared,
                False,
                f"{label} writes ({', '.join(writes)}) but takes a shared lock",
            )
        # Guards against the markers silently matching nothing after a rename.
        self.assertGreaterEqual(inspected, 14)

    def test_no_handler_reads_the_shared_index_after_it_has_written(
        self,
    ) -> None:
        inspected = 0
        for label, parser in subcommand_parsers().items():
            source = inspect.getsource(parser._defaults["handler"])
            if "args.index" not in source:
                continue
            inspected += 1
            self.assertEqual(
                stale_index_reads(source),
                [],
                f"{label} queries the vault index after writing, which "
                f"answers from the pre-write snapshot",
            )
        # Guards against the check quietly inspecting nothing after a rename.
        self.assertGreaterEqual(inspected, 10)

    def test_a_write_that_moved_into_a_helper_is_still_a_write(self) -> None:
        """The reason the marker list is derived instead of hand-written.

        `write_attachment_blob` contains no marker a reader would recognise at
        the call site -- it calls `os.replace` two frames down. Before the
        module split every write sat in the handler's own source, so a literal
        marker list was enough; now a handler may delegate, and the list has to
        follow the call graph or the rule silently stops applying to whatever
        was moved out.
        """

        derived = set(write_markers()) - set(DIRECT_WRITE_MARKERS)
        self.assertIn("write_attachment_blob(", derived)
        self.assertIn("relocate_entry(", DIRECT_WRITE_MARKERS)
        # Reaching a write only through a helper is enough.
        self.assertNotIn(
            "os.replace(",
            inspect.getsource(noetrail.commands.edit.command_attach),
        )
        self.assertIn(
            "write_attachment_blob(",
            inspect.getsource(noetrail.commands.edit.command_attach),
        )
        # And a read-only helper must not be swept in with them.
        self.assertNotIn("find_entry(", derived)

    def test_supersession_computes_the_new_relations_without_writing(
        self,
    ) -> None:
        """Both halves of a replacement have to land in one file write.

        `relate --supersedes` closes the previous relation and adds its
        replacement. If the helper that does that persisted the closed relation
        itself, the entry would briefly hold a state in which the old assertion
        had already ended and the new one did not yet exist -- and a failure
        between the two writes would leave it that way.

        The rule is enforced the same way the lock rule above is: the helper
        must not appear in the derived write-marker set, which means no path
        from it reaches a write. It returns the new relation list, and
        `command_relate` writes once.
        """

        derived = set(write_markers())
        self.assertNotIn("apply_relation_validity(", derived)
        self.assertNotIn("check_validity_arguments(", derived)
        self.assertNotIn("find_edge(", derived)
        source = inspect.getsource(noetrail.commands.edit.command_relate)
        self.assertIn("apply_relation_validity(", source)
        # And the handler is still classified as a writer, so it keeps the
        # exclusive lock it already declared.
        self.assertIn("atomic_write_entry(", source)

    def test_the_stale_index_check_catches_a_planted_violation(self) -> None:
        """Otherwise the check above passes for the wrong reason."""

        planted = """
            def command_broken(args, root):
                path, metadata, body = find_entry(args.index, args.id)
                atomic_write_entry(path, metadata, body)
                if duplicate_titles(args.index, "note", "T"):
                    raise SystemExit("duplicate")
                return 0
        """
        self.assertNotEqual(stale_index_reads(planted), [])

        accepted = """
            def command_fine(args, root):
                path, metadata, body = find_entry(args.index, args.id)
                if args.remove:
                    atomic_write_entry(path, metadata, body)
                    return 0
                validate_relation_objects([], args.index, args.layout)
                atomic_write_entry(path, metadata, body)
                return 0
        """
        self.assertEqual(stale_index_reads(accepted), [])

    def test_index_maintenance_runs_inside_the_lock_the_command_took(
        self,
    ) -> None:
        """The one write that is not in a handler, kept where it is safe.

        The derived BM25 index is refreshed by the dispatcher rather than by
        each writing handler: one place cannot be forgotten by a new command,
        and it keeps `args.index` -- the pre-write scan snapshot the check
        above is about -- out of the refresh entirely, because the refresh
        reads the files.

        That only holds while the call sits inside the `with vault_lock(...)`
        block. Outside it, a refresh would read a vault another process is
        already writing to and store a signature digest for a state that never
        existed as a whole.
        """

        source = textwrap.dedent(inspect.getsource(noetrail.cli.run_command))
        tree = ast.parse(source)
        locked = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            and any(
                "vault_lock" in ast.unparse(item.context_expr)
                for item in node.items
            )
        ]
        self.assertEqual(len(locked), 1, "run_command no longer takes one lock")
        inside = {
            ast.unparse(node.func).rsplit(".", 1)[-1]
            for statement in locked[0].body
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
        }
        self.assertIn("refresh_if_present", inside)
        self.assertEqual(
            source.count("refresh_if_present("),
            1,
            "index maintenance is called from more than one place",
        )
        # And it must be gated on the command having declared a write, so a
        # read never rewrites the cache under a shared lock.
        guard = next(
            node
            for node in ast.walk(locked[0])
            if isinstance(node, ast.If)
            and "refresh_if_present" in ast.unparse(node)
        )
        self.assertIn("mutating", ast.unparse(guard.test))

    def test_the_index_subcommands_split_reads_from_writes(self) -> None:
        """`index status` must not block writers to report a cache state."""

        parsers = subcommand_parsers()
        self.assertIs(parsers["index status"]._defaults["mutating"], False)
        for label in ("index rebuild", "index drop"):
            self.assertIsNot(parsers[label]._defaults["mutating"], False, label)
        self.assertEqual(
            len(
                {
                    parsers[label]._defaults["handler"]
                    for label in ("index status", "index rebuild", "index drop")
                }
            ),
            3,
            "one shared handler would force the exclusive lock on all three",
        )

    def test_missing_declaration_is_rejected_instead_of_silently_shared(self) -> None:
        namespace = argparse.Namespace(command="capture")
        with self.assertRaises(InternalError):
            noetrail.cli.command_is_mutating(namespace)


class ConcurrentVaultAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.data.mkdir()
        self.config.mkdir()
        self.assertEqual(self.run_cli("init").returncode, 0)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(self.data),
                "--config-root",
                str(self.config),
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_parallel_captures_produce_two_valid_entries(self) -> None:
        texts = [f"Parallel capture number {index}." for index in range(4)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda text: self.run_cli("capture", "--text", text),
                    texts,
                )
            )

        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        created = {json.loads(result.stdout)["created"] for result in results}
        self.assertEqual(len(created), len(texts))

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)
        self.assertIn(f"{len(texts)} active", validated.stdout)

        listed = json.loads(self.run_cli("search", "").stdout)
        self.assertEqual(listed["total"], len(texts))

    def test_only_one_of_two_racing_updates_wins_with_the_same_revision(self) -> None:
        captured = json.loads(
            self.run_cli("capture", "--text", "Shared subject.").stdout
        )
        entry_id = captured["id"]
        revision = captured["revision"]

        patches = []
        for index, note in enumerate(["First writer.", "Second writer."]):
            patch = self.base / f"patch-{index}.json"
            patch.write_text(json.dumps({"append": note}), encoding="utf-8")
            patches.append(patch)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda patch: self.run_cli(
                        "update",
                        entry_id,
                        "--patch-file",
                        str(patch),
                        "--expected-revision",
                        revision,
                    ),
                    patches,
                )
            )

        successes = [item for item in results if item.returncode == 0]
        failures = [item for item in results if item.returncode != 0]
        self.assertEqual(len(successes), 1, [item.stderr for item in results])
        self.assertEqual(len(failures), 1)
        self.assertIn("revision", failures[0].stderr.casefold())

        validated = self.run_cli("validate")
        self.assertEqual(validated.returncode, 0, validated.stdout)


if __name__ == "__main__":
    unittest.main()
