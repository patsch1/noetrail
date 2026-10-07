"""The README image has to be a recording of this program, not a picture of it.

A screenshot in a repository goes stale silently: the command changes, the
output changes, and the image keeps showing what the project used to do. Here
the image is generated from `demo/session.sh`, so this test runs the session
again and compares. Only the values a fresh capture necessarily produces -- the
entry ID, its revision, the current date -- are masked out.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RENDERER = REPOSITORY_ROOT / "tools" / "render_terminal_svg.py"
TRANSCRIPT = REPOSITORY_ROOT / "docs" / "assets" / "demo.txt"
IMAGE = REPOSITORY_ROOT / "docs" / "assets" / "demo.svg"
SESSION = REPOSITORY_ROOT / "demo" / "session.sh"


def load_renderer() -> object:
    specification = importlib.util.spec_from_file_location(
        "render_terminal_svg", RENDERER
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class DemoScriptTest(unittest.TestCase):
    def test_the_session_is_executable_and_leaves_the_checkout_alone(self) -> None:
        self.assertTrue(SESSION.is_file())
        self.assertTrue(SESSION.stat().st_mode & 0o111, "session.sh is not executable")
        source = SESSION.read_text(encoding="utf-8")
        # It must work on a copy: the checked-in demo instance is a fixture
        # other tests assert on, and a capture into it would change their
        # expected counts.
        self.assertIn("mktemp -d", source)
        self.assertIn('cp -R demo/data "$workspace/data"', source)
        self.assertIn("trap 'rm -rf \"$workspace\"' EXIT", source)
        self.assertIn("set -euo pipefail", source)

    def test_the_demo_instance_is_unchanged_after_a_recording(self) -> None:
        before = {
            path.relative_to(REPOSITORY_ROOT): path.read_bytes()
            for path in sorted((REPOSITORY_ROOT / "demo" / "data").rglob("*"))
            if path.is_file()
        }
        self.assertTrue(before)
        module = load_renderer()
        module.record(SESSION)  # type: ignore[attr-defined]
        after = {
            path.relative_to(REPOSITORY_ROOT): path.read_bytes()
            for path in sorted((REPOSITORY_ROOT / "demo" / "data").rglob("*"))
            if path.is_file()
        }
        self.assertEqual(before, after)


class RecordingIsCurrentTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("bash") is None, "bash is unavailable")
    def test_the_committed_recording_still_matches_a_fresh_run(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(RENDERER), "--check"],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        self.assertEqual(
            completed.returncode,
            0,
            "docs/assets/ is stale; regenerate with "
            "`python3 tools/render_terminal_svg.py`\n"
            f"{completed.stdout}\n{completed.stderr}",
        )

    def test_the_transcript_contains_real_output_not_placeholders(self) -> None:
        transcript = TRANSCRIPT.read_text(encoding="utf-8")
        commands = [
            line for line in transcript.splitlines() if line.startswith("$ ")
        ]
        self.assertGreaterEqual(len(commands), 4)
        for expected in ("noetrail search", "noetrail capture", "noetrail review"):
            self.assertTrue(
                any(expected in command for command in commands), expected
            )
        # Output that only a real run produces: a stable demo entry ID, a
        # content revision, and the validation summary.
        self.assertIn("kn_22222222222222222222222222222222", transcript)
        self.assertIn('"revision": "sha256:', transcript)
        self.assertIn("Validation passed for 3 active", transcript)
        for placeholder in ("...", "<output>", "TODO", "example output"):
            self.assertNotIn(placeholder, transcript)

    def test_the_image_is_a_plain_svg_without_script_or_external_reference(
        self,
    ) -> None:
        image = IMAGE.read_text(encoding="utf-8")
        self.assertTrue(image.startswith("<svg "))
        self.assertIn("</svg>", image)
        for forbidden in ("<script", "javascript:", "<foreignObject", "xlink:href"):
            self.assertNotIn(forbidden, image)
        # GitHub renders it as an image; it must carry its own text.
        self.assertIn("<title>", image)
        self.assertIn("<desc>", image)


if __name__ == "__main__":
    unittest.main()
