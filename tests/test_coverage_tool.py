"""Tests for the dependency-free coverage tool.

The tool decides whether CI passes, so its two failure modes need a check of
their own: silently measuring nothing (which would report a green 100%), and
silently measuring the wrong lines after a rename in the module it slices.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tests import temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPOSITORY_ROOT / "tools" / "coverage.py"


def _load_tool() -> object:
    # Imported by path rather than by name: `tools` is not a package, and
    # `import coverage` would find whatever is installed instead.
    specification = importlib.util.spec_from_file_location(
        "noetrail_coverage_tool",
        TOOL,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


tool = _load_tool()


class StatementDetectionTest(unittest.TestCase):
    def write(self, source: str) -> Path:
        directory = tempfile.mkdtemp()
        path = Path(directory) / "sample.py"
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        self.addCleanup(path.unlink)
        return path

    def test_blank_lines_and_comments_are_not_statements(self) -> None:
        path = self.write(
            """\
            # a comment
            x = 1

            y = 2
            """
        )
        self.assertEqual(tool.statements_of(path), {2, 4})

    def test_a_pragma_excludes_the_block_it_opens(self) -> None:
        path = self.write(
            """\
            def f(value):
                try:
                    return int(value)
                except ValueError:  # pragma: no cover - defence in depth
                    raise RuntimeError("nope") from None
            """
        )
        statements = tool.statements_of(path)
        self.assertIn(3, statements)
        self.assertNotIn(4, statements)
        self.assertNotIn(5, statements)

    def test_multiline_annotations_are_not_executable_statements(self) -> None:
        path = self.write(
            """\
            def transform(
                value: list[str],
                *,
                enabled: bool = True,
            ) -> dict[str, int]:
                if enabled:
                    return {item: len(item) for item in value}
                return {}
            """
        )
        statements = tool.statements_of(path)
        self.assertEqual(statements, {1, 6, 7, 8})

    def test_a_slice_over_an_unknown_definition_is_refused(self) -> None:
        path = self.write("def present():\n    return 1\n")
        with self.assertRaisesRegex(SystemExit, "unknown definitions"):
            tool.definition_ranges(path, ["present", "renamed_away"])


class SsrfSliceTest(unittest.TestCase):
    def test_every_sliced_definition_still_exists(self) -> None:
        # A rename in `bookmark_fetcher` must fail loudly here rather than
        # quietly shrink the surface the 95% threshold is measured over.
        source = (
            REPOSITORY_ROOT
            / "src"
            / "noetrail"
            / str(tool.SSRF_SLICE["file"])
        )
        span = tool.definition_ranges(source, tool.SSRF_SLICE["definitions"])
        self.assertGreater(len(span), 100)


class SubprocessMeasurementTest(unittest.TestCase):
    def test_a_child_process_is_measured(self) -> None:
        """The suite runs the CLI as a subprocess; missing those is the one
        error that would make every number meaningless."""

        with tempfile.TemporaryDirectory() as directory:
            base = temporary_root(directory)
            package = base / "measured"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "worker.py").write_text(
                "def touched():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def untouched():\n"
                "    return 2\n"
                "\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    touched()\n",
                encoding="utf-8",
            )
            report = base / "report.json"
            # The measured command starts a *further* Python process; only the
            # sitecustomize hook can see it.
            driver = base / "driver.py"
            driver.write_text(
                "import subprocess, sys\n"
                f"sys.path.insert(0, {str(base)!r})\n"
                "import measured.worker  # noqa: F401\n"
                f"subprocess.run([sys.executable, {str(package / 'worker.py')!r}],"
                " check=True)\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL),
                    "--source",
                    str(package),
                    "--json",
                    str(report),
                    "--no-thresholds",
                    "--",
                    sys.executable,
                    str(driver),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertGreaterEqual(payload["measured_processes"], 2)
            worker = next(
                entry
                for entry in payload["modules"]
                if entry["name"] == "worker.py"
            )
            # `untouched` is defined but never called, so its body is the only
            # thing that may be reported as missing.
            self.assertEqual(worker["missing_lines"], [6])


if __name__ == "__main__":
    unittest.main()
