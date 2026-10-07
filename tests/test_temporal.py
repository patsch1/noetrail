"""Valid time on relations: supersession, point-in-time, and contradictions.

The unit classes cover the interval arithmetic and the contradiction rules
directly, because a chain that loops or an interval that overlaps its own
successor cannot be produced through `relate` -- that command refuses both --
and would otherwise only be reachable by hand-editing a vault. The CLI and MCP
classes cover the paths a user and an agent actually take.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from noetrail.migrations import CURRENT_SCHEMA_VERSION, migrate_entry
from noetrail.temporal import (
    find_edge,
    instant,
    relation_holds_at,
    relations_at,
    resolve_as_of,
    supersession_errors,
    temporal_counts,
)
from tests import CLI_COMMAND, MCP_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Same instant, written from two places. Every comparison in `temporal` has to
# treat these as equal; a lexicographic one orders them the wrong way round.
BERLIN_NOON = "2026-03-01T13:00:00+02:00"
LONDON_NOON = "2026-03-01T11:00:00+00:00"


def edge(**fields: object) -> dict[str, object]:
    return {"predicate": "involves", **fields}


class InstantTest(unittest.TestCase):
    def test_two_offsets_naming_one_instant_compare_equal(self) -> None:
        self.assertEqual(instant(BERLIN_NOON), instant(LONDON_NOON))
        # And the string order they would have been compared in is the
        # opposite of the order they actually stand in, which is the reason
        # nothing here compares strings.
        self.assertGreater(BERLIN_NOON, LONDON_NOON)
        self.assertEqual(
            instant("2026-03-01T00:30:00+02:00")
            > instant("2026-02-28T23:30:00+00:00"),
            False,
        )

    def test_unusable_bounds_are_reported_as_absent(self) -> None:
        for value in (None, 17, "not-a-time", "2026-03-01T13:00:00"):
            with self.subTest(value=value):
                self.assertIsNone(instant(value))

    def test_a_relation_with_an_unparseable_bound_still_reads(self) -> None:
        """A read of the whole vault must not abort over one damaged entry."""

        broken = edge(target="kn_x", valid_from="tomorrow")
        self.assertTrue(relation_holds_at(broken, 0.0))

    def test_as_of_defaults_to_now_without_echoing_a_label(self) -> None:
        moment, label = resolve_as_of(None)
        self.assertIsNone(label)
        self.assertGreater(moment, 0.0)
        moment, label = resolve_as_of(LONDON_NOON)
        self.assertEqual(label, LONDON_NOON)
        self.assertEqual(moment, instant(LONDON_NOON))


class IntervalTest(unittest.TestCase):
    def test_a_relation_without_bounds_holds_at_every_instant(self) -> None:
        timeless = edge(target="kn_x")
        for moment in (0.0, instant(LONDON_NOON), 4.0e9):
            assert moment is not None
            self.assertTrue(relation_holds_at(timeless, moment))

    def test_the_interval_is_half_open_at_its_end(self) -> None:
        """Otherwise a replacement and what it replaces both hold at T."""

        replaced = edge(
            target="kn_x", valid_from="2024-01-01T00:00:00+00:00",
            valid_until=LONDON_NOON,
        )
        replacement = edge(target="kn_y", valid_from=BERLIN_NOON)
        moment = instant(LONDON_NOON)
        assert moment is not None
        self.assertFalse(relation_holds_at(replaced, moment))
        self.assertTrue(relation_holds_at(replacement, moment))
        self.assertTrue(relation_holds_at(replaced, moment - 1))
        self.assertFalse(relation_holds_at(replacement, moment - 1))

    def test_splitting_reports_both_sides_and_keeps_the_replaced_one(
        self,
    ) -> None:
        metadata = {
            "relations": [
                edge(
                    target="kn_x",
                    valid_from="2024-01-01T00:00:00+00:00",
                    valid_until=LONDON_NOON,
                    superseded_by="kn_y",
                ),
                edge(target="kn_y", valid_from=BERLIN_NOON),
                "not an object",
            ]
        }
        moment = instant(LONDON_NOON)
        assert moment is not None
        current, not_in_force = relations_at(metadata, moment)
        self.assertEqual([item["target"] for item in current], ["kn_y"])
        self.assertEqual([item["target"] for item in not_in_force], ["kn_x"])

        counts = temporal_counts([metadata], moment)
        self.assertEqual(counts["relation_count"], 1)
        self.assertEqual(counts["recorded_relation_count"], 2)
        self.assertEqual(counts["temporal_relation_count"], 2)
        self.assertEqual(counts["not_in_force_relation_count"], 1)

    def test_an_edge_is_found_by_identity_not_by_equality(self) -> None:
        relations = [edge(target="kn_x", valid_from=LONDON_NOON)]
        self.assertIsNotNone(find_edge(relations, "involves", "kn_x"))
        self.assertIsNone(find_edge(relations, "involves", "kn_y"))
        self.assertIsNone(find_edge(relations, "about", "kn_x"))


class SupersessionContradictionTest(unittest.TestCase):
    def test_a_clean_chain_reports_nothing(self) -> None:
        relations = [
            edge(
                target="kn_x",
                valid_from="2024-01-01T00:00:00+00:00",
                valid_until=LONDON_NOON,
                superseded_by="kn_y",
            ),
            edge(target="kn_y", valid_from=BERLIN_NOON),
        ]
        self.assertEqual(supersession_errors(relations), [])

    def test_overlapping_intervals_for_one_statement_are_reported(self) -> None:
        relations = [
            edge(
                target="kn_x",
                valid_from="2024-01-01T00:00:00+00:00",
                valid_until="2026-06-01T00:00:00+00:00",
                superseded_by="kn_y",
            ),
            edge(target="kn_y", valid_from=BERLIN_NOON),
        ]
        self.assertIn(
            "relation 0 overlaps the successor that replaces it",
            supersession_errors(relations),
        )

    def test_a_replacement_dated_before_the_start_is_reported(self) -> None:
        relations = [
            edge(
                target="kn_x",
                valid_from="2026-06-01T00:00:00+00:00",
                valid_until=LONDON_NOON,
                superseded_by="kn_y",
            ),
            edge(target="kn_y", valid_from=BERLIN_NOON),
        ]
        errors = supersession_errors(relations)
        self.assertIn("relation 0 is superseded from before it began", errors)

    def test_a_cycle_is_reported_once_and_names_its_members(self) -> None:
        relations = [
            edge(
                target="kn_x",
                valid_from="2024-01-01T00:00:00+00:00",
                valid_until=LONDON_NOON,
                superseded_by="kn_y",
            ),
            edge(
                target="kn_y",
                valid_from=BERLIN_NOON,
                valid_until="2027-01-01T00:00:00+00:00",
                superseded_by="kn_x",
            ),
        ]
        cycles = [
            error
            for error in supersession_errors(relations)
            if "cycle" in error
        ]
        self.assertEqual(len(cycles), 1, cycles)
        self.assertIn("involves:kn_x", cycles[0])
        self.assertIn("involves:kn_y", cycles[0])

    def test_the_structural_preconditions_are_reported_too(self) -> None:
        relations = [
            edge(target="kn_x", superseded_by="kn_x"),
            edge(target="kn_y", superseded_by="kn_missing"),
            edge(target="kn_z", superseded_by=17),
            edge(target="kn_a", superseded_by="kn_b"),
            edge(target="kn_b"),
        ]
        errors = supersession_errors(relations)
        self.assertIn("relation 0 supersedes itself", errors)
        self.assertIn(
            "relation 1 names a successor the entry does not assert: "
            "involves:kn_missing",
            errors,
        )
        self.assertIn("relation 2 superseded_by must be an entry ID", errors)
        self.assertIn(
            "relation 3 is superseded but records no valid_until", errors
        )
        self.assertIn(
            "relation 3 is superseded by a relation without valid_from", errors
        )

    def test_a_non_list_relations_field_is_left_to_the_envelope_check(
        self,
    ) -> None:
        self.assertEqual(supersession_errors("relations"), [])


class RelationValidityMigrationTest(unittest.TestCase):
    """10 to 11 has to widen the format and change no entry."""

    def test_a_schema_10_entry_keeps_every_key_value_and_its_body(self) -> None:
        original = {
            "id": "kn_" + "0" * 32,
            "schema_version": 10,
            "type": "person",
            "type_version": 1,
            "attributes": {},
            "title": "Alex",
            "created_at": "2026-01-01T09:00:00+01:00",
            "updated_at": "2026-01-02T09:00:00+01:00",
            "status": "active",
            "sensitivity": "personal",
            "tags": ["colleague"],
            "relations": [{"predicate": "involves", "target": "kn_" + "1" * 32}],
            "provenance": {"title": "user"},
            "origin": "conversation",
        }
        body = "    indented code\n\nAlex works somewhere."
        migrated, migrated_body, start, steps = migrate_entry(
            json.loads(json.dumps(original)), body
        )
        self.assertEqual(start, 10)
        self.assertEqual(
            steps,
            [
                "10->11:add-relation-validity",
                "11->12:add-entry-aliases",
            ],
        )
        self.assertEqual(migrated_body, body)
        self.assertEqual(migrated["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            {key: value for key, value in migrated.items() if key != "schema_version"},
            {key: value for key, value in original.items() if key != "schema_version"},
        )
        # Key order is part of the on-disk format, and the only difference is
        # the version's own value.
        self.assertEqual(list(migrated), list(original))

    def test_no_interval_is_invented_from_the_record_timestamps(self) -> None:
        """`created_at` says when the vault learned it, not since when it held."""

        migrated, _, _, _ = migrate_entry(
            {
                "id": "kn_" + "0" * 32,
                "schema_version": 10,
                "created_at": "2026-01-01T09:00:00+01:00",
                "relations": [
                    {"predicate": "involves", "target": "kn_" + "1" * 32}
                ],
            },
            "",
        )
        self.assertEqual(
            migrated["relations"],
            [{"predicate": "involves", "target": "kn_" + "1" * 32}],
        )

    def test_a_second_run_changes_nothing(self) -> None:
        first, body, _, _ = migrate_entry(
            {"id": "kn_" + "0" * 32, "schema_version": 10}, "b"
        )
        second, second_body, start, steps = migrate_entry(first, body)
        self.assertEqual(steps, [])
        self.assertEqual(start, CURRENT_SCHEMA_VERSION)
        self.assertEqual(second, first)
        self.assertEqual(second_body, body)


class TemporalCliTest(unittest.TestCase):
    """The user-facing path: record a change of employer and query around it."""

    started = "2024-01-08T09:00:00+01:00"
    # Deliberately the *same instant* written with two different offsets: the
    # replacement is recorded from Berlin, the query is asked from London.
    changed_berlin = BERLIN_NOON
    changed_london = LONDON_NOON

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.data.mkdir()
        self.config.mkdir()
        (self.config / "packs").mkdir()
        shutil.copytree(
            REPOSITORY_ROOT / ".knowledge",
            self.base / "builtins",
        )
        self.assertEqual(self.cli("init").returncode, 0)
        self.person = self.capture("person", "Alex")
        self.old = self.capture("project", "Orbit Works")
        self.new = self.capture("project", "Harbor Labs")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *CLI_COMMAND,
                "--data-root",
                str(self.data),
                "--config-root",
                str(self.config),
                "--builtins-root",
                str(self.base / "builtins"),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def json_cli(self, *arguments: str) -> dict[str, object]:
        result = self.cli(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def capture(self, entry_type: str, title: str) -> str:
        created = self.json_cli(
            "capture", "--type", entry_type, "--title", title, "--text", title
        )
        return str(created["id"])

    def entry_path(self) -> Path:
        return next((self.data / "vault" / "entities" / "people").glob("*.md"))

    def relations_of(self, entry_id: str) -> list[dict[str, object]]:
        lines = self.entry_path().read_text(encoding="utf-8").splitlines()
        for line in lines[1 : lines.index("---", 1)]:
            key, _, raw = line.partition(":")
            if key.strip() == "relations":
                parsed = json.loads(raw.strip())
                assert isinstance(parsed, list)
                return parsed
        raise AssertionError(f"no relations recorded for {entry_id}")

    def record_change_of_employer(self) -> None:
        self.json_cli(
            "relate",
            self.person,
            "involves",
            self.old,
            "--valid-from",
            self.started,
        )
        self.json_cli(
            "relate",
            self.person,
            "involves",
            self.new,
            "--supersedes",
            self.old,
            "--valid-from",
            self.changed_berlin,
        )

    def test_the_replaced_statement_stays_in_the_file(self) -> None:
        self.record_change_of_employer()
        relations = self.relations_of(self.person)
        self.assertEqual(len(relations), 2)
        replaced = find_edge(relations, "involves", self.old)
        assert replaced is not None
        self.assertEqual(replaced["valid_from"], self.started)
        self.assertEqual(replaced["valid_until"], self.changed_berlin)
        self.assertEqual(replaced["superseded_by"], self.new)
        self.assertIn("recorded_at", replaced)
        self.assertEqual(self.cli("validate").returncode, 0)

    def test_point_in_time_search_crosses_the_change(self) -> None:
        self.record_change_of_employer()

        current = self.json_cli("search", "", "--related-id", self.old)
        self.assertEqual(current["total"], 0)
        self.assertNotIn("as_of", current)

        # The query is asked in the London offset for the same instant the
        # Berlin-offset record used, one second before and one second after.
        before = self.json_cli(
            "search",
            "",
            "--related-id",
            self.old,
            "--as-of",
            "2026-03-01T10:59:59+00:00",
        )
        self.assertEqual(before["total"], 1)
        item = before["items"][0]  # type: ignore[index]
        self.assertEqual(item["id"], self.person)
        self.assertEqual(item["relation_count"], 1)
        self.assertEqual(item["not_in_force_relation_count"], 1)
        self.assertEqual(before["as_of"], "2026-03-01T10:59:59+00:00")

        at_the_change = self.json_cli(
            "search", "", "--related-id", self.old, "--as-of", self.changed_london
        )
        self.assertEqual(at_the_change["total"], 0)
        successor = self.json_cli(
            "search", "", "--related-id", self.new, "--as-of", self.changed_london
        )
        self.assertEqual(successor["total"], 1)

    def test_point_in_time_search_agrees_under_bm25_and_the_index(self) -> None:
        self.record_change_of_employer()
        self.assertEqual(self.cli("index", "rebuild").returncode, 0)
        scanned = self.json_cli(
            "search",
            "Alex",
            "--rank",
            "bm25",
            "--related-id",
            self.old,
            "--as-of",
            "2025-06-01T00:00:00+00:00",
        )
        self.assertEqual(scanned["total"], 1)
        # The index holds no relation data at all, so a point-in-time query
        # cannot make it stale; it stays fresh across the supersession write.
        status = self.json_cli("index", "status")
        self.assertEqual(status["state"], "fresh")

    def test_relations_and_get_entry_split_the_two_states(self) -> None:
        self.record_change_of_employer()
        now = self.json_cli("relations", self.person)
        self.assertEqual(
            [item["target"] for item in now["outgoing"]],  # type: ignore[index]
            [self.new],
        )
        self.assertEqual(
            [
                item["target"]
                for item in now["outgoing_not_in_force"]  # type: ignore[index]
            ],
            [self.old],
        )
        earlier = self.json_cli(
            "relations", self.person, "--as-of", "2025-06-01T00:00:00+00:00"
        )
        self.assertEqual(
            [item["target"] for item in earlier["outgoing"]],  # type: ignore[index]
            [self.old],
        )
        incoming = self.json_cli("relations", self.old)
        self.assertEqual(incoming["incoming"], [])
        self.assertEqual(
            len(incoming["incoming_not_in_force"]),  # type: ignore[arg-type]
            1,
        )

        entry = self.json_cli("review", self.person)
        self.assertEqual(
            [item["target"] for item in entry["relations"]],  # type: ignore[index]
            [self.new],
        )
        self.assertEqual(
            [
                item["target"]
                for item in entry["relations_not_in_force"]  # type: ignore[index]
            ],
            [self.old],
        )

    def test_inventory_counts_what_holds_at_the_queried_instant(self) -> None:
        self.record_change_of_employer()
        now = self.json_cli("inventory")
        self.assertEqual(now["relation_count"], 1)
        self.assertEqual(now["recorded_relation_count"], 2)
        self.assertEqual(now["temporal_relation_count"], 2)
        self.assertEqual(now["not_in_force_relation_count"], 1)

    def test_the_queue_refuses_an_instant_it_cannot_honour(self) -> None:
        refused = self.cli("review", "--as-of", self.changed_london)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("requires an entry ID", refused.stderr)

    def test_validate_reports_a_hand_written_contradiction(self) -> None:
        self.record_change_of_employer()
        path = self.entry_path()
        text = path.read_text(encoding="utf-8")
        # Extend the replaced statement past the instant its replacement
        # begins. Both relations still parse and both are structurally valid;
        # what is wrong is the claim they make together.
        path.write_text(
            text.replace(
                f'"valid_until":"{self.changed_berlin}"',
                '"valid_until":"2026-09-01T00:00:00+02:00"',
            ),
            encoding="utf-8",
        )
        failed = self.cli("validate")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("overlaps the successor that replaces it", failed.stdout)

        diagnosed = self.cli("doctor")
        self.assertEqual(diagnosed.returncode, 1)
        checks = {
            str(check["name"]): check
            for check in json.loads(diagnosed.stdout)["checks"]
        }
        self.assertEqual(checks["relation_validity"]["status"], "error")
        self.assertIn("noetrail validate", checks["relation_validity"]["detail"])

    def test_a_healthy_vault_reports_its_temporal_relations(self) -> None:
        self.record_change_of_employer()
        report = self.json_cli("doctor")
        checks = {
            str(check["name"]): check
            for check in report["checks"]  # type: ignore[union-attr]
        }
        self.assertEqual(checks["relation_validity"]["status"], "ok")
        self.assertIn("1 entries", checks["relation_validity"]["detail"])

    def test_the_write_path_refuses_the_contradictions_it_can_see(self) -> None:
        self.json_cli(
            "relate", self.person, "involves", self.old, "--valid-from", self.started
        )
        earlier = self.cli(
            "relate",
            self.person,
            "involves",
            self.new,
            "--supersedes",
            self.old,
            "--valid-from",
            "2023-01-01T00:00:00+01:00",
        )
        self.assertEqual(earlier.returncode, 1)
        self.assertIn("precedes the start", earlier.stderr)

        without_instant = self.cli(
            "relate",
            self.person,
            "involves",
            self.new,
            "--supersedes",
            self.old,
        )
        self.assertEqual(without_instant.returncode, 1)
        self.assertIn("requires --valid-from", without_instant.stderr)

        itself = self.cli(
            "relate",
            self.person,
            "involves",
            self.old,
            "--supersedes",
            self.old,
            "--valid-from",
            self.changed_berlin,
        )
        self.assertEqual(itself.returncode, 1)

        unknown = self.cli(
            "relate",
            self.person,
            "involves",
            self.new,
            "--supersedes",
            self.person,
            "--valid-from",
            self.changed_berlin,
        )
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("no involves relation to supersede", unknown.stderr)

        on_removal = self.cli(
            "relate",
            self.person,
            "involves",
            self.old,
            "--remove",
            "--valid-from",
            self.started,
        )
        self.assertEqual(on_removal.returncode, 1)
        self.assertIn("only apply when adding", on_removal.stderr)

    def test_a_second_supersession_of_the_same_statement_is_refused(
        self,
    ) -> None:
        self.record_change_of_employer()
        third = self.capture("project", "Beacon Studio")
        again = self.cli(
            "relate",
            self.person,
            "involves",
            third,
            "--supersedes",
            self.old,
            "--valid-from",
            "2026-07-01T00:00:00+02:00",
        )
        self.assertEqual(again.returncode, 1)
        self.assertIn("already been superseded", again.stderr)

        # Superseding the current statement instead extends the chain.
        chained = self.json_cli(
            "relate",
            self.person,
            "involves",
            third,
            "--supersedes",
            self.new,
            "--valid-from",
            "2026-07-01T00:00:00+02:00",
        )
        self.assertEqual(chained["supersedes"], self.new)
        self.assertEqual(self.cli("validate").returncode, 0)

    def test_removing_a_successor_unlinks_it_instead_of_dangling(self) -> None:
        self.record_change_of_employer()
        removed = self.json_cli(
            "relate", self.person, "involves", self.new, "--remove"
        )
        self.assertEqual(removed["unlinked_supersessions"], [self.old])
        replaced = find_edge(self.relations_of(self.person), "involves", self.old)
        assert replaced is not None
        # The interval survives: the statement did stop being asserted then.
        self.assertEqual(replaced["valid_until"], self.changed_berlin)
        self.assertNotIn("superseded_by", replaced)
        self.assertEqual(self.cli("validate").returncode, 0)

    def test_an_interval_may_be_recorded_without_any_supersession(self) -> None:
        created = self.json_cli(
            "relate",
            self.person,
            "involves",
            self.old,
            "--valid-from",
            self.started,
            "--valid-until",
            self.changed_berlin,
        )
        self.assertEqual(created["action"], "added")
        self.assertEqual(self.cli("validate").returncode, 0)
        self.assertEqual(self.json_cli("inventory")["relation_count"], 0)

        backwards = self.cli(
            "relate",
            self.person,
            "involves",
            self.new,
            "--valid-from",
            self.changed_berlin,
            "--valid-until",
            self.started,
        )
        self.assertEqual(backwards.returncode, 1)
        self.assertIn("later than", backwards.stderr)

    def test_a_naive_instant_is_refused_rather_than_assumed_local(self) -> None:
        naive = self.cli(
            "relate",
            self.person,
            "involves",
            self.old,
            "--valid-from",
            "2026-03-01T13:00:00",
        )
        self.assertEqual(naive.returncode, 1)
        self.assertIn("timezone", naive.stderr)

        queried = self.cli("search", "", "--as-of", "2026-03-01")
        self.assertEqual(queried.returncode, 1)

    def test_an_ordinary_relation_stays_exactly_as_it_was(self) -> None:
        """The format widened; entries that state no interval must not change."""

        self.json_cli("relate", self.person, "involves", self.old)
        self.assertEqual(
            self.relations_of(self.person),
            [{"predicate": "involves", "target": self.old}],
        )
        listed = self.json_cli("search", "", "--related-id", self.old)
        self.assertEqual(listed["total"], 1)
        self.assertNotIn(
            "not_in_force_relation_count",
            listed["items"][0],  # type: ignore[operator]
        )


class TemporalMcpTest(unittest.TestCase):
    """One agent round trip: record the replacement, then ask what held."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temporary)
        (self.root / ".knowledge").mkdir()
        (self.root / ".knowledge" / "SPEC.md").write_text("test", encoding="utf-8")
        (self.root / ".knowledge" / "relation-types.yaml").write_text(
            "relations:\n  involves:\n    inverse: experienced_in\n",
            encoding="utf-8",
        )
        shutil.copytree(
            REPOSITORY_ROOT / ".knowledge" / "packs" / "knowledge-core",
            self.root / ".knowledge" / "packs" / "knowledge-core",
        )
        self.process = subprocess.Popen(  # noqa: S603 - fixed argument vector
            [*MCP_COMMAND, "--root", str(self.root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.request_id = 0

    def tearDown(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - server hung
            self.process.terminate()
            self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        self.temporary.cleanup()

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.request_id += 1
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
            + "\n"
        )
        self.process.stdin.flush()
        response = json.loads(self.process.stdout.readline())
        return response["result"]

    def ok(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        result = self.call(name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]  # type: ignore[return-value]

    def capture(self, entry_type: str, title: str) -> dict[str, object]:
        return self.ok(
            "capture", {"type": entry_type, "title": title, "text": title}
        )

    def test_the_new_capability_needs_no_new_tool(self) -> None:
        self.request_id += 1
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "method": "tools/list",
                    "params": {},
                }
            )
            + "\n"
        )
        self.process.stdin.flush()
        tools = json.loads(self.process.stdout.readline())["result"]["tools"]
        by_name = {item["name"]: item for item in tools}
        for name in ("search", "get_entry", "relations", "inventory"):
            self.assertIn("as_of", by_name[name]["inputSchema"]["properties"], name)
        relation_schema = by_name["set_relation"]["inputSchema"]
        for name in ("valid_from", "valid_until", "supersedes"):
            self.assertIn(name, relation_schema["properties"])
        # Still required: recording a replacement is a mutation like any other.
        self.assertIn("expected_revision", relation_schema["required"])
        self.assertNotIn("supersede", set(by_name) - {"set_relation"})

    def test_an_agent_records_a_replacement_and_queries_across_it(self) -> None:
        person = self.capture("person", "Alex")
        old = self.capture("project", "Orbit Works")
        new = self.capture("project", "Harbor Labs")

        first = self.ok(
            "set_relation",
            {
                "id": str(person["id"]),
                "expected_revision": str(person["revision"]),
                "predicate": "involves",
                "target_id": str(old["id"]),
                "action": "add",
                "valid_from": "2024-01-08T09:00:00+01:00",
            },
        )
        replacement = self.ok(
            "set_relation",
            {
                "id": str(person["id"]),
                "expected_revision": str(first["revision"]),
                "predicate": "involves",
                "target_id": str(new["id"]),
                "action": "add",
                "supersedes": str(old["id"]),
                "valid_from": "2026-03-01T13:00:00+02:00",
            },
        )
        self.assertEqual(replacement["supersedes"], old["id"])

        now = self.ok("search", {"query": "", "related_id": str(old["id"])})
        self.assertEqual(now["total"], 0)
        earlier = self.ok(
            "search",
            {
                "query": "",
                "related_id": str(old["id"]),
                "as_of": "2026-03-01T10:59:59+00:00",
            },
        )
        self.assertEqual(earlier["total"], 1)

        entry = self.ok(
            "get_entry",
            {"id": str(person["id"]), "as_of": "2025-06-01T00:00:00+00:00"},
        )
        self.assertEqual(
            [item["target"] for item in entry["relations"]],  # type: ignore[index]
            [old["id"]],
        )
        graph = self.ok("relations", {"id": str(person["id"])})
        self.assertEqual(
            [item["target"] for item in graph["outgoing"]],  # type: ignore[index]
            [new["id"]],
        )
        self.assertEqual(
            [
                item["target"]
                for item in graph["outgoing_not_in_force"]  # type: ignore[index]
            ],
            [old["id"]],
        )
        self.assertEqual(
            self.ok("inventory", {})["not_in_force_relation_count"], 1
        )
        self.assertFalse(self.call("validate", {})["isError"])

    def test_a_stale_revision_still_blocks_a_replacement(self) -> None:
        person = self.capture("person", "Alex")
        old = self.capture("project", "Orbit Works")
        new = self.capture("project", "Harbor Labs")
        self.ok(
            "set_relation",
            {
                "id": str(person["id"]),
                "expected_revision": str(person["revision"]),
                "predicate": "involves",
                "target_id": str(old["id"]),
                "action": "add",
                "valid_from": "2024-01-08T09:00:00+01:00",
            },
        )
        stale = self.call(
            "set_relation",
            {
                "id": str(person["id"]),
                # The revision from before the first relation was added.
                "expected_revision": str(person["revision"]),
                "predicate": "involves",
                "target_id": str(new["id"]),
                "action": "add",
                "supersedes": str(old["id"]),
                "valid_from": "2026-03-01T13:00:00+02:00",
            },
        )
        self.assertTrue(stale["isError"])
        self.assertEqual(
            stale["structuredContent"]["error"]["code"],  # type: ignore[index]
            "revision_conflict",
        )

    def test_an_instant_that_looks_like_a_flag_is_rejected_as_a_timestamp(
        self,
    ) -> None:
        person = self.capture("person", "Alex")
        other = self.capture("project", "Orbit Works")
        smuggled = self.call(
            "set_relation",
            {
                "id": str(person["id"]),
                "expected_revision": str(person["revision"]),
                "predicate": "involves",
                "target_id": str(other["id"]),
                "action": "add",
                "valid_from": "--remove",
            },
        )
        self.assertTrue(smuggled["isError"])
        self.assertIn(
            "invalid format",
            json.dumps(smuggled["structuredContent"]),
        )


if __name__ == "__main__":
    unittest.main()
