from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

from tests import temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
# A URL that points back into this repository, and the path it names.
REPOSITORY_URL = re.compile(
    r"https://(?:github\.com/patsch1/noetrail/(?:blob|tree)/main"
    r"|raw\.githubusercontent\.com/patsch1/noetrail/main)/([^)\s\"]+)"
)

# An HTML comment on its own line opts the next ```sh block out of execution.
# It is invisible in a rendered document, and the reason after the marker is
# what a reviewer reads when deciding whether the exemption still holds.
SKIP_MARKER = "<!-- docs-check: skip"

# Documents whose shell blocks are a walkthrough a reader is expected to paste
# and run, so they have to keep working. `docs/quickstart.md` once told the
# reader to expand `$demo_root` where the surrounding blocks had defined
# `$DEMO_ROOT`; the block failed with "data root is unavailable: /data" and
# every link- and prose-level check still passed.
EXECUTABLE_DOCUMENTS = ("README.md", "docs/quickstart.md")


def shell_blocks(text: str) -> tuple[list[str], list[str]]:
    """Split a document's ```sh blocks into runnable and opted-out ones."""

    lines = text.splitlines()
    runnable: list[str] = []
    exempt: list[str] = []
    marked = False
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith(SKIP_MARKER):
            marked = True
            index += 1
            continue
        if stripped == "```sh":
            end = index + 1
            while end < len(lines) and lines[end].strip() != "```":
                end += 1
            body = "\n".join(lines[index + 1 : end])
            (exempt if marked else runnable).append(body)
            marked = False
            index = end + 1
            continue
        # A marker applies to the block that follows it, not to a block further
        # down the document behind unrelated prose.
        if stripped and not stripped.startswith("<!--"):
            marked = False
        index += 1
    return runnable, exempt


