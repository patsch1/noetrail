"""Unit tests for the schema migration ladder.

The end-to-end path is exercised through ``noetrail migrate`` elsewhere. What
that path cannot reach are the refusals: a migration only runs on entries that
already look plausible, so the branches that reject an entry -- or that catch a
migration function which advanced the version by the wrong amount -- are only
reachable from here.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import tests  # noqa: F401

# `tests` puts `src` on the path, so the package imports have to follow it.
BOOK_LAYOUTS = {
    "bookmark": (3, ("url", "bookmark_kind"), {"bookmark_kind": "unknown"}),
}

from noetrail import migrations  # noqa: E402
from noetrail.migrations import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    get_schema_version,
    migrate_0_to_1,
    migrate_3_to_4,
    migrate_8_to_9,
    migrate_11_to_12,
    migrate_entry,
)


class SchemaVersionTest(unittest.TestCase):
    def test_non_integer_schema_version_is_rejected(self) -> None:
        for value in ("9", 9.0, True, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    get_schema_version({"schema_version": value})

    def test_negative_schema_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            get_schema_version({"schema_version": -1})

    def test_missing_schema_version_counts_as_zero(self) -> None:
        self.assertEqual(get_schema_version({}), 0)


class WithSchemaVersionTest(unittest.TestCase):
    def test_version_is_inserted_after_the_id(self) -> None:
        migrated, _ = migrate_0_to_1({"id": "kn_1", "title": "T"}, "body")
        self.assertEqual(list(migrated), ["id", "schema_version", "title"])

    def test_version_leads_when_the_entry_has_no_id(self) -> None:
        # Frontmatter without `id` is invalid, but the migration runs before
        # validation and must not drop the version it just set.
        migrated, _ = migrate_0_to_1({"title": "T"}, "body")
        self.assertEqual(list(migrated), ["schema_version", "title"])
        self.assertEqual(migrated["schema_version"], 1)


class BookmarkKindMigrationTest(unittest.TestCase):
    def test_kind_follows_the_reading_status(self) -> None:
        migrated, _ = migrate_3_to_4(
            {"id": "kn_1", "type": "bookmark", "reading_status": "unread"},
            "body",
        )
        self.assertEqual(
            list(migrated).index("bookmark_kind"),
            list(migrated).index("reading_status") + 1,
        )

    def test_kind_is_appended_when_no_reading_status_exists(self) -> None:
        migrated, _ = migrate_3_to_4(
            {"id": "kn_1", "type": "bookmark"},
            "body",
        )
        self.assertEqual(migrated["bookmark_kind"], "unknown")
        self.assertEqual(list(migrated)[-1], "bookmark_kind")

    def test_non_bookmarks_are_left_alone(self) -> None:
        migrated, _ = migrate_3_to_4({"id": "kn_1", "type": "note"}, "body")
        self.assertNotIn("bookmark_kind", migrated)


class BuiltinAttributesMigrationTest(unittest.TestCase):
    def test_entry_without_a_string_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "no valid type"):
            migrate_8_to_9({"id": "kn_1", "type": ["bookmark"]}, "b", {})

    def test_custom_entry_needs_a_type_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "no valid type_version"):
            migrate_8_to_9(
                {"id": "kn_1", "type": "books/book", "attributes": {}},
                "b",
                {},
            )

    def test_custom_entry_needs_an_attributes_object(self) -> None:
        with self.assertRaisesRegex(ValueError, "no attributes object"):
            migrate_8_to_9(
                {"id": "kn_1", "type": "books/book", "type_version": 1},
                "b",
                {},
            )

    def test_custom_entry_only_gets_a_version_bump(self) -> None:
        migrated, body = migrate_8_to_9(
            {
                "id": "kn_1",
                "schema_version": 8,
                "type": "books/book",
                "type_version": 2,
                "attributes": {"author": "A"},
            },
            "body",
            {},
        )
        self.assertEqual(migrated["schema_version"], 9)
        self.assertEqual(migrated["attributes"], {"author": "A"})
        self.assertEqual(body, "body")

    def test_unknown_builtin_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "no installed built-in type"):
            migrate_8_to_9({"id": "kn_1", "type": "bookmark"}, "b", {})

    def test_entry_that_already_looks_migrated_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "already contains schema 9"):
            migrate_8_to_9(
                {"id": "kn_1", "type": "bookmark", "attributes": {}},
                "b",
                BOOK_LAYOUTS,
            )

    def test_defaults_fill_fields_the_entry_never_had(self) -> None:
        migrated, _ = migrate_8_to_9(
            {
                "id": "kn_1",
                "schema_version": 8,
                "type": "bookmark",
                "url": "https://example.invalid/",
            },
            "b",
            BOOK_LAYOUTS,
        )
        self.assertEqual(migrated["type_version"], 3)
        self.assertEqual(
            migrated["attributes"],
            {"url": "https://example.invalid/", "bookmark_kind": "unknown"},
        )
        self.assertNotIn("url", migrated)


class MigrateEntryTest(unittest.TestCase):
    def test_alias_migration_only_bumps_version(self) -> None:
        original = {
            "id": "kn_1",
            "schema_version": 11,
            "type": "note",
            "title": "Synthetic",
            "relations": [{"predicate": "related_to", "target": "kn_2"}],
        }
        migrated, body = migrate_11_to_12(original, "Exact body\n")
        self.assertEqual(
            migrated,
            {**original, "schema_version": CURRENT_SCHEMA_VERSION},
        )
        self.assertEqual(body, "Exact body\n")
        self.assertNotIn("aliases", migrated)

    def test_future_schema_version_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "future schema version"):
            migrate_entry({"schema_version": CURRENT_SCHEMA_VERSION + 1}, "b")

    def test_migrating_past_schema_8_requires_the_type_registry(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires the active built-in"):
            migrate_entry(
                {"id": "kn_1", "schema_version": 8, "type": "bookmark"},
                "b",
            )

    # The three guards below exist because a migration that advances the
    # version by the wrong amount would otherwise loop forever or skip a step
    # silently. Reaching them means replacing a migration with a broken one.
    def test_a_migration_that_skips_a_version_is_caught(self) -> None:
        def broken(
            metadata: dict[str, object],
            body: str,
        ) -> tuple[dict[str, object], str]:
            return {**metadata, "schema_version": 5}, body

        with patch.dict(migrations.MIGRATIONS, {0: ("broken", broken)}):
            with self.assertRaisesRegex(ValueError, "produced schema version 5"):
                migrate_entry({"id": "kn_1"}, "b")

    def test_a_missing_migration_step_is_reported(self) -> None:
        with patch.dict(migrations.MIGRATIONS, clear=True):
            with self.assertRaisesRegex(ValueError, "no migration exists"):
                migrate_entry({"id": "kn_1"}, "b")

    def test_an_8_to_9_step_that_lands_elsewhere_is_caught(self) -> None:
        def broken(
            metadata: dict[str, object],
            body: str,
            builtin_types: object,
        ) -> tuple[dict[str, object], str]:
            return {**metadata, "schema_version": 8}, body

        with patch.object(migrations, "migrate_8_to_9", broken):
            with self.assertRaisesRegex(ValueError, "expected 9"):
                migrate_entry(
                    {"id": "kn_1", "schema_version": 8, "type": "bookmark"},
                    "b",
                    builtin_types=BOOK_LAYOUTS,
                )

    def test_a_complete_ladder_records_every_step(self) -> None:
        migrated, _, start, steps = migrate_entry(
            {"id": "kn_1", "type": "bookmark", "url": "https://example.invalid/"},
            "b",
            builtin_types=BOOK_LAYOUTS,
        )
        self.assertEqual(start, 0)
        self.assertEqual(len(steps), CURRENT_SCHEMA_VERSION)
        self.assertEqual(migrated["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertTrue(steps[-1].startswith("11->12:"))


if __name__ == "__main__":
    unittest.main()
