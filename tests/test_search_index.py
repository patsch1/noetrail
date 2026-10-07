"""The derived BM25 index, and the rules that keep it from lying.

The index is a cache of a vault whose Markdown a user may edit in an editor,
restore from a backup, or sync between machines. Every test here is about one
of four claims, because those are the claims that make a cache acceptable at
all in a system whose selling point is that the files are the truth:

* the same query returns the same answer with and without the index;
* an index that no longer matches the vault is detected, not used;
* a damaged index is discarded, not partially believed;
* the index survives concurrent writers, or is rejected.

Timings are deliberately absent. They belong in `tools/measure_search.py`,
where they can be reproduced; an assertion about milliseconds on shared CI
hardware measures the neighbours.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from noetrail import cli
from noetrail.errors import InvalidRequest, StorageError
from noetrail.index import (
    index_path,
    open_for_search,
    read_index,
    scan_signatures,
)
from noetrail.layout import resolve_layout
from noetrail.schema import SchemaRegistry
from noetrail.search import (
    MemoryTermSource,
    inverse_document_frequency,
    registry_fingerprint,
    score_query,
    term_frequencies,
    tokenize,
)
from noetrail.store import entry_paths
from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILTINS_ROOT = REPOSITORY_ROOT / ".knowledge"


class TokenizerTest(unittest.TestCase):
    def test_tokens_are_casefolded_and_script_agnostic(self) -> None:
        self.assertEqual(tokenize("Basalt CADENCE"), ["basalt", "cadence"])
        self.assertEqual(tokenize("Straße"), ["strasse"])
        self.assertEqual(tokenize("Ελληνικά"), ["ελληνικά"])
        self.assertEqual(tokenize("日本語 テスト"), ["日本語", "テスト"])

    def test_compatibility_forms_fold_to_the_same_token(self) -> None:
        # NFKC, so a full-width or ligature spelling of a word is the same
        # word. A vault holds text pasted from anywhere.
        self.assertEqual(tokenize("ﬁle"), tokenize("file"))
        self.assertEqual(tokenize("ＡＢＣ"), ["abc"])

    def test_separators_and_oversized_runs_are_dropped(self) -> None:
        self.assertEqual(
            tokenize("snake_case, kebab-case; dotted.name"),
            ["snake", "case", "kebab", "case", "dotted", "name"],
        )
        self.assertEqual(tokenize("a" * 65), [])
        self.assertEqual(tokenize("a" * 64), ["a" * 64])

    def test_no_stemming_is_applied(self) -> None:
        """Stated as a test because it is a design decision, not an omission.

        A stemmer that is right for English is wrong for German compounds, and
        a personal vault is written in whatever its owner writes in.
        """

        self.assertNotEqual(tokenize("noodles"), tokenize("noodle"))
        self.assertNotEqual(tokenize("running"), tokenize("run"))


class ScoringTest(unittest.TestCase):
    def test_idf_stays_positive_for_a_term_in_every_document(self) -> None:
        # The textbook form goes negative past half the collection, which lets
        # a common word subtract from a score.
        self.assertGreater(inverse_document_frequency(100, 100), 0.0)
        self.assertGreater(
            inverse_document_frequency(100, 1),
            inverse_document_frequency(100, 50),
        )

    def test_a_rarer_term_outranks_a_common_one(self) -> None:
        source = MemoryTermSource()
        for index in range(10):
            text = "common word here" + (" rare" if index == 3 else "")
            source.add(term_frequencies(text))
        scores = score_query(source, ["rare", "common"])
        self.assertEqual(max(scores, key=lambda key: scores[key]), 3)

    def test_a_shorter_document_outranks_a_longer_one_with_the_same_term(
        self,
    ) -> None:
        source = MemoryTermSource()
        source.add(term_frequencies("basalt"))
        source.add(term_frequencies("basalt " + " ".join(f"w{i}" for i in range(50))))
        scores = score_query(source, ["basalt"])
        self.assertGreater(scores[0], scores[1])


class VaultHarness:
    """Drives the real CLI in this process against a temporary vault."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.assertEqual(self.run_cli("init"), 0)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> int:
        self.stdout = io.StringIO()
        with contextlib.redirect_stdout(self.stdout):
            return cli.run_command(
                [
                    "--data-root",
                    str(self.data),
                    "--config-root",
                    str(self.config),
                    "--builtins-root",
                    str(BUILTINS_ROOT),
                    *arguments,
                ]
            )

    def json_cli(self, *arguments: str) -> dict:
        self.assertEqual(self.run_cli(*arguments), 0, self.stdout.getvalue())
        return json.loads(self.stdout.getvalue())

    def layout(self):
        return resolve_layout(
            data_root=str(self.data),
            config_root=str(self.config),
            builtins_root=str(BUILTINS_ROOT),
        )

    def registry(self) -> SchemaRegistry:
        return SchemaRegistry.load(self.layout())

    def capture(self, title: str, text: str, **extra: str) -> str:
        arguments = ["capture", "--type", "note", "--title", title, "--text", text]
        for key, value in extra.items():
            arguments.extend([f"--{key.replace('_', '-')}", value])
        return str(self.json_cli(*arguments)["id"])

    def seed(self) -> dict[str, str]:
        return {
            "grinder": self.capture(
                "Espresso grind size",
                "A finer grind raises extraction and pressure in the basket.",
                tag="coffee",
            ),
            "bitter": self.capture(
                "Bitter shots",
                "The shots tasted bitter and hollow after the new bag.",
                tag="coffee",
            ),
            "chain": self.capture(
                "Chain wear",
                "Replacing the chain early saves the cassette on a bicycle.",
                tag="cycling",
            ),
            "salad": self.capture(
                "Bitter greens",
                "Radicchio and chicory balanced with a sweet dressing.",
                tag="food",
            ),
        }

    def search(self, *arguments: str) -> dict:
        return self.json_cli("search", *arguments)

    def index_state(self, *arguments: str) -> dict:
        """`index status` reports a stale cache with exit 1, which is not a
        failure the test harness should treat as one."""

        self.run_cli("index", "status", *arguments)
        return json.loads(self.stdout.getvalue())


