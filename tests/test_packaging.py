from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import venv

from tests import CLI_COMMAND, temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# PEP 639 metadata (`license = "Apache-2.0"`, `license-files`) is what the build
# backend has to understand; older setuptools rejects pyproject.toml outright.
MINIMUM_SETUPTOOLS = (77, 0)

# Paths that have to survive into the source distribution. `tests/__init__.py`
# is the load-bearing one: setuptools' implicit sdist file list only picks up
# `tests/test*.py`, so the package initialiser was dropped and
# `python -m unittest discover -s tests -t .` failed on the unpacked archive
# with "Start directory is not importable" -- while all sixteen test modules
# were present. The rest is what a packager or auditor needs to rebuild and
# check the release.
REQUIRED_SDIST_PATHS = (
    "MANIFEST.in",
    "Makefile",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "ROADMAP.md",
    "SECURITY.md",
    "SUPPORT.md",
    "tests/__init__.py",
    "tests/test_packaging.py",
    "docs/quickstart.md",
    "docs/releasing.md",
    "docs/internal/decision-log.md",
    "docs/assets/demo.svg",
    "tools/release_acceptance.py",
    "tools/check_secrets.py",
    "tools/check_history.py",
    "tools/check_release_consistency.py",
    "tools/check_reproducible.py",
    "tools/generate_sbom.py",
    "skills/capture-knowledge/SKILL.md",
    "demo/config/packs/travel/pack.yaml",
    "demo/session.sh",
    "deploy/zeroclaw/AGENTS.md",
    ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/CODEOWNERS",
)

REQUIRED_SDIST_DIRECTORIES = ("docs/", "tools/", "skills/", "demo/", "deploy/")

# `uses: owner/repo[/path]@ref`, with the ref that has to be a commit.
ACTION_REFERENCE = re.compile(
    r"uses:\s*(?P<action>[\w.-]+/[\w.-]+(?:/[\w./-]+)?)@(?P<ref>\S+)"
)
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
JOB_HEADING = re.compile(r"^  (?P<name>[A-Za-z0-9_-]+):\s*$")


def workflow_jobs(text: str) -> dict[str, str]:
    """Split a workflow's `jobs:` mapping into one source block per job.

    The project has no YAML parser -- it has no dependencies at all -- and the
    interesting assertions are per job, not per file: `id-token: write`
    anywhere in `release.yml` says nothing, `id-token: write` in the job that
    runs the build says a great deal. The workflows use two-space indentation
    throughout, which is all this needs.

    Comment-only lines are dropped. The comment above the build job explains
    why it has no `id-token: write`, and a test asserting the absence of that
    string must not be satisfied or defeated by prose.
    """

    lines = text.splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines) if line.rstrip() == "jobs:"
        )
    except StopIteration:
        return {}
    jobs: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1 :]:
        heading = JOB_HEADING.match(line)
        if heading is not None:
            current = heading.group("name")
            jobs[current] = []
            continue
        if line and not line.startswith(" "):
            break
        if current is not None and not line.lstrip().startswith("#"):
            jobs[current].append(line)
    return {name: "\n".join(body) for name, body in jobs.items()}

from noetrail.migrations import CURRENT_SCHEMA_VERSION  # noqa: E402
from noetrail.version import DISTRIBUTION_NAME, FALLBACK_VERSION  # noqa: E402

RESERVED_TOP_LEVEL_NAMES = (
    "bookmark_fetch_mcp",
    "know",
    "knowledge_layout",
    "knowledge_mcp",
    "knowledge_migrations",
    "knowledge_schema",
    "knowledge_version",
)


