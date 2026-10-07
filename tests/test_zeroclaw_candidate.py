"""The rollout pointer must identify a commit, never a moving ref."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from tools.zeroclaw_candidate import render


class CandidateTests(unittest.TestCase):
    def test_candidate_is_exact_and_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            revision = "a" * 40
            render(revision, root)
            resource = json.loads((root / "revision.yaml").read_text())
            self.assertEqual(resource["data"], {"revision": revision})
            self.assertEqual(resource["kind"], "ConfigMap")
            self.assertEqual(resource["metadata"]["namespace"], "zeroclaw")
            render(revision, root)
            self.assertEqual(resource, json.loads((root / "revision.yaml").read_text()))

    def test_rejects_mutable_and_malformed_refs_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            for revision in ("main", "a" * 39, "A" * 40, "../main", "a" * 40 + "\n"):
                with self.subTest(revision=revision), self.assertRaises(ValueError):
                    render(revision, root)
            self.assertFalse(root.exists())


class CandidatePublishingTests(unittest.TestCase):
    def test_workflow_bootstraps_branch_from_detached_head(self):
        source = Path(__file__).resolve().parents[1]
        git_binary = shutil.which("git")
        shell_binary = shutil.which("bash")
        self.assertIsNotNone(git_binary)
        self.assertIsNotNone(shell_binary)
        workflow = (source / ".github/workflows/deploy-zeroclaw.yml").read_text()
        script = textwrap.dedent(workflow.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, checkout = root / "remote.git", root / "checkout"
            checkout.mkdir()

            def git(*args):
                return subprocess.run(
                    [git_binary, *args],
                    cwd=checkout,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            git("init", "--bare", str(remote))
            git("init", "-b", "main")
            git("config", "user.name", "Synthetic CI")
            git("config", "user.email", "ci@example.invalid")
            (checkout / "tools").mkdir()
            shutil.copyfile(
                source / "tools/zeroclaw_candidate.py",
                checkout / "tools/zeroclaw_candidate.py",
            )
            git("add", ".")
            git("commit", "-m", "Create synthetic source")
            revision = git("rev-parse", "HEAD")
            git("remote", "add", "origin", str(remote))
            git("push", "origin", "main")
            git("checkout", "--detach", revision)
            subprocess.run(
                [shell_binary, "-c", script],
                cwd=checkout,
                env={**os.environ, "CANDIDATE_SHA": revision},
                check=True,
                capture_output=True,
                text=True,
            )
            candidate = json.loads(
                git(
                    "--git-dir=" + str(remote),
                    "show",
                    "refs/heads/deploy/test:deploy/zeroclaw/candidate/revision.yaml",
                )
            )
            self.assertEqual(candidate["data"]["revision"], revision)
            self.assertEqual(
                git("--git-dir=" + str(remote), "rev-parse", "main"), revision
            )
            self.assertLessEqual(len(git("log", "-1", "--format=%s")), 72)