class IndexedVaultTest(VaultHarness, unittest.TestCase):
    # -- the four claims --------------------------------------------------

    def test_the_indexed_answer_equals_the_scanned_answer(self) -> None:
        """The property that makes the cache safe to have at all.

        Every field of the response is compared, not just the identifiers: a
        cache that got `total`, `complete`, or the page boundary wrong would
        be just as much a second source of truth as one that got the hits
        wrong.
        """

        self.seed()
        cases: list[tuple[str, tuple[str, ...]]] = [
            ("bitter", ()),
            ("espresso extraction bitter", ()),
            ("chain cassette", ()),
            ("bitter", ("--sort", "title_asc")),
            ("bitter", ("--sort", "updated_desc")),
            ("bitter", ("--limit", "1")),
            ("bitter", ("--limit", "1", "--offset", "1")),
            ("chain", ("--type", "note")),
            ("nothing-matches-this", ()),
            ("", ()),
            ("grind", ("--bm25-k1", "0.4", "--bm25-b", "0.2")),
        ]

        def ask(query: str, options: tuple[str, ...]) -> dict:
            return self.search("--rank", "bm25", *options, "--", query)

        self.json_cli("index", "rebuild")
        with_index = [ask(query, options) for query, options in cases]
        self.json_cli("index", "drop")
        without_index = [ask(query, options) for query, options in cases]
        for case, indexed, scanned in zip(
            cases, with_index, without_index, strict=True
        ):
            self.assertEqual(indexed, scanned, case)
        # And a query that matches nothing must not silently match everything
        # on one of the two paths.
        self.assertEqual(with_index[8]["total"], 0)
        self.assertEqual(with_index[9]["total"], 4)

    def test_an_edited_entry_makes_the_index_stale_and_search_stays_correct(
        self,
    ) -> None:
        identifiers = self.seed()
        self.json_cli("index", "rebuild")
        self.assertEqual(self.index_state()["state"], "fresh")

        # An out-of-band edit: what an editor, a sync client, or a restore
        # does. Nothing tells Noetrail about it.
        target = next(
            path
            for path in entry_paths(self.data)
            if identifiers["chain"] in path.read_text(encoding="utf-8")
        )
        target.write_text(
            target.read_text(encoding="utf-8").replace("cassette", "quernstone"),
            encoding="utf-8",
        )

        status = self.index_state()
        self.assertEqual(status["state"], "stale_vault")
        self.assertEqual(status["changed_entry_count"], 2)

        # The old word is gone and the new one is found, both immediately.
        self.assertEqual(self.search("--rank", "bm25", "cassette")["total"], 0)
        found = self.search("--rank", "bm25", "quernstone")
        self.assertEqual(
            [item["id"] for item in found["items"]], [identifiers["chain"]]
        )

    def test_a_stale_index_is_never_the_source_of_a_hit(self) -> None:
        """Deleting an entry outside Noetrail must not leave a ghost."""

        identifiers = self.seed()
        self.json_cli("index", "rebuild")
        target = next(
            path
            for path in entry_paths(self.data)
            if identifiers["salad"] in path.read_text(encoding="utf-8")
        )
        target.unlink()
        results = self.search("--rank", "bm25", "radicchio")
        self.assertEqual(results["total"], 0)
        self.assertEqual(self.index_state()["state"], "stale_vault")

    def test_a_corrupt_index_is_rebuilt_rather_than_partially_believed(self) -> None:
        self.seed()
        self.json_cli("index", "rebuild")
        stored = index_path(self.layout())
        expected = self.search("--rank", "bm25", "bitter")

        for damage in (
            lambda raw: raw[: len(raw) // 2],  # truncated mid-write
            lambda raw: raw + b"\x00" * 16,  # extended
            lambda raw: raw.replace(b"bitter", b"bxtter"),  # flipped payload byte
            lambda raw: b"not json at all\n" + raw.split(b"\n", 1)[1],
            lambda raw: b"",
        ):
            raw = stored.read_bytes()
            stored.write_bytes(damage(raw))
            self.assertEqual(
                self.search("--rank", "bm25", "bitter"),
                expected,
                "a damaged index changed the answer",
            )
            self.assertNotEqual(self.index_state()["state"], "fresh")
            self.json_cli("index", "rebuild", "--full")

    def test_the_index_is_dropped_and_rebuilt_without_losing_anything(self) -> None:
        self.seed()
        before = self.search("--rank", "bm25", "bitter")
        self.assertEqual(self.index_state()["state"], "absent")
        self.json_cli("index", "rebuild")
        self.assertEqual(self.search("--rank", "bm25", "bitter"), before)
        dropped = self.json_cli("index", "drop")
        self.assertTrue(dropped["dropped"])
        self.assertFalse(index_path(self.layout()).exists())
        self.assertEqual(self.search("--rank", "bm25", "bitter"), before)
        self.assertFalse(self.json_cli("index", "drop")["dropped"])

    # -- maintenance ------------------------------------------------------

    def test_a_write_refreshes_an_existing_index_and_creates_none(self) -> None:
        """Index maintenance is opt-in, and free for anyone who opted out."""

        self.seed()
        self.capture("Later note", "A vellum kestrel appears.")
        self.assertEqual(self.index_state()["state"], "absent")

        self.json_cli("index", "rebuild")
        self.capture("Even later", "An obsidian trellis appears.")
        self.assertEqual(self.index_state()["state"], "fresh")
        self.assertEqual(self.search("--rank", "bm25", "obsidian")["total"], 1)

        # And a deletion has to remove the entry from the postings, not merely
        # from the file listing.
        identifier = self.search("--rank", "bm25", "obsidian")["items"][0]["id"]
        self.json_cli("trash", str(identifier))
        self.assertEqual(self.index_state()["state"], "fresh")
        self.assertEqual(self.search("--rank", "bm25", "obsidian")["total"], 0)

    def test_an_incremental_refresh_matches_a_full_rebuild(self) -> None:
        identifiers = self.seed()
        self.json_cli("index", "rebuild")
        for number in range(5):
            self.capture(f"Extra {number}", f"harbour lantern number {number}")
        self.json_cli("trash", identifiers["salad"])
        incremental = self.search("--rank", "bm25", "harbour lantern grind")

        self.json_cli("index", "rebuild", "--full")
        self.assertEqual(
            self.search("--rank", "bm25", "harbour lantern grind"), incremental
        )
        self.assertIs(self.index_state("--verify")["verified"], True)

    def test_verification_catches_a_change_the_signature_cannot_see(self) -> None:
        """The documented blind spot of every mtime-based cache, with a test.

        A rewrite that keeps the byte length and restores the modification
        time is invisible to the cheap check that runs on every query. It is
        not invisible to `index status --verify`, which is why that flag
        exists rather than being a claim in a document.
        """

        self.seed()
        self.json_cli("index", "rebuild")
        target = entry_paths(self.data)[0]
        status = target.stat()
        original = target.read_text(encoding="utf-8")
        forged = original.replace("Radicchio", "Rxdicchio").replace(
            "extraction", "extractiXn"
        )
        self.assertEqual(len(forged), len(original))
        self.assertNotEqual(forged, original)
        target.write_text(forged, encoding="utf-8")
        os.utime(target, ns=(status.st_atime_ns, status.st_mtime_ns))

        self.assertEqual(self.index_state()["state"], "fresh")
        self.assertEqual(self.run_cli("index", "status", "--verify"), 1)
        drifted = json.loads(self.stdout.getvalue())
        self.assertEqual(drifted["state"], "drifted")
        self.assertFalse(drifted["verified"])

    def test_a_changed_searchable_declaration_invalidates_the_index(self) -> None:
        """Nothing about the entries changed, but the indexed text did."""

        self.seed()
        self.json_cli("index", "rebuild")
        stored, state = open_for_search(self.data, self.layout(), self.registry())
        self.assertEqual(state, "fresh")
        self.assertIsNotNone(stored)

        registry = self.registry()
        for definition in registry.types.values():
            for field in definition.fields.values():
                object.__setattr__(field, "searchable", not field.searchable)
            break
        self.assertNotEqual(
            registry_fingerprint(registry),
            read_index(self.layout()).header["registry_fingerprint"],
        )
        _, state = open_for_search(self.data, self.layout(), registry)
        self.assertEqual(state, "stale_registry")

    def test_the_index_lives_outside_the_data_root(self) -> None:
        """Restore is a file operation over the data root and must stay one."""

        self.seed()
        self.json_cli("index", "rebuild")
        stored = index_path(self.layout())
        self.assertTrue(stored.is_file())
        self.assertTrue(stored.is_relative_to(self.config))
        self.assertFalse(stored.is_relative_to(self.data))
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)

    def test_two_data_roots_do_not_share_one_config_root_index(self) -> None:
        second = self.base / "data2"
        second.mkdir()
        subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(second),
                "--config-root",
                str(self.config),
                "init",
            ],
            check=True,
            capture_output=True,
        )
        self.seed()
        self.json_cli("index", "rebuild")
        first = index_path(self.layout())
        other = index_path(
            resolve_layout(
                data_root=str(second),
                config_root=str(self.config),
                builtins_root=str(BUILTINS_ROOT),
            )
        )
        self.assertNotEqual(first, other)
        self.assertFalse(other.exists())

    def test_a_read_only_config_root_refuses_cleanly_and_never_blocks_a_write(
        self,
    ) -> None:
        """A read-only instance config root is a supported deployment.

        `index rebuild` has to say so in a sentence; a `capture` must not fail
        because a cache could not be updated, since its entry is already in
        the vault by then.
        """

        self.seed()
        # Standing in for a read-only mount, which a test running as root
        # cannot produce with `chmod`: the directory the index needs cannot be
        # created, which is the same OSError from the same call.
        blocked = self.config / "index"
        blocked.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(StorageError) as raised:
            self.run_cli("index", "rebuild")
        self.assertIn("Search index cannot be written", str(raised.exception))

        self.assertEqual(
            self.run_cli(
                "capture", "--type", "note", "--title", "Still", "--text", "works"
            ),
            0,
        )
        # The write landed, and search answers from the vault regardless.
        self.assertEqual(self.search("--rank", "bm25", "works")["total"], 1)
        # And `doctor` reports the obstruction rather than pretending the
        # cache is fine; a real read-only mount with no `index/` at all
        # reports `absent` instead, which is not a warning.
        self.assertEqual(self.index_state()["state"], "unreadable")

    def test_the_signature_walk_covers_exactly_the_scanned_files(self) -> None:
        """`scan_signatures` reimplements the walk; this pins it to the scan.

        A file the fallback scan sees and the freshness check does not would
        be an entry that could change without ever making the index stale.
        """

        self.seed()
        nested = self.data / "vault" / "notes" / "deep" / "deeper"
        nested.mkdir(parents=True)
        (nested / "buried.md").write_text("---\nid: x\n---\n", encoding="utf-8")
        (self.data / "vault" / "notes" / "not-markdown.txt").write_text(
            "ignored", encoding="utf-8"
        )
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "linked.md").write_text("---\nid: y\n---\n", encoding="utf-8")
        (self.data / "vault" / "linked-away").symlink_to(outside)
        (self.data / "vault" / "notes" / "linked-file.md").symlink_to(
            outside / "linked.md"
        )

        expected = {
            path.relative_to(self.data).as_posix() for path in entry_paths(self.data)
        }
        self.assertEqual(set(scan_signatures(self.data).signatures), expected)


