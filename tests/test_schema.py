from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests import CLI_COMMAND

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

from noetrail.layout import resolve_layout  # noqa: E402
from noetrail.schema import SchemaPackError, SchemaRegistry  # noqa: E402

BOOKS_MANIFEST = """\
format_version: 1
id: "books"
version: 1
title: "Books"
description: "Track books without changing Python code."
types:
  book:
    title: "Book"
    description: "A book the user reads or wants to read."
    template: "book.md"
    body_sections: ["Notes", "Quotes", "Personal review"]
    fields:
      author:
        type: "string"
        required: true
        searchable: true
        max_length: 500
      reading_state:
        type: "enum"
        values: ["wishlist", "reading", "read", "abandoned"]
        default: "wishlist"
        searchable: true
      pages:
        type: "integer"
        minimum: 1
      source_url:
        type: "url"
      topics:
        type: "array"
        items: "string"
        max_items: 20
"""


BUILTIN_MANIFEST = """\
format_version: 1
id: "catalog"
version: 1
title: "Catalog"
description: "Synthetic built-in test pack."
types:
  item:
    title: "Catalog item"
    description: "One synthetic catalog item."
    fields:
      code:
        type: "string"
        required: true
"""


SHORT_TYPE_MANIFEST = """\
format_version: 1
id: "core-test"
version: 1
title: "Core test"
description: "Synthetic short built-in type test."
types:
  note:
    entry_type: "note"
    title: "Note"
    description: "Synthetic note definition."
"""


