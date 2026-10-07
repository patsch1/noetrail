from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sysconfig
import tempfile
import unittest
from unittest.mock import patch

from tests import CLI_COMMAND, MCP_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

from noetrail import layout as layout_module  # noqa: E402
from noetrail.layout import (  # noqa: E402
    LayoutError,
    NoetrailLayout,
    default_application_root,
    default_builtins_root,
    default_config_root,
    resolve_layout,
)

RELATIONS = (
    "relations:\n"
    "  related_to:\n"
    "    symmetric: true\n"
    "  involves:\n"
    "    inverse: experienced_in\n"
    "  took_place_at:\n"
    "    inverse: hosted_experience\n"
)


class NoetrailLayoutTest(unittest.TestCase):
    def make_roots(self, base: Path) -> tuple[Path, Path, Path, Path]:
        application = base / "application"
        builtins = base / "builtins"
        config = base / "config"
        data = base / "data"
        for path in (application, builtins, config, data):
            path.mkdir()
        (builtins / "SPEC.md").write_text("synthetic spec", encoding="utf-8")
        (builtins / "relation-types.yaml").write_text(
            "relations:\n  builtin_only:\n    symmetric: true\n",
            encoding="utf-8",
        )
        (config / "relation-types.yaml").write_text(
            RELATIONS,
            encoding="utf-8",
        )
        shutil.copytree(
            REPOSITORY_ROOT / ".knowledge" / "packs" / "knowledge-core",
            builtins / "packs" / "knowledge-core",
        )
        return application, builtins, config, data

    def run_cli(
        self,
        data: Path,
        config: Path,
        builtins: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(data),
                "--config-root",
                str(config),
                "--builtins-root",
                str(builtins),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_separated_roots_support_cli_without_source_in_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            _, builtins, config, data = self.make_roots(base)
            builtins_before = {
                path.relative_to(builtins): path.read_bytes()
                for path in builtins.rglob("*")
                if path.is_file()
            }

            first = self.run_cli(
                data,
                config,
                builtins,
                "capture",
                "--type",
                "note",
                "--title",
                "Separated first",
                "--text",
                "Synthetic content.",
            )
            second = self.run_cli(
                data,
                config,
                builtins,
                "capture",
                "--type",
                "note",
                "--title",
                "Separated second",
                "--text",
                "Synthetic content.",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            first_result = json.loads(first.stdout)
            second_result = json.loads(second.stdout)

            related = self.run_cli(
                data,
                config,
                builtins,
                "relate",
                first_result["id"],
                "related_to",
                second_result["id"],
                "--expected-revision",
                first_result["revision"],
            )
            self.assertEqual(related.returncode, 0, related.stderr)
            validated = self.run_cli(data, config, builtins, "validate")
            self.assertEqual(validated.returncode, 0, validated.stdout)

            self.assertTrue((data / first_result["created"]).is_file())
            self.assertFalse((data / ".knowledge").exists())
            self.assertFalse((data / "tools").exists())
            self.assertFalse((config / "vault").exists())
            self.assertFalse((builtins / "vault").exists())
            self.assertEqual(
                builtins_before,
                {
                    path.relative_to(builtins): path.read_bytes()
                    for path in builtins.rglob("*")
                    if path.is_file()
                },
            )

    def test_separated_roots_are_forwarded_through_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            _, builtins, config, data = self.make_roots(base)
            process = subprocess.Popen(
                [
                    *MCP_COMMAND,
                    "--data-root",
                    str(data),
                    "--config-root",
                    str(config),
                    "--builtins-root",
                    str(builtins),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            try:
                assert process.stdin is not None
                assert process.stdout is not None
                process.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "capture",
                                "arguments": {
                                    "type": "note",
                                    "title": "Separated MCP",
                                    "text": "Synthetic MCP content.",
                                },
                            },
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                response = json.loads(process.stdout.readline())
                self.assertNotIn("error", response)
                result = response["result"]["structuredContent"]
                self.assertTrue((data / result["created"]).is_file())
                self.assertFalse((data / ".knowledge").exists())
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_legacy_combined_root_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = temporary_root(directory)
            knowledge = root / ".knowledge"
            knowledge.mkdir()
            (knowledge / "SPEC.md").write_text("test", encoding="utf-8")
            (knowledge / "relation-types.yaml").write_text(
                RELATIONS,
                encoding="utf-8",
            )
            layout = resolve_layout(root=root, application_root=root)
            resolved_root = root.resolve()
            resolved_knowledge = knowledge.resolve()
            self.assertEqual(layout.data_root, resolved_root)
            self.assertEqual(layout.config_root, resolved_knowledge)
            self.assertEqual(layout.builtins_root, resolved_knowledge)
            self.assertEqual(
                layout.cli_root_arguments(),
                ["--root", str(resolved_root)],
            )

    def test_config_pack_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            application, builtins, config, data = self.make_roots(base)
            outside = base / "outside-packs"
            outside.mkdir()
            (config / "packs").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(LayoutError, "must not be a symbolic link"):
                resolve_layout(
                    data_root=data,
                    config_root=config,
                    builtins_root=builtins,
                    application_root=application,
                )

    def test_separated_roots_can_be_selected_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            application, builtins, config, data = self.make_roots(base)
            with patch.dict(
                os.environ,
                {
                    "NOETRAIL_DATA_ROOT": str(data),
                    "NOETRAIL_CONFIG_ROOT": str(config),
                    "NOETRAIL_BUILTINS_ROOT": str(builtins),
                    "KNOWLEDGE_DATA_ROOT": str(base / "legacy-data"),
                    "KNOWLEDGE_CONFIG_ROOT": str(base / "legacy-config"),
                    "KNOWLEDGE_BUILTINS_ROOT": str(base / "legacy-builtins"),
                },
                clear=True,
            ):
                layout = resolve_layout(application_root=application)
            self.assertEqual(layout.data_root, data.resolve())
            self.assertEqual(layout.config_root, config.resolve())
            self.assertEqual(layout.builtins_root, builtins.resolve())
            self.assertIsNone(layout.compatibility_root)

    def test_legacy_environment_aliases_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            application, builtins, config, data = self.make_roots(base)
            with patch.dict(
                os.environ,
                {
                    "KNOWLEDGE_DATA_ROOT": str(data),
                    "KNOWLEDGE_CONFIG_ROOT": str(config),
                    "KNOWLEDGE_BUILTINS_ROOT": str(builtins),
                },
                clear=True,
            ):
                layout = resolve_layout(application_root=application)
            self.assertEqual(layout.data_root, data.resolve())
            self.assertEqual(layout.config_root, config.resolve())
            self.assertEqual(layout.builtins_root, builtins.resolve())

    def test_noetrail_defaults_prefer_new_paths_with_legacy_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            config_home = base / "config-home"
            legacy_config = config_home / "knowledge"
            legacy_config.mkdir(parents=True)
            with patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(config_home)},
                clear=False,
            ):
                self.assertEqual(default_config_root(), legacy_config)
                canonical_config = config_home / "noetrail"
                canonical_config.mkdir()
                self.assertEqual(default_config_root(), canonical_config)

            application = base / "application"
            legacy_builtins = (
                application / "share" / "personal-knowledge-vault" / ".knowledge"
            )
            legacy_builtins.mkdir(parents=True)
            (legacy_builtins / "SPEC.md").write_text("legacy", encoding="utf-8")
            self.assertEqual(default_builtins_root(application), legacy_builtins)

            canonical_builtins = application / "share" / "noetrail" / ".knowledge"
            canonical_builtins.mkdir(parents=True)
            (canonical_builtins / "SPEC.md").write_text(
                "canonical",
                encoding="utf-8",
            )
            self.assertEqual(default_builtins_root(application), canonical_builtins)

    def test_legacy_and_separated_options_cannot_be_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = temporary_root(directory)
            (root / ".knowledge").mkdir()
            (root / ".knowledge" / "SPEC.md").write_text(
                "test",
                encoding="utf-8",
            )
            (root / ".knowledge" / "relation-types.yaml").write_text(
                RELATIONS,
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LayoutError, "cannot be combined"):
                resolve_layout(
                    root=root,
                    data_root=root,
                    application_root=root,
                )


class LayoutRefusalTest(unittest.TestCase):
    """The rejections that a well-formed installation never reaches.

    These are the guards that keep a misconfigured or hostile layout from
    being used, so they need a test of their own: an end-to-end run only ever
    walks the happy path through them.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.application = self.base / "application"
        self.builtins = self.base / "builtins"
        self.config = self.base / "config"
        self.data = self.base / "data"
        for path in (self.application, self.builtins, self.config, self.data):
            path.mkdir()
        (self.builtins / "SPEC.md").write_text("spec", encoding="utf-8")
        (self.builtins / "relation-types.yaml").write_text(
            RELATIONS,
            encoding="utf-8",
        )
        # No NOETRAIL_/KNOWLEDGE_ variable may leak in: several of the tests
        # below depend on there being no separated layout in the environment.
        self.environment = patch.dict(
            os.environ,
            {
                name: value
                for name, value in os.environ.items()
                if not name.startswith(("NOETRAIL_", "KNOWLEDGE_"))
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def resolve(self, **overrides: object) -> NoetrailLayout:
        arguments: dict[str, object] = {
            "data_root": self.data,
            "config_root": self.config,
            "builtins_root": self.builtins,
            "application_root": self.application,
        }
        arguments.update(overrides)
        return resolve_layout(**arguments)  # type: ignore[arg-type]

    def test_a_root_that_is_a_file_is_rejected(self) -> None:
        regular = self.base / "not-a-directory"
        regular.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(LayoutError, "is not a directory"):
            self.resolve(data_root=regular)

    def test_a_missing_root_is_reported_as_unavailable(self) -> None:
        with self.assertRaisesRegex(LayoutError, "is unavailable"):
            self.resolve(data_root=self.base / "absent")

    def test_a_config_root_that_is_a_file_is_rejected(self) -> None:
        regular = self.base / "config-file"
        regular.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(LayoutError, "is not a directory"):
            self.resolve(config_root=regular)

    def test_a_dangling_config_root_symlink_is_reported(self) -> None:
        # A symlink that exists but points nowhere: the optional-directory
        # resolver has to resolve it strictly, which is the one case in which
        # it can see an OSError.
        dangling = self.base / "dangling"
        dangling.symlink_to(self.base / "absent")
        with self.assertRaisesRegex(LayoutError, "is unavailable"):
            self.resolve(config_root=dangling)

    def test_overlapping_roots_are_rejected(self) -> None:
        with self.assertRaisesRegex(LayoutError, "must be distinct"):
            self.resolve(config_root=self.data)

    def test_a_symlinked_specification_is_rejected(self) -> None:
        specification = self.builtins / "SPEC.md"
        specification.unlink()
        target = self.base / "elsewhere.md"
        target.write_text("spec", encoding="utf-8")
        specification.symlink_to(target)
        with self.assertRaisesRegex(LayoutError, "must not be a symbolic link"):
            self.resolve()

    def test_a_missing_specification_is_rejected(self) -> None:
        (self.builtins / "SPEC.md").unlink()
        with self.assertRaisesRegex(LayoutError, "does not exist"):
            self.resolve()

    def test_a_missing_builtin_relation_file_is_rejected(self) -> None:
        (self.builtins / "relation-types.yaml").unlink()
        with self.assertRaisesRegex(LayoutError, "does not exist"):
            self.resolve()

    def test_a_packs_path_that_is_a_file_is_rejected(self) -> None:
        (self.config / "packs").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(LayoutError, "is not a directory"):
            self.resolve()

    def test_a_relation_file_that_is_a_directory_is_rejected(self) -> None:
        (self.config / "relation-types.yaml").mkdir()
        with self.assertRaisesRegex(LayoutError, "is not a file"):
            self.resolve()

    def test_a_separated_layout_without_a_data_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(LayoutError, "requires --data-root"):
            resolve_layout(
                config_root=self.config,
                builtins_root=self.builtins,
                application_root=self.application,
            )

    def test_derived_roots_hang_below_the_data_root(self) -> None:
        layout = self.resolve()
        self.assertEqual(layout.vault_root, self.data / "vault")
        self.assertEqual(layout.trash_root, self.data / "trash")
        self.assertEqual(layout.imports_root, self.data / "imports")

    def test_a_combined_root_is_discovered_from_the_start_directory(self) -> None:
        combined = self.base / "combined"
        resources = combined / ".knowledge"
        resources.mkdir(parents=True)
        (resources / "SPEC.md").write_text("spec", encoding="utf-8")
        (resources / "relation-types.yaml").write_text(RELATIONS, encoding="utf-8")
        nested = combined / "a" / "b"
        nested.mkdir(parents=True)

        layout = resolve_layout(
            start=nested,
            application_root=self.application,
        )
        self.assertEqual(layout.data_root, combined.resolve())
        self.assertEqual(layout.cli_root_arguments(), ["--root", str(combined)])

    def test_no_combined_root_anywhere_is_reported(self) -> None:
        empty = self.base / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(LayoutError, "Could not find a combined"):
            resolve_layout(start=empty, application_root=self.application)

    def test_the_application_root_falls_back_to_the_installed_data_path(
        self,
    ) -> None:
        # In a checkout the loop finds the repository; an installed wheel has
        # no SPEC.md above the package and has to fall through to sysconfig.
        detached = self.base / "site-packages" / "noetrail" / "layout.py"
        detached.parent.mkdir(parents=True)
        with patch.object(layout_module, "__file__", str(detached)):
            self.assertEqual(
                default_application_root(),
                Path(sysconfig.get_path("data")).resolve(),
            )

    def test_the_builtins_root_stays_canonical_without_a_legacy_tree(
        self,
    ) -> None:
        self.assertEqual(
            default_builtins_root(self.application),
            self.application / "share" / "noetrail" / ".knowledge",
        )


if __name__ == "__main__":
    unittest.main()