def _setuptools_version(interpreter: str) -> tuple[int, ...] | None:
    probe = subprocess.run(
        [
            interpreter,
            "-c",
            "import setuptools; print(setuptools.__version__)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return None
    parts = probe.stdout.strip().split(".")
    try:
        return tuple(int(part) for part in parts[:2])
    except ValueError:
        return None


class SourceDistributionTest(unittest.TestCase):
    """Build the real sdist and check what it carries."""

    def _build_interpreter(self, workspace: Path) -> str:
        """Pick an interpreter whose setuptools can read this pyproject.toml."""

        ambient = _setuptools_version(sys.executable)
        if ambient is not None and ambient >= MINIMUM_SETUPTOOLS:
            return sys.executable
        # `python -m build` is not assumed to be installed and the environment
        # may have no package index. A throwaway venv is seeded from the
        # interpreter's own bundled wheels, which is often newer than whatever
        # the system packaged.
        environment = workspace / "buildenv"
        venv.EnvBuilder(with_pip=True).create(environment)
        candidate = str(environment / "bin" / "python")
        seeded = _setuptools_version(candidate)
        if seeded is not None and seeded >= MINIMUM_SETUPTOOLS:
            return candidate
        raise unittest.SkipTest(
            "no reachable setuptools >= "
            f"{'.'.join(str(part) for part in MINIMUM_SETUPTOOLS)}; "
            "the CI sdist job covers this"
        )

    def _build_sdist(self, workspace: Path) -> Path:
        interpreter = self._build_interpreter(workspace)
        output = workspace / "dist"
        output.mkdir()
        # Build the tree from a copy: an sdist build writes `setup.cfg` and an
        # egg-info directory, and a test must not mutate the checkout.
        source = workspace / "source"
        shutil.copytree(
            REPOSITORY_ROOT,
            source,
            ignore=shutil.ignore_patterns(
                ".git", "build", "dist", "*.egg-info", "__pycache__", ".venv"
            ),
            symlinks=True,
        )
        built = subprocess.run(
            [
                interpreter,
                "-c",
                (
                    "import setuptools.build_meta as backend;"
                    + "print(backend.build_sdist(__import__('sys').argv[1]))"
                ),
                str(output),
            ],
            cwd=source,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        archives = list(output.glob("*.tar.gz"))
        self.assertEqual(len(archives), 1, archives)
        return archives[0]

    def test_sdist_contains_everything_needed_to_test_and_audit_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = temporary_root(raw)
            archive = self._build_sdist(workspace)
            with tarfile.open(archive) as handle:
                names = handle.getnames()

            prefix = f"{DISTRIBUTION_NAME}-{FALLBACK_VERSION}/"
            contained = {
                name[len(prefix) :] for name in names if name.startswith(prefix)
            }

            missing = [path for path in REQUIRED_SDIST_PATHS if path not in contained]
            self.assertEqual(missing, [], f"missing from sdist: {missing}")

            for directory in REQUIRED_SDIST_DIRECTORIES:
                self.assertTrue(
                    any(name.startswith(directory) for name in contained),
                    f"{directory} is absent from the sdist",
                )

            # Every test module must arrive, not just the ones setuptools
            # discovers by name.
            expected_tests = {
                f"tests/{path.name}"
                for path in (REPOSITORY_ROOT / "tests").glob("*.py")
            }
            self.assertEqual(expected_tests - contained, set())

            # Private runtime data and build residue must never ship.
            for name in contained:
                self.assertFalse(name.endswith(".lock"), name)
                self.assertNotIn("__pycache__", name)
                self.assertFalse(name.startswith(("vault/", "trash/", "imports/")))


class PackagingMetadataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.metadata = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

    def test_console_scripts_and_versions_are_consistent(self) -> None:
        project = self.metadata["project"]
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(project["license-files"], ["LICENSE"])
        self.assertEqual(project["name"], "noetrail")
        self.assertEqual(DISTRIBUTION_NAME, "noetrail")
        self.assertEqual(
            project["urls"]["Repository"],
            "https://github.com/patsch1/noetrail.git",
        )
        self.assertEqual(
            project["scripts"],
            {
                "noetrail": "noetrail.cli:main",
                "noetrail-mcp": "noetrail.mcp:main",
                "noetrail-bookmark-fetcher": "noetrail.bookmark_fetcher:main",
                "knowledge": "noetrail.cli:main",
                "knowledge-mcp": "noetrail.mcp:main",
                "knowledge-bookmark-fetcher": "noetrail.bookmark_fetcher:main",
            },
        )
        self.assertEqual(
            self.metadata["tool"]["setuptools"]["packages"]["find"]["where"],
            ["src"],
        )
        self.assertEqual(project["version"], FALLBACK_VERSION)
        self.assertEqual(
            self.metadata["tool"]["noetrail"]["schema-version"],
            CURRENT_SCHEMA_VERSION,
        )

    def test_distribution_claims_only_the_noetrail_import_namespace(self) -> None:
        source_root = REPOSITORY_ROOT / "src"
        entries = sorted(
            path.name
            for path in source_root.iterdir()
            if not path.name.startswith(".") and not path.name.endswith(".egg-info")
        )
        self.assertEqual(entries, ["noetrail"])
        for name in RESERVED_TOP_LEVEL_NAMES:
            self.assertFalse(
                (source_root / f"{name}.py").exists(),
                f"{name} would be installed as a generic top-level module",
            )
        acceptance = (
            REPOSITORY_ROOT / "tools" / "release_acceptance.py"
        ).read_text(encoding="utf-8")
        self.assertIn("verify_import_namespace", acceptance)

    def test_compatibility_shims_delegate_to_the_package(self) -> None:
        shims = {
            "know.py": "noetrail.cli",
            "knowledge_mcp.py": "noetrail.mcp",
            "bookmark_fetch_mcp.py": "noetrail.bookmark_fetcher",
        }
        expected = subprocess.run(
            [*CLI_COMMAND, "--version"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for name, target in shims.items():
            shim = REPOSITORY_ROOT / "tools" / name
            source = shim.read_text(encoding="utf-8")
            self.assertIn(f"from {target} import main", source)
            self.assertIn("0.11.0", source)
        observed = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "tools" / "know.py"), "--version"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(observed, expected)

    def test_apache_license_is_present(self) -> None:
        license_text = (REPOSITORY_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn("END OF TERMS AND CONDITIONS", license_text)

    def test_every_declared_distribution_resource_exists(self) -> None:
        data_files = self.metadata["tool"]["setuptools"]["data-files"]
        self.assertTrue(data_files)
        self.assertTrue(
            all(path.startswith("share/noetrail/") for path in data_files)
        )
        for patterns in data_files.values():
            for pattern in patterns:
                matches = list(REPOSITORY_ROOT.glob(pattern))
                self.assertTrue(matches, pattern)
                self.assertTrue(all(path.is_file() for path in matches), pattern)

    def test_ci_is_synthetic_and_covers_supported_python_versions(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f'"{version}"', workflow)
        self.assertIn("python -m unittest discover -s tests -t . -v", workflow)
        self.assertIn("python tools/check_secrets.py", workflow)
        self.assertIn("python tools/check_git_boundary.py", workflow)
        self.assertIn("python -m pip wheel .", workflow)
        self.assertIn("python -m ruff check .", workflow)
        self.assertIn("python tools/release_acceptance.py", workflow)
        self.assertNotIn("continue-on-error: true", workflow)
        self.assertNotIn("KNOWLEDGE_DATA_ROOT", workflow)
        self.assertNotIn("${{ secrets.", workflow)
        self.assertNotIn("make check", workflow)

        # `on: push` without a branch filter plus `pull_request` ran the whole
        # matrix twice for every branch that had an open pull request.
        self.assertIn("branches: [main]", workflow)
        self.assertIn("cancel-in-progress: true", workflow)

        # The suite has to be proven runnable from the built sdist, not only
        # from the checkout.
        self.assertIn("python -m build --sdist", workflow)
        self.assertIn("tar xzf dist/noetrail-*.tar.gz", workflow)

        makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(
            "rm -rf build dist src/noetrail.egg-info tools/noetrail.egg-info",
            makefile,
        )

    def _release_workflow(self) -> str:
        return (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

    def test_release_workflow_builds_verifies_and_describes_the_artifacts(
        self,
    ) -> None:
        jobs = workflow_jobs(self._release_workflow())
        build = jobs["build"]
        # docs/releasing.md promises PyPI the identical reviewed artifacts, and
        # PyPI wants a source distribution beside the wheel.
        self.assertIn("python -m build --sdist --wheel --outdir dist", build)
        self.assertIn("python tools/release_acceptance.py", build)
        self.assertIn("python tools/check_release_consistency.py", build)
        self.assertIn("actions/upload-artifact@", build)
        # A build that embeds "now" cannot be checked by anyone, so the
        # timestamp is pinned and the result is rebuilt and compared.
        self.assertIn("SOURCE_DATE_EPOCH", build)
        self.assertIn("tools/check_reproducible.py", build)
        # Zero runtime dependencies is only a claim without a document.
        self.assertIn("tools/generate_sbom.py", build)
        self.assertNotIn("gh release create", self._release_workflow())

    @unittest.skipUnless(shutil.which("sha256sum"), "requires sha256sum")
    def test_downloaded_release_checksums_verify_without_build_paths(self) -> None:
        """Run the real checksum step, then verify a flat consumer download."""
        match = re.search(
            r"      - name: Create checksums\n        run: \|\n"
            r"(?P<script>(?:          .+\n)+)",
            self._release_workflow(),
        )
        self.assertIsNotNone(match)
        assert match is not None
        script = "\n".join(line[10:] for line in match["script"].splitlines())
        with tempfile.TemporaryDirectory(prefix="noetrail-checksums-") as temp:
            root = Path(temp)
            dist = root / "dist"
            dist.mkdir()
            filenames = (
                "noetrail-0.0.0-py3-none-any.whl",
                "noetrail-0.0.0.tar.gz",
                "noetrail-sbom.cdx.json",
            )
            for filename in filenames:
                (dist / filename).write_bytes(b"synthetic release fixture\n")
            subprocess.run(["/bin/sh", "-c", script], cwd=root, check=True)
            download = root / "download"
            shutil.copytree(dist, download)
            shutil.rmtree(dist)
            manifest = (download / "SHA256SUMS.txt").read_text(encoding="utf-8")
            self.assertEqual(
                {line.split(maxsplit=1)[1] for line in manifest.splitlines()},
                set(filenames),
            )
            command = ["sha256sum", "--check", "SHA256SUMS.txt"]
            verified = subprocess.run(command, cwd=download, capture_output=True)
            self.assertEqual(verified.returncode, 0, verified.stderr.decode())
            (download / filenames[0]).write_bytes(b"tampered synthetic fixture\n")
            tampered = subprocess.run(command, cwd=download, capture_output=True)
            self.assertNotEqual(tampered.returncode, 0)

    def test_release_workflow_uses_trusted_publishing_and_no_api_token(self) -> None:
        """Short-lived OIDC only: this project has no PyPI API token at all."""

        workflow = self._release_workflow()
        jobs = workflow_jobs(workflow)
        self.assertEqual(
            sorted(jobs),
            ["attest", "build", "publish-pypi", "publish-testpypi"],
        )

        # Nothing that could carry or accept a long-lived credential.
        self.assertNotIn("${{ secrets.", workflow)
        self.assertNotIn("PYPI_API_TOKEN", workflow)
        self.assertNotIn("__token__", workflow)
        self.assertNotIn("password:", workflow)
        # The workflow default has to stay least privilege.
        self.assertIn("permissions:\n  contents: read", workflow)

        # The job that runs the project's own code and the build backend must
        # not be able to mint a token an index would accept.
        self.assertNotIn("id-token", jobs["build"])
        self.assertNotIn("environment:", jobs["build"])

        for name, environment in (
            ("publish-testpypi", "testpypi"),
            ("publish-pypi", "pypi"),
        ):
            with self.subTest(job=name):
                job = jobs[name]
                self.assertIn("pypa/gh-action-pypi-publish@", job)
                self.assertIn("id-token: write", job)
                # A protected environment is what binds the OIDC claim, so a
                # token cannot be replayed from another workflow or branch.
                self.assertIn("environment:", job)
                self.assertIn(f"name: {environment}", job)
                # `workflow_dispatch` rehearses a build; it must not upload.
                self.assertIn("startsWith(github.ref, 'refs/tags/v')", job)
                # The artifact also carries checksums and the SBOM; an index
                # accepts distributions only.
                self.assertIn(
                    "mv staging/noetrail-*.whl staging/noetrail-*.tar.gz dist/",
                    job,
                )
                self.assertNotIn("python -m build", job)

        # TestPyPI is the rehearsal, so it has to come first.
        self.assertIn("https://test.pypi.org/legacy/", jobs["publish-testpypi"])
        self.assertIn("needs: publish-testpypi", jobs["publish-pypi"])
        self.assertNotIn("test.pypi.org/legacy", jobs["publish-pypi"])

    def test_release_workflow_attests_both_artifacts(self) -> None:
        attest = workflow_jobs(self._release_workflow())["attest"]
        self.assertIn("actions/attest-build-provenance@", attest)
        # Sigstore signing needs an OIDC token and the attestation store.
        self.assertIn("id-token: write", attest)
        self.assertIn("attestations: write", attest)
        for subject in ("dist/noetrail-*.whl", "dist/noetrail-*.tar.gz"):
            self.assertIn(subject, attest)
        # It attests what the build produced; it must not build anything.
        self.assertNotIn("python -m build", attest)
        self.assertIn("actions/download-artifact@", attest)

    def test_release_tag_filter_covers_more_than_alpha_tags(self) -> None:
        """A `v1.0.0` tag used to produce no release build at all."""

        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        for suffix in ("", "a[0-9]+", "b[0-9]+", "rc[0-9]+"):
            self.assertIn(
                f'"v[0-9]+.[0-9]+.[0-9]+{suffix}"',
                workflow,
                f"release.yml does not trigger for a {suffix or 'final'} tag",
            )

    def test_every_third_party_action_is_pinned_to_a_commit(self) -> None:
        """A mutable major tag can be repointed at new code at any time."""

        workflows = sorted(
            (REPOSITORY_ROOT / ".github" / "workflows").glob("*.yml")
        )
        self.assertTrue(workflows)
        unpinned: list[str] = []
        for workflow in workflows:
            for number, line in enumerate(
                workflow.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = ACTION_REFERENCE.search(line)
                if match is None:
                    continue
                if not COMMIT_SHA.fullmatch(match.group("ref")):
                    unpinned.append(f"{workflow.name}:{number}: {match.group(0)}")
                    continue
                # The trailing comment is the only readable record of which
                # release a SHA stands for; Dependabot updates both together.
                self.assertRegex(
                    line,
                    r"#\s*v\d+(\.\d+)*",
                    f"{workflow.name}:{number} has no version comment",
                )
        self.assertEqual(unpinned, [])

    def test_dependabot_watches_the_pinned_actions(self) -> None:
        configuration = (
            REPOSITORY_ROOT / ".github" / "dependabot.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('package-ecosystem: "github-actions"', configuration)
        self.assertIn('interval: "weekly"', configuration)

    def test_codeql_analyses_python_with_least_privilege(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "codeql.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("languages: python", workflow)
        self.assertIn("security-events: write", workflow)
        self.assertIn("github/codeql-action/init@", workflow)
        self.assertIn("github/codeql-action/analyze@", workflow)


if __name__ == "__main__":
    unittest.main()