class ConcurrentIndexTest(unittest.TestCase):
    """Real processes, real advisory locks, one shared index file."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.data.mkdir()
        self.config.mkdir()
        self.assertEqual(self.run_process("init").returncode, 0)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_process(self, *arguments: str) -> subprocess.CompletedProcess[str]:
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

    def test_the_index_survives_concurrent_writers(self) -> None:
        """Six processes capture at once while an index exists.

        Each holds the exclusive vault lock for its own write and refreshes
        the index inside it, so the file is rewritten six times by six
        processes. The result has to be one whole index that matches the vault
        -- not a torn file, and not one missing the entries written by the
        writers that lost the race.
        """

        self.assertEqual(self.run_process("index", "rebuild").returncode, 0)
        words = [f"quernstone{index}" for index in range(6)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(
                pool.map(
                    lambda word: self.run_process(
                        "capture", "--text", f"Parallel {word} entry."
                    ),
                    words,
                )
            )
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)

        status = json.loads(self.run_process("index", "status", "--verify").stdout)
        self.assertEqual(status["state"], "fresh", status)
        self.assertTrue(status["verified"])
        self.assertEqual(status["document_count"], len(words))
        for word in words:
            found = json.loads(
                self.run_process("search", "--rank", "bm25", "--", word).stdout
            )
            self.assertEqual(found["total"], 1, word)

    def test_a_reader_never_sees_a_half_written_index(self) -> None:
        """Searches and rebuilds interleave without a single bad answer."""

        for number in range(12):
            written = self.run_process(
                "capture", "--text", f"Lantern basalt entry {number}."
            )
            self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(self.run_process("index", "rebuild").returncode, 0)

        def rebuild(_: int) -> str:
            return self.run_process("index", "rebuild", "--full").stdout

        def query(_: int) -> str:
            return self.run_process("search", "--rank", "bm25", "basalt").stdout

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            searches = list(pool.map(query, range(6)))
            rebuilds = list(pool.map(rebuild, range(3)))
        for raw in rebuilds:
            self.assertEqual(json.loads(raw)["documents"], 12)
        for raw in searches:
            self.assertEqual(json.loads(raw)["total"], 12)


class SearchSurfaceTest(VaultHarness, unittest.TestCase):
    """The command surface around the ranking mode."""

    def test_substring_stays_available_and_is_unchanged(self) -> None:
        """The mode is no longer the default; its behaviour has not moved.

        It stopped being the default because returning nothing for two words
        that never occur adjacently is a poor answer to a question. It stayed
        in the surface because a prefix of a longer word is something no
        tokenised ranking can match, and hybrid keeps that half by running
        this engine, not by approximating it.
        """

        self.seed()
        self.json_cli("index", "rebuild")
        literal = ("--rank", "substring")
        self.assertEqual(self.search(*literal, "espresso bitter")["total"], 0)
        self.assertEqual(
            self.search(*literal, "espresso bitter")["sort"], "updated_desc"
        )
        self.assertEqual(self.search(*literal, "extractio")["total"], 1)
        self.assertEqual(self.search("--rank", "bm25", "extractio")["total"], 0)
        # And the half that hybrid inherits from it.
        self.assertEqual(self.search("--rank", "hybrid", "extractio")["total"], 1)

    def test_bm25_defaults_to_relevance_but_not_for_an_empty_query(self) -> None:
        self.seed()
        self.assertEqual(self.search("--rank", "bm25", "bitter")["sort"], "relevance")
        self.assertEqual(self.search("--rank", "bm25", "")["sort"], "updated_desc")
        self.assertEqual(self.search("--rank", "bm25", "")["total"], 4)

    def test_relevance_ordering_puts_the_better_match_first(self) -> None:
        identifiers = self.seed()
        results = self.search("--rank", "bm25", "bitter shots espresso")
        self.assertEqual(results["items"][0]["id"], identifiers["bitter"])
        self.assertIn(identifiers["salad"], [item["id"] for item in results["items"]])

    def test_relevance_sorting_requires_a_ranking_mode(self) -> None:
        self.seed()
        with self.assertRaises(InvalidRequest) as raised:
            self.run_cli(
                "search", "--rank", "substring", "--sort", "relevance", "bitter"
            )
        self.assertIn("relevance", str(raised.exception))
        # Both ranking modes produce one, so neither may refuse it.
        for rank in ("bm25", "hybrid"):
            self.assertEqual(
                self.search("--rank", rank, "--sort", "relevance", "bitter")["sort"],
                "relevance",
            )

    def test_out_of_range_bm25_parameters_are_refused(self) -> None:
        self.seed()
        for arguments in (
            ("--bm25-k1", "-1"),
            ("--bm25-b", "1.5"),
            ("--bm25-b", "-0.1"),
        ):
            with self.assertRaises(InvalidRequest):
                self.run_cli("search", "--rank", "bm25", "bitter", *arguments)

    def test_filters_and_pagination_apply_to_the_ranked_set(self) -> None:
        identifiers = self.seed()
        self.json_cli("index", "rebuild")
        filtered = self.search(
            "--rank", "bm25", "--related-id", identifiers["chain"], "bitter"
        )
        self.assertEqual(filtered["total"], 0)
        page = self.search("--rank", "bm25", "--limit", "1", "bitter")
        self.assertEqual(page["returned"], 1)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 1)

    def test_doctor_reports_the_index_without_calling_it_an_error(self) -> None:
        """A cache that is stale costs latency, never correctness."""

        self.seed()
        report = self.json_cli("doctor")
        self.assertEqual(report["search_index"]["state"], "absent")
        self.json_cli("index", "rebuild")
        self.assertEqual(self.json_cli("doctor")["search_index"]["state"], "fresh")

        index_path(self.layout()).write_bytes(b"ruined")
        damaged = self.json_cli("doctor")
        self.assertTrue(damaged["ok"])
        check = next(
            item for item in damaged["checks"] if item["name"] == "search_index"
        )
        self.assertEqual(check["status"], "warning")
        self.assertIn("index rebuild", check["detail"])


if __name__ == "__main__":
    unittest.main()
