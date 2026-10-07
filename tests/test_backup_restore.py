"""Restore a separated instance with private packs, views, edges and blobs."""

import json
from pathlib import Path
import shutil
import unittest

from tests.test_search_index import VaultHarness


class BackupRestoreTest(VaultHarness, unittest.TestCase):
    def test_restored_instance_works_without_original_data_or_configuration(self):
        pack = Path(__file__).resolve().parents[1] / "demo/config/packs/travel"
        shutil.copytree(pack, self.config / "packs/travel")
        (self.config / "views.yaml").write_text(
            "format_version: 1\nviews:\n  destinations:\n"
            '    title: "Destinations"\n    type: "travel/destination"\n',
            encoding="utf-8",
        )
        attributes = self.base / "attributes.json"
        attributes.write_text(json.dumps({"country": "Exampleland"}), encoding="utf-8")
        created = self.json_cli(
            "capture",
            "--type",
            "travel/destination",
            "--title",
            "Synthetic island",
            "--text",
            "A synthetic destination.",
            "--attributes-file",
            str(attributes),
        )
        identifier = created["id"]
        note = self.capture("Travel plan", "A synthetic plan.")
        self.json_cli("relate", note, "related_to", identifier)
        inbox = self.base / "inbox"
        inbox.mkdir()
        photo = inbox / "synthetic.png"
        photo.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-backup-fixture")
        self.json_cli(
            "--attachment-inbox",
            str(inbox),
            "attach",
            identifier,
            photo.name,
            "--expected-revision",
            self.json_cli("review", identifier)["revision"],
        )
        original = self.json_cli("review", identifier)
        self.json_cli("index", "rebuild")
        backup_data, backup_config = (
            self.base / "restored-data",
            self.base / "restored-config",
        )
        shutil.copytree(self.data, backup_data, ignore=shutil.ignore_patterns(".locks"))
        shutil.copytree(
            self.config, backup_config, ignore=shutil.ignore_patterns("index")
        )
        shutil.rmtree(self.data)
        shutil.rmtree(self.config)
        shutil.rmtree(inbox)
        self.data, self.config = backup_data, backup_config
        self.assertEqual(self.run_cli("validate"), 0, self.stdout.getvalue())
        restored = self.json_cli("review", identifier)
        self.assertEqual(restored, original)
        self.assertEqual(self.json_cli("view", "run", "destinations")["total"], 1)
        related = self.search("--related-id", identifier, "")
        self.assertEqual([item["id"] for item in related["items"]], [note])
        self.assertEqual(
            next((self.data / "vault/attachments").rglob("*.png")).read_bytes(),
            b"\x89PNG\r\n\x1a\nsynthetic-backup-fixture",
        )
