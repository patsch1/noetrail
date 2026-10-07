"""The two release-verification tools, checked without building a release.

`tools/generate_sbom.py` and `tools/check_reproducible.py` are what turns two
claims -- "no runtime dependencies" and "this artifact can be rebuilt" -- into
something a third party can check. Both are exercised here against real
packaging metadata and real archives, but without running a full build: the
release workflow does that, and it needs a package index.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import venv

from tests import temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPOSITORY_ROOT / "tools"
FIXED_EPOCH = "1754438400"  # 2025-08-06T00:00:00Z


def load_tool(name: str) -> object:
    """Import a maintainer script from `tools/` without installing it."""

    import importlib.util

    specification = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class SoftwareBillOfMaterialsTest(unittest.TestCase):
    def generate(self, *arguments: str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(TOOLS / "generate_sbom.py"), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "SOURCE_DATE_EPOCH": FIXED_EPOCH},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        parsed = json.loads(completed.stdout)
        assert isinstance(parsed, dict)
        return parsed

    def test_it_describes_this_distribution_in_cyclonedx(self) -> None:
        document = self.generate()
        self.assertEqual(document["bomFormat"], "CycloneDX")
        self.assertEqual(document["specVersion"], "1.6")
        self.assertTrue(str(document["serialNumber"]).startswith("urn:uuid:"))

        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        metadata = document["metadata"]
        assert isinstance(metadata, dict)
        component = metadata["component"]
        assert isinstance(component, dict)
        self.assertEqual(component["name"], project["name"])
        self.assertEqual(component["version"], project["version"])
        self.assertEqual(
            component["purl"],
            f"pkg:pypi/{project['name']}@{project['version']}",
        )
        self.assertEqual(
            component["licenses"], [{"license": {"id": project["license"]}}]
        )

    def test_the_component_list_is_read_from_the_metadata(self) -> None:
        """The empty list is the claim; it must not be a hard-coded empty one."""

        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
            declared = tomllib.load(handle)["project"]["dependencies"]
        self.assertEqual(declared, [], "this test needs updating, not the tool")
        self.assertEqual(self.generate()["components"], [])

        module = load_tool("generate_sbom")
        # Give the same code a dependency and it has to appear.
        components = module.dependency_components(  # type: ignore[attr-defined]
            ["requests>=2.31; python_version >= '3.11'"]
        )
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["name"], "requests")
        self.assertEqual(components[0]["purl"], "pkg:pypi/requests")

    def test_it_is_deterministic_and_records_artifact_digests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifact = temporary_root(raw) / "noetrail-0.0.0-py3-none-any.whl"
            artifact.write_bytes(b"synthetic")
            first = self.generate("--distribution", str(artifact))
            second = self.generate("--distribution", str(artifact))
        self.assertEqual(first, second)
        metadata = first["metadata"]
        assert isinstance(metadata, dict)
        self.assertEqual(metadata["timestamp"], "2025-08-06T00:00:00Z")
        component = metadata["component"]
        assert isinstance(component, dict)
        distributions = [
            reference
            for reference in component["externalReferences"]
            if reference["type"] == "distribution"
        ]
        self.assertEqual(len(distributions), 1)
        self.assertEqual(
            distributions[0]["hashes"],
            [{"alg": "SHA-256", "content": hashlib.sha256(b"synthetic").hexdigest()}],
        )
        self.assertEqual(distributions[0]["url"], "noetrail-0.0.0-py3-none-any.whl")


class ReproducibleComparisonTest(unittest.TestCase):
    """The comparison logic, against archives built here rather than a release."""

    def archive(self, path: Path, payloads: dict[str, bytes], mtime: int) -> Path:
        with tarfile.open(path, "w:gz") as handle:
            for name, payload in sorted(payloads.items()):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mtime = mtime
                info.mode = 0o644
                handle.addfile(info, io.BytesIO(payload))
        return path

    def test_build_directory_is_not_an_installed_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = temporary_root(raw)
            environment = workspace / "venv"
            venv.EnvBuilder(with_pip=False).create(environment)
            python = environment / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            source = workspace / "source"
            (source / "build").mkdir(parents=True)
            probe = (
                "import importlib.util,json;from pathlib import Path;import sys;"
                "import build;assert build.__spec__.origin is None;"
                f"s=importlib.util.spec_from_file_location('repro',"
                f"{str(TOOLS / 'check_reproducible.py')!r});"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "print(json.dumps(m.build_command(sys.executable,Path('dist'))))"
            )
            result = subprocess.run(
                [str(python), "-c", probe], cwd=source, capture_output=True,
                text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            command = json.loads(result.stdout)
            self.assertEqual(command[1], "-c")
            self.assertIn("setuptools.build_meta", command[2])

    def test_a_timestamp_only_difference_is_reported_but_tolerated(self) -> None:
        module = load_tool("check_reproducible")
        with tempfile.TemporaryDirectory() as raw:
            workspace = temporary_root(raw)
            payloads = {"pkg/PKG-INFO": b"Name: noetrail\n", "pkg/a.py": b"x = 1\n"}
            left = self.archive(workspace / "left.tar.gz", payloads, 1_000)
            right = self.archive(workspace / "right.tar.gz", payloads, 2_000)
            differences, timestamps = module.compare_sdists(left, right)  # type: ignore[attr-defined]
        self.assertEqual(differences, [])
        self.assertEqual(sorted(timestamps), ["pkg/PKG-INFO", "pkg/a.py"])

    def test_a_content_difference_fails(self) -> None:
        module = load_tool("check_reproducible")
        with tempfile.TemporaryDirectory() as raw:
            workspace = temporary_root(raw)
            left = self.archive(
                workspace / "left.tar.gz", {"pkg/a.py": b"x = 1\n"}, 1_000
            )
            right = self.archive(
                workspace / "right.tar.gz",
                {"pkg/a.py": b"x = 2\n", "pkg/b.py": b""},
                1_000,
            )
            differences, _ = module.compare_sdists(left, right)  # type: ignore[attr-defined]
        self.assertTrue(any("only in the rebuild" in item for item in differences))
        self.assertTrue(any("bytes !=" in item for item in differences))


if __name__ == "__main__":
    unittest.main()
