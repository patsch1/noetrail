from __future__ import annotations

import json
from pathlib import Path
import unittest

import tests  # noqa: F401

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

from noetrail.constants import (  # noqa: E402
    BOOKMARK_KINDS,
    ENTRY_TYPES,
    EXPERIENCE_KINDS,
    FETCH_STATUSES,
    INTEREST_STATUSES,
    PLACE_KINDS,
    PRECISIONS,
    PRODUCT_KINDS,
    READING_STATUSES,
)
from noetrail.layout import resolve_layout  # noqa: E402
from noetrail.migrations import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    ORIGINS,
)
from noetrail.schema import SchemaRegistry  # noqa: E402
from noetrail.validation import validate_metadata  # noqa: E402


class BuiltinSchemaRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        layout = resolve_layout(
            root=REPOSITORY_ROOT,
            application_root=REPOSITORY_ROOT,
        )
        cls.registry = SchemaRegistry.load(layout)

    def values(self, type_id: str, field: str) -> set[str]:
        return set(
            self.registry.require_type(type_id).fields[field].values
        )

    def test_every_productive_short_type_has_one_registry_definition(self) -> None:
        self.assertEqual(set(self.registry.builtin_types), ENTRY_TYPES)
        self.assertFalse(
            set(self.registry.builtin_types) & set(self.registry.custom_types)
        )

    def test_registry_enums_match_specialized_workflow_constants(self) -> None:
        self.assertEqual(self.values("memory", "occurred_precision"), PRECISIONS)
        self.assertEqual(self.values("place", "place_kind"), PLACE_KINDS)
        self.assertEqual(self.values("product", "product_kind"), PRODUCT_KINDS)
        self.assertEqual(
            self.values("experience", "experience_kind"),
            EXPERIENCE_KINDS,
        )
        self.assertEqual(
            self.values("experience", "occurred_precision"),
            PRECISIONS,
        )
        for type_id in ("place", "product", "recipe"):
            self.assertEqual(
                self.values(type_id, "interest_status"),
                INTEREST_STATUSES,
            )
        self.assertEqual(
            self.values("bookmark", "reading_status"),
            READING_STATUSES,
        )
        self.assertEqual(
            self.values("bookmark", "bookmark_kind"),
            BOOKMARK_KINDS,
        )
        self.assertEqual(
            self.values("bookmark", "fetch_status"),
            FETCH_STATUSES,
        )

    def field(self, type_id: str, name: str):
        return self.registry.require_type(type_id).fields[name]

    def test_registry_numeric_bounds_match_specialized_workflow_rules(self) -> None:
        for type_id in ("place", "product", "experience"):
            rating = self.field(type_id, "rating")
            self.assertEqual((rating.minimum, rating.maximum), (1, 5))
        for field_name in ("prep_minutes", "cook_minutes"):
            field = self.field("recipe", field_name)
            self.assertEqual((field.minimum, field.maximum), (0, 10080))

    def test_required_flags_carry_the_whole_missing_field_rule(self) -> None:
        """What `missing bookmark fields: ...` used to be, in the pack.

        The parity test compared enum *values* only, so a field could lose its
        required flag -- or gain one -- without anything failing, as long as
        the list of accepted strings still matched. Since `validate_metadata`
        reads these flags instead of restating them, the flag is now the rule
        and has to be pinned like one.
        """

        expected = {
            "thought": set(),
            "memory": set(),
            "note": set(),
            "person": set(),
            "project": set(),
            "media": set(),
            "source": set(),
            "place": {"place_kind", "interest_status"},
            "product": {"product_kind", "interest_status"},
            "recipe": {"interest_status"},
            "experience": {
                "experience_kind",
                "occurred_at",
                "occurred_precision",
            },
            "bookmark": {
                "url",
                "canonical_url",
                "domain",
                "retrieved_at",
                "reading_status",
                "bookmark_kind",
                "fetch_status",
            },
        }
        self.assertEqual(set(expected), ENTRY_TYPES)
        for type_id, names in expected.items():
            definition = self.registry.require_type(type_id)
            self.assertEqual(
                {
                    name
                    for name, field in definition.fields.items()
                    if field.required
                },
                names,
                type_id,
            )

    def test_string_length_limits_are_declared_for_every_free_text_field(
        self,
    ) -> None:
        expected = {
            ("memory", "occurred_at"): (None, 100),
            ("recipe", "source_domain"): (None, 255),
            ("recipe", "source_site_name"): (1, 500),
            ("recipe", "source_description"): (1, 10_000),
            ("recipe", "servings"): (1, 200),
            ("bookmark", "domain"): (None, 255),
            ("bookmark", "site_name"): (None, 500),
            ("bookmark", "published_at"): (None, 100),
            ("bookmark", "language"): (None, 50),
            ("bookmark", "page_description"): (None, 10_000),
        }
        for (type_id, name), bounds in expected.items():
            field = self.field(type_id, name)
            self.assertEqual((field.min_length, field.max_length), bounds, name)
        self.assertEqual(self.field("bookmark", "authors").max_items, 32)
        # Nothing that stores free text may be unbounded: a 40 MB `site_name`
        # off a fetched page is a denial of service against every reader.
        for definition in self.registry.builtin_types.values():
            for field in definition.fields.values():
                if field.kind in {"string", "text"}:
                    self.assertIsNotNone(
                        field.max_length,
                        f"{definition.local_id}.{field.name}",
                    )

    def test_cross_conditions_live_in_the_pack_not_in_python(self) -> None:
        """The rules `validate_metadata` used to hard-code, as declarations."""

        self.assertEqual(
            self.field("memory", "occurred_precision").requires,
            (("occurred_at", None),),
        )
        for type_id in ("place", "product"):
            self.assertEqual(
                self.field(type_id, "rating").requires,
                (("last_experienced_at", None),),
            )
        self.assertEqual(
            self.field("bookmark", "read_at").requires,
            (("reading_status", "read"),),
        )
        self.assertEqual(
            self.field("recipe", "source_url").requires,
            (("source_domain", None),),
        )
        self.assertEqual(
            self.field("recipe", "source_domain").requires,
            (("source_url", None),),
        )
        # Derived values: the domain is the URL's host, and the canonical URL
        # is stored in the form `canonicalize_url` produces.
        self.assertEqual(self.field("bookmark", "domain").host_of, "canonical_url")
        self.assertEqual(self.field("recipe", "source_domain").host_of, "source_url")
        self.assertTrue(self.field("bookmark", "canonical_url").normalized)
        self.assertTrue(self.field("recipe", "source_url").normalized)
        self.assertFalse(self.field("bookmark", "url").normalized)

    def test_one_bad_attribute_does_not_report_the_valid_ones_as_missing(
        self,
    ) -> None:
        """Bug: a single invalid enum made `validate` invent seven more errors.

        The type-specific ladder ran against `{**metadata, **validated}`, and
        `validated` was set to `{}` whenever `validate_attributes` raised. One
        bad `reading_status` therefore left every other domain field invisible
        to the ladder, which then reported `missing bookmark fields:
        bookmark_kind, canonical_url, domain, fetch_status, reading_status,
        retrieved_at, url` for an entry that stored all seven -- plus `url must
        be a string` and `domain must be a string` about strings that were
        there. The one real defect was buried in eight lines of fiction.
        """

        metadata = {
            "id": "kn_" + "0" * 32,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "type": "bookmark",
            "type_version": 1,
            "title": "Titel",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "status": "active",
            "sensitivity": "normal",
            "tags": [],
            "relations": [],
            "attributes": {
                "url": "https://example.invalid/a",
                "canonical_url": "https://example.invalid/a",
                "domain": "example.invalid",
                "retrieved_at": "2026-01-01T00:00:00+00:00",
                "reading_status": "bogus",
                "bookmark_kind": "unknown",
                "fetch_status": "not_attempted",
            },
        }
        path = Path("vault/bookmarks/x.md")
        reported = validate_metadata(
            path, metadata, set(), self.registry, in_trash=False
        )
        self.assertEqual(len(reported), 1, reported)
        self.assertIn("reading_status must be one of", reported[0])
        for absent in ("missing bookmark fields", "must be a string"):
            self.assertNotIn(absent, " ".join(reported))

    def test_a_declared_cross_condition_is_actually_enforced(self) -> None:
        """A declaration nothing reads would pass every test above."""

        base = {
            "id": "kn_" + "0" * 32,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "type": "bookmark",
            "type_version": 1,
            "title": "Titel",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "status": "active",
            "sensitivity": "normal",
            "tags": [],
            "relations": [],
            "attributes": {
                "url": "https://example.invalid/a",
                "canonical_url": "https://example.invalid/a",
                "domain": "example.invalid",
                "retrieved_at": "2026-01-01T00:00:00+00:00",
                "reading_status": "unread",
                "bookmark_kind": "unknown",
                "fetch_status": "not_attempted",
            },
        }
        path = Path("vault/bookmarks/x.md")
        self.assertEqual(
            validate_metadata(path, base, set(), self.registry, in_trash=False),
            [],
        )

        unread_but_read = json.loads(json.dumps(base))
        unread_but_read["attributes"]["read_at"] = "2026-01-02T00:00:00+00:00"
        self.assertIn(
            f"{path}: read_at requires reading_status read",
            validate_metadata(
                path, unread_but_read, set(), self.registry, in_trash=False
            ),
        )

        wrong_host = json.loads(json.dumps(base))
        wrong_host["attributes"]["domain"] = "other.invalid"
        self.assertIn(
            f"{path}: domain does not match canonical_url",
            validate_metadata(
                path, wrong_host, set(), self.registry, in_trash=False
            ),
        )

        unnormalized = json.loads(json.dumps(base))
        unnormalized["attributes"]["canonical_url"] = (
            "https://example.invalid/a?utm_source=newsletter"
        )
        self.assertIn(
            f"{path}: canonical_url is not normalized",
            validate_metadata(
                path, unnormalized, set(), self.registry, in_trash=False
            ),
        )

        stray = json.loads(json.dumps(base))
        stray["place_kind"] = "bar"
        self.assertIn(
            f"{path}: entry contains fields bookmark does not declare: place_kind",
            validate_metadata(path, stray, set(), self.registry, in_trash=False),
        )

    def test_current_envelope_and_builtin_templates_use_attributes(self) -> None:
        envelope = json.loads(
            (
                REPOSITORY_ROOT
                / ".knowledge"
                / "schemas"
                / "entry.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            envelope["properties"]["schema_version"]["const"],
            CURRENT_SCHEMA_VERSION,
        )
        self.assertTrue(
            {"type_version", "attributes"} <= set(envelope["required"])
        )
        # Optional on purpose: an entry that records nothing about where its
        # values came from is normal, not invalid.
        self.assertNotIn("provenance", envelope["required"])
        self.assertEqual(
            envelope["properties"]["provenance"]["additionalProperties"][
                "enum"
            ],
            list(ORIGINS),
        )

        for path in sorted(
            (REPOSITORY_ROOT / ".knowledge" / "templates").glob("*.md")
        ):
            lines = path.read_text(encoding="utf-8").splitlines()
            end = lines.index("---", 1)
            metadata = {
                key: json.loads(raw_value.strip())
                for key, raw_value in (
                    line.split(":", 1) for line in lines[1:end]
                )
            }
            self.assertEqual(
                metadata["schema_version"], CURRENT_SCHEMA_VERSION, path.name
            )
            self.assertEqual(metadata["type_version"], 1, path.name)
            self.assertIsInstance(metadata["attributes"], dict, path.name)


if __name__ == "__main__":
    unittest.main()