class ShellExampleTest(unittest.TestCase):
    """Run the documented shell walkthroughs in a disposable sandbox."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.sandbox = temporary_root(self.temporary)
        # Both walkthroughs open with `pip install .` into `.venv`, which needs
        # a network and a build. That block is opted out; this stand-in gives
        # the remaining blocks the same `.venv/bin/noetrail` entry point, so
        # they run exactly as written.
        launcher = self.sandbox / ".venv" / "bin" / "noetrail"
        launcher.parent.mkdir(parents=True)
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -m noetrail.cli "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_document(self, script: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
        environment["NOETRAIL_BUILTINS_ROOT"] = str(REPOSITORY_ROOT / ".knowledge")
        # `mktemp -d` in the documented blocks then lands inside the sandbox and
        # is removed with it.
        environment["TMPDIR"] = str(self.sandbox)
        environment["HOME"] = str(self.sandbox)
        return subprocess.run(
            # The documentation says "```sh" and a reader pastes into
            # whatever bash is on PATH; a hard-coded path would test a
            # different shell than the one the reader actually uses.
            ["bash", "-euo", "pipefail", "-c", script],  # noqa: S607
            cwd=self.sandbox,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )

    @unittest.skipIf(shutil.which("bash") is None, "bash is unavailable")
    def test_documented_shell_examples_run(self) -> None:
        for relative in EXECUTABLE_DOCUMENTS:
            with self.subTest(document=relative):
                document = REPOSITORY_ROOT / relative
                runnable, _ = shell_blocks(document.read_text(encoding="utf-8"))
                self.assertTrue(
                    runnable,
                    f"{relative} has no executable shell block left; an "
                    f"exemption was probably added too broadly",
                )
                # The blocks share state: one defines DEMO_ROOT, later ones use
                # it. Running them as one script is what makes an undefined or
                # misspelled variable fail.
                result = self.run_document("\n\n".join(runnable))
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{relative}\n--- stdout ---\n{result.stdout}"
                    f"\n--- stderr ---\n{result.stderr}",
                )

    def test_every_exempt_block_states_a_reason(self) -> None:
        for relative in EXECUTABLE_DOCUMENTS:
            document = REPOSITORY_ROOT / relative
            text = document.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith(SKIP_MARKER):
                    continue
                reason = stripped[len(SKIP_MARKER) :].removesuffix("-->").strip(" -")
                self.assertTrue(reason, f"{relative}: bare skip marker")


class DocumentationTest(unittest.TestCase):
    def test_relative_markdown_links_resolve(self) -> None:
        # The root governance documents are included because they are the ones
        # a reader arrives at first, and because splitting the roadmap into
        # `ROADMAP.md` and `docs/internal/decision-log.md` moved links across
        # two directory levels, which is exactly where a relative path breaks.
        documents = [
            *sorted(REPOSITORY_ROOT.glob("*.md")),
            *sorted((REPOSITORY_ROOT / "docs").rglob("*.md")),
            *sorted((REPOSITORY_ROOT / "demo").rglob("*.md")),
            *sorted((REPOSITORY_ROOT / ".github").rglob("*.md")),
        ]
        failures: list[str] = []
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for target in MARKDOWN_LINK.findall(text):
                if (
                    target.startswith(("http://", "https://", "#"))
                    or "://" in target
                ):
                    continue
                path_value = target.split("#", 1)[0]
                if not path_value:
                    continue
                resolved = (document.parent / path_value).resolve()
                if not resolved.exists():
                    failures.append(
                        f"{document.relative_to(REPOSITORY_ROOT)} -> {target}"
                    )
        self.assertEqual(failures, [])

    def test_readme_urls_name_files_that_exist(self) -> None:
        # `pyproject.toml` sets `readme = "README.md"`, so this file is also the
        # package long description. A package index resolves a relative link
        # against its own host, where every one of them is a 404 and neither
        # image renders. The README therefore uses absolute URLs and reference
        # definitions -- which moves the risk from "breaks on the package page"
        # to "points at a file that was renamed", so the paths are checked here.
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        targets = REPOSITORY_URL.findall(readme)
        self.assertGreater(len(targets), 20, "the URLs stopped being repository paths")
        failures = [
            target
            for target in targets
            if not (REPOSITORY_ROOT / target.split("#", 1)[0].rstrip("/")).exists()
        ]
        self.assertEqual(failures, [])

        relative = [
            target
            for _, target in re.findall(r"(!?)\[[^\]]*\]\(([^)]+)\)", readme)
            if not target.startswith(("http", "#", "mailto:"))
        ]
        relative += [
            source
            for source in re.findall(r'<img[^>]*src="([^"]+)"', readme)
            if not source.startswith("http")
        ]
        self.assertEqual(relative, [], "a relative link would 404 on a package index")

    def test_publication_switches_are_documented(self) -> None:
        # `docs/releasing.md` lists the sentences that stop being true when the
        # repository goes public or the package is uploaded. A list like that
        # is only useful while it is accurate, and its failure mode is silent:
        # the phrase gets reworded, the entry points at nothing, and the person
        # doing the release finds out by reading a stale claim on the live page.
        releasing = (REPOSITORY_ROOT / "docs" / "releasing.md").read_text(
            encoding="utf-8"
        )
        section = releasing.split("## Documentation that changes")[1].split("\n## ")[0]
        quoted = re.findall(r'"([^"]+)"', section)
        self.assertGreaterEqual(len(quoted), 6, "the list lost entries")
        pages = [
            "README.md",
            "ROADMAP.md",
            "docs/quickstart.md",
            "docs/installation.md",
            "docs/integrations/mcp-clients.md",
        ]
        # The pages wrap at 79 columns, so a quoted phrase routinely straddles a
        # line break in its source. Compare on collapsed whitespace.
        corpus = re.sub(
            r"\s+",
            " ",
            "\n".join(
                (REPOSITORY_ROOT / page).read_text(encoding="utf-8") for page in pages
            ),
        )
        for phrase in quoted:
            self.assertIn(
                re.sub(r"\s+", " ", phrase), corpus, f"no page still says {phrase!r}"
            )

    def test_readme_badges_state_only_what_the_repository_enforces(self) -> None:
        """A badge is a claim; each of these has to match its source of truth."""

        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

        # The first alpha is published; its entry command selects that version.
        self.assertNotIn("img.shields.io/pypi", readme)
        self.assertIn("https://pypi.org/project/noetrail/0.10.0a1/", readme)
        self.assertIn("uvx --from 'noetrail==0.10.0a1' noetrail quickstart", readme)

        # The CI badge has to point at a workflow that exists.
        self.assertIn(
            "https://github.com/patsch1/noetrail/actions/workflows/ci.yml/badge.svg",
            readme,
        )
        self.assertTrue(
            (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").is_file()
        )

        # The coverage badge states the enforced floor, not a measured value,
        # so it must equal the floor `tools/coverage.py` actually applies.
        coverage_tool = (REPOSITORY_ROOT / "tools" / "coverage.py").read_text(
            encoding="utf-8"
        )
        threshold = re.search(
            r"DEFAULT_TOTAL_THRESHOLD\s*=\s*([0-9]+)(?:\.0)?", coverage_tool
        )
        assert threshold is not None
        self.assertIn(f"coverage%20gate-{threshold.group(1)}%25", readme)

        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertIn(
            f"runtime%20dependencies-{len(project['dependencies'])}", readme
        )
        badge = re.search(r"img\.shields\.io/badge/python-(\S+?)\)", readme)
        assert badge is not None
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(version, badge.group(1))
            self.assertIn(
                f"Programming Language :: Python :: {version}",
                "\n".join(project["classifiers"]),
            )

    def test_readme_shows_the_recorded_demo_and_not_an_illustration(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        # The README carries absolute URLs so that it also renders on a package
        # index; `test_readme_urls_name_files_that_exist` maps them back.
        self.assertIn("docs/assets/demo.svg", readme)
        for relative in ("docs/assets/demo.svg", "docs/assets/demo.txt"):
            self.assertTrue((REPOSITORY_ROOT / relative).is_file(), relative)
        # The image is a drawing of the transcript, so every prompt line in the
        # recording has to appear in it.
        transcript = (REPOSITORY_ROOT / "docs" / "assets" / "demo.txt").read_text(
            encoding="utf-8"
        )
        image = (REPOSITORY_ROOT / "docs" / "assets" / "demo.svg").read_text(
            encoding="utf-8"
        )
        commands = [
            line[2:] for line in transcript.splitlines() if line.startswith("$ ")
        ]
        self.assertGreaterEqual(len(commands), 4)
        for command in commands:
            self.assertIn(command[:80], image, command)
        # Nothing executable may end up in an SVG the README embeds.
        self.assertNotIn("<script", image)
        self.assertNotIn("javascript:", image)

    def test_the_valid_time_diagram_stays_one_drawing_in_two_palettes(
        self,
    ) -> None:
        # GitHub renders a repository SVG as an image, so a single file cannot
        # follow the reader's theme; the light and dark variants are picked by
        # `<picture>` instead. That leaves two files to keep identical, and the
        # only thing allowed to differ between them is the palette.
        palette = {
            "#9A6700": "#D29922",
            "#59636E": "#9198A1",
            "#D8DEE4": "#30363D",
            "#AFB8C1": "#6E7681",
            "#1D9E75": "#2EA883",
            "#0F7A5A": "#3FBC92",
        }
        assets = REPOSITORY_ROOT / "docs" / "assets"
        light = (assets / "valid-time-light.svg").read_text(encoding="utf-8")
        dark = (assets / "valid-time-dark.svg").read_text(encoding="utf-8")
        recoloured = light
        for from_colour, to_colour in palette.items():
            recoloured = recoloured.replace(from_colour, to_colour)
        self.assertEqual(recoloured, dark)
        for image in (light, dark):
            # Nothing executable may end up in an SVG a document embeds.
            self.assertNotIn("<script", image)
            self.assertNotIn("javascript:", image)
            # The predicate has to be one the shipped vocabulary accepts, or
            # the drawing teaches a relation that `relate` would refuse.
            self.assertIn("created_by", image)
        vocabulary = (
            REPOSITORY_ROOT / ".knowledge" / "relation-types.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("\n  created_by:", vocabulary)

        architecture = (REPOSITORY_ROOT / "docs" / "architecture.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('srcset="assets/valid-time-dark.svg"', architecture)
        self.assertIn('src="assets/valid-time-light.svg"', architecture)

    def test_the_public_roadmap_is_separate_from_the_public_decision_record(
        self,
    ) -> None:
        """Operational journals must not be part of a public repository."""

        self.assertFalse((REPOSITORY_ROOT / "docs" / "open-source-roadmap.md").exists())
        roadmap = (REPOSITORY_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        log = (
            REPOSITORY_ROOT / "docs" / "internal" / "decision-log.md"
        ).read_text(encoding="utf-8")

        # The forward-looking document must not carry a progress log with it.
        for marker in ("Decision and progress log", "in_progress", "Baseline commit"):
            self.assertNotIn(marker, roadmap)
        self.assertLess(len(roadmap), 8000, "ROADMAP.md is growing into a log again")

        # And it has to say what is deliberately not going to happen.
        self.assertIn("Deliberately not planned", roadmap)
        self.assertIn("docs/internal/decision-log.md", roadmap)
        self.assertIn("Public product decision record", log)
        self.assertIn("make publication-check", log)
        for marker in ("Baseline commit:", "backup age", "Flux revision"):
            self.assertNotIn(marker, log)

    def test_agents_file_guides_a_contributor_instead_of_assigning_a_plan(
        self,
    ) -> None:
        """It is read by every agent that touches a fork, not only this one."""

        agents = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for instruction in (
            "persistent source of truth",
            "first unfinished phase",
            "open-source-roadmap",
        ):
            self.assertNotIn(instruction, agents)
        # What it must say instead: how to pass the gates.
        for gate in (
            "python3 -m ruff check .",
            "python3 -m mypy",
            "python3 -m unittest discover -s tests -t . -v",
            "python3 tools/coverage.py",
            "python3 tools/check_secrets.py",
            "python3 tools/check_git_boundary.py",
        ):
            self.assertIn(gate, agents)

    def test_project_metadata_declares_supported_python(self) -> None:
        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["dependencies"], [])

    def test_human_security_documentation_is_visible(self) -> None:
        self.assertTrue(
            (
                REPOSITORY_ROOT / "docs" / "integrations" / "zeroclaw-security.md"
            ).is_file()
        )
        self.assertFalse(
            (
                REPOSITORY_ROOT
                / ".knowledge"
                / "ZEROCLAW_SECURITY.md"
            ).exists()
        )

    def test_zeroclaw_runtime_instructions_are_dedicated_and_hardened(self) -> None:
        prompt = (
            REPOSITORY_ROOT / "deploy" / "zeroclaw" / "AGENTS.md"
        ).read_text(encoding="utf-8")

        self.assertIn("knowledge__inventory` exactly once", prompt)
        self.assertIn("without titles, bodies, or a result cap", prompt)
        self.assertIn("Never search for\n`https`, `http`", prompt)
        self.assertIn("Preserve its `total`, `returned`, and `has_more`", prompt)
        self.assertIn("knowledge__list_pending_attachments", prompt)
        self.assertIn("Treat every delegation as the complete task", prompt)
        self.assertIn("Never use shell", prompt)
        self.assertNotIn("Open-source roadmap", prompt)
        self.assertNotIn("python3 tools/know.py", prompt)
        self.assertNotIn("tools/knowledge_mcp.py", prompt)

    def test_portable_skills_do_not_depend_on_the_zeroclaw_overlay(self) -> None:
        skills = sorted((REPOSITORY_ROOT / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        for skill in skills:
            text = skill.read_text(encoding="utf-8")
            with self.subTest(skill=skill.parent.name):
                self.assertNotIn("knowledge__", text)
                self.assertNotIn("bookmark_fetch__", text)
                self.assertNotIn("ZeroClaw", text)
                self.assertNotIn("[IMAGE:", text)
                self.assertNotIn("[image:", text)

    def test_generic_mcp_documentation_uses_the_real_tool_surface(self) -> None:
        document = (
            REPOSITORY_ROOT / "docs" / "integrations" / "mcp-clients.md"
        ).read_text(encoding="utf-8")
        for tool in (
            "`search`",
            "`inventory`",
            "`set_relation`",
            "`set_tags`",
            "`get_attachment`",
        ):
            self.assertIn(tool, document)
        self.assertIn("prefix is a\nhost convention", document)
        self.assertIn("`server/discover`", document)
        self.assertIn("`initialize`", document)
        self.assertNotIn("`knowledge__relate`", document)
        self.assertNotIn("`knowledge__tag`", document)

    def test_bookmark_fetcher_deployment_has_no_vault_mount(self) -> None:
        deployment = (
            REPOSITORY_ROOT
            / "deploy"
            / "zeroclaw"
            / "bookmark-fetcher-deployment.example.yaml"
        ).read_text(encoding="utf-8")
        containerfile = (
            REPOSITORY_ROOT
            / "deploy"
            / "zeroclaw"
            / "Containerfile.bookmark-fetcher"
        ).read_text(encoding="utf-8")
        network_policy = (
            REPOSITORY_ROOT
            / "deploy"
            / "zeroclaw"
            / "bookmark-fetcher-network-policy.example.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("automountServiceAccountToken: false", deployment)
        self.assertIn("readOnlyRootFilesystem: true", deployment)
        self.assertIn("secretKeyRef:", deployment)
        self.assertNotIn("volumeMounts:", deployment)
        self.assertNotIn("volumes:", deployment)
        self.assertNotIn("vault", deployment.casefold())
        self.assertIn("COPY src/noetrail/bookmark_fetcher.py", containerfile)
        self.assertIn("COPY src/noetrail/mcp_protocol.py", containerfile)
        self.assertNotIn("COPY . ", containerfile)
        self.assertNotIn("cli.py", containerfile)
        self.assertNotIn("mcp.py", containerfile)
        self.assertNotIn(".knowledge", containerfile)
        self.assertIn("- Ingress", network_policy)
        self.assertIn("- Egress", network_policy)


if __name__ == "__main__":
    unittest.main()