class NoetrailSchemaRegistryTest(unittest.TestCase):
    def make_layout(self, base: Path):
        application = base / "application"
        builtins = base / "builtins"
        config = base / "config"
        data = base / "data"
        for path in (application, builtins, config, data):
            path.mkdir()
        (builtins / "SPEC.md").write_text("synthetic spec", encoding="utf-8")
        (builtins / "relation-types.yaml").write_text(
            "relations:\n  related_to:\n    symmetric: true\n",
            encoding="utf-8",
        )
        return resolve_layout(
            data_root=data,
            config_root=config,
            builtins_root=builtins,
            application_root=application,
        )

    def add_pack(
        self,
        root: Path,
        pack_id: str,
        manifest: str,
        *,
        template: str | None = None,
    ) -> Path:
        pack = root / "packs" / pack_id
        pack.mkdir(parents=True)
        (pack / "pack.yaml").write_text(manifest, encoding="utf-8")
        if template is not None:
            (pack / "book.md").write_text(template, encoding="utf-8")
        return pack

    def run_cli(self, layout, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(layout.data_root),
                "--config-root",
                str(layout.config_root),
                "--builtins-root",
                str(layout.builtins_root),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_registry_loads_builtin_and_local_packs_and_generates_schema(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            self.add_pack(
                layout.builtins_root,
                "catalog",
                BUILTIN_MANIFEST,
            )
            self.add_pack(
                layout.config_root,
                "books",
                BOOKS_MANIFEST,
                template="# Book notes\n",
            )

            registry = SchemaRegistry.load(layout)
            self.assertEqual(sorted(registry.packs), ["books", "catalog"])
            self.assertEqual(
                sorted(registry.types),
                ["books/book", "catalog/item"],
            )
            book = registry.require_type("books/book")
            self.assertEqual(book.source, "local")
            self.assertEqual(book.version, 1)
            self.assertEqual(book.body_sections[0], "Notes")
            self.assertTrue(book.template_path.is_file())
            schema = book.entry_json_schema()
            self.assertEqual(
                schema["properties"]["type"],  # type: ignore[index]
                {"const": "books/book"},
            )
            attributes = schema["properties"]["attributes"]  # type: ignore[index]
            self.assertEqual(attributes["required"], ["author"])
            self.assertEqual(
                attributes["properties"]["reading_state"]["default"],
                "wishlist",
            )
            self.assertFalse(attributes["additionalProperties"])

    def test_schema_cli_lists_validates_and_explains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            self.add_pack(
                layout.config_root,
                "books",
                BOOKS_MANIFEST,
                template="# Book notes\n",
            )

            listed = self.run_cli(layout, "schema", "list")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            list_result = json.loads(listed.stdout)
            self.assertTrue(list_result["valid"])
            self.assertEqual(list_result["type_count"], 1)
            self.assertEqual(list_result["types"][0]["id"], "books/book")

            validated = self.run_cli(layout, "schema", "validate")
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(json.loads(validated.stdout)["pack_count"], 1)

            explained = self.run_cli(
                layout,
                "schema",
                "explain",
                "books/book",
            )
            self.assertEqual(explained.returncode, 0, explained.stderr)
            explanation = json.loads(explained.stdout)
            self.assertEqual(explanation["id"], "books/book")
            self.assertIn("json_schema", explanation)

    def test_schema_cli_scaffolds_and_validates_uninstalled_packs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            output = Path(directory) / "candidate-packs"
            output.mkdir()

            initialized = self.run_cli(
                layout,
                "schema",
                "init",
                "field-notes",
                "--type",
                "observation",
                "--output",
                str(output),
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            result = json.loads(initialized.stdout)
            candidate = (output / "field-notes").resolve()
            self.assertEqual(Path(result["created"]), candidate)
            self.assertTrue((candidate / "pack.yaml").is_file())

            validated = self.run_cli(
                layout,
                "schema",
                "validate-pack",
                str(candidate),
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            report = json.loads(validated.stdout)
            self.assertTrue(report["valid"])
            self.assertEqual(report["types"][0]["id"], "field-notes/observation")
            self.assertIn("json_schema", report["types"][0])

            duplicate = self.run_cli(
                layout,
                "schema",
                "init",
                "field-notes",
                "--output",
                str(output),
            )
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn("already exists", duplicate.stderr)

            (candidate / "pack.yaml").write_text(
                (candidate / "pack.yaml").read_text(encoding="utf-8")
                + 'executable_hook: "bad"\n',
                encoding="utf-8",
            )
            invalid = self.run_cli(
                layout,
                "schema",
                "validate-pack",
                str(candidate),
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("unsupported keys", invalid.stderr)

    def test_invalid_pack_blocks_capture_before_vault_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            invalid = BOOKS_MANIFEST.replace(
                '        searchable: true\n',
                '        searchable: true\n        executable_hook: "bad"\n',
                1,
            )
            self.add_pack(
                layout.config_root,
                "books",
                invalid,
                template="# Book notes\n",
            )
            captured = self.run_cli(
                layout,
                "capture",
                "--type",
                "note",
                "--title",
                "Must not be written",
                "--text",
                "Synthetic content.",
            )
            self.assertNotEqual(captured.returncode, 0)
            self.assertIn("Invalid schema pack", captured.stderr)
            self.assertIn("unsupported keys", captured.stderr)
            self.assertFalse((layout.data_root / "vault").exists())

    def test_duplicate_pack_id_across_sources_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            self.add_pack(
                layout.builtins_root,
                "books",
                BOOKS_MANIFEST,
                template="# Built-in\n",
            )
            self.add_pack(
                layout.config_root,
                "books",
                BOOKS_MANIFEST,
                template="# Local\n",
            )
            with self.assertRaisesRegex(SchemaPackError, "duplicate pack ID"):
                SchemaRegistry.load(layout)

    def test_short_entry_types_are_reserved_for_bundled_or_combined_packs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            self.add_pack(
                layout.builtins_root,
                "core-test",
                SHORT_TYPE_MANIFEST,
            )
            registry = SchemaRegistry.load(layout)
            self.assertEqual(sorted(registry.builtin_types), ["note"])
            self.assertFalse(registry.require_type("note").qualified_id.count("/"))
            self.assertTrue(registry.require_type("note").builtin)

        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            self.add_pack(
                layout.config_root,
                "core-test",
                SHORT_TYPE_MANIFEST,
            )
            with self.assertRaisesRegex(
                SchemaPackError,
                "local packs cannot define short built-in entry types",
            ):
                SchemaRegistry.load(layout)

    def test_pack_directory_and_template_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            outside = Path(directory) / "outside-pack"
            outside.mkdir()
            packs = layout.config_root / "packs"
            packs.mkdir()
            (packs / "books").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(SchemaPackError, "symbolic link"):
                SchemaRegistry.load(layout)

        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            pack = self.add_pack(
                layout.config_root,
                "books",
                BOOKS_MANIFEST,
            )
            outside = Path(directory) / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            (pack / "book.md").symlink_to(outside)
            with self.assertRaisesRegex(SchemaPackError, "symbolic link"):
                SchemaRegistry.load(layout)

    def test_yaml_execution_features_and_unsupported_versions_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            anchored = BOOKS_MANIFEST.replace(
                'description: "Track books without changing Python code."',
                "description: &execute",
            )
            self.add_pack(
                layout.config_root,
                "books",
                anchored,
                template="# Book notes\n",
            )
            with self.assertRaisesRegex(SchemaPackError, "anchors"):
                SchemaRegistry.load(layout)

        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            future = BOOKS_MANIFEST.replace("format_version: 1", "format_version: 99")
            self.add_pack(
                layout.config_root,
                "books",
                future,
                template="# Book notes\n",
            )
            with self.assertRaisesRegex(
                SchemaPackError,
                "unsupported format_version",
            ):
                SchemaRegistry.load(layout)

    def test_array_defaults_must_obey_generated_uniqueness_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            duplicated = BOOKS_MANIFEST.replace(
                '        max_items: 20\n',
                '        max_items: 20\n'
                '        default: ["fiction", "fiction"]\n',
            )
            self.add_pack(
                layout.config_root,
                "books",
                duplicated,
                template="# Book notes\n",
            )
            with self.assertRaisesRegex(
                SchemaPackError,
                "array default values must be unique",
            ):
                SchemaRegistry.load(layout)

    # `urlsplit` raises ValueError for an unterminated IPv6 literal, and the
    # url validators called it unguarded. A pack manifest and an entry
    # attribute are both untrusted enough that this surfaced as a traceback
    # instead of a rejection.
    def test_malformed_url_default_is_reported_as_a_pack_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            malformed = BOOKS_MANIFEST.replace(
                '      source_url:\n        type: "url"\n',
                '      source_url:\n'
                '        type: "url"\n'
                '        default: "http://[::1"\n',
            )
            self.assertNotEqual(malformed, BOOKS_MANIFEST)
            self.add_pack(
                layout.config_root,
                "books",
                malformed,
                template="# Book notes\n",
            )
            with self.assertRaisesRegex(
                SchemaPackError,
                "default must be an HTTP",
            ):
                SchemaRegistry.load(layout)

    def test_malformed_url_attribute_is_reported_as_a_pack_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self.make_layout(Path(directory))
            self.add_pack(
                layout.config_root,
                "books",
                BOOKS_MANIFEST,
                template="# Book notes\n",
            )
            book = SchemaRegistry.load(layout).require_type("books/book")
            with self.assertRaisesRegex(
                SchemaPackError,
                "must be an HTTP",
            ):
                book.validate_attributes(
                    {"author": "A", "source_url": "http://[::1"},
                    apply_defaults=False,
                )


if __name__ == "__main__":
    unittest.main()
