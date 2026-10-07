"""The community files exist, and the templates actually enforce what they ask.

A repository can carry a `CODE_OF_CONDUCT.md` with an unfilled placeholder
address, an issue form whose fields are all optional, and a pull-request
template nobody has to fill in. Each of those is worse than not having the file
at all, because it looks like a commitment. These tests check the parts that
make the files load-bearing.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import unittest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")
ISSUE_TEMPLATES = REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE"
MAINTAINER_EMAIL = "patrick.schiffler@yahoo.de"

# The environment facts SUPPORT.md asks every reporter for. Before the issue
# forms existed, it asked and nothing enforced it.
REQUIRED_FIELDS = ("version", "python", "os", "doctor")

# `id: <name>` followed, somewhere in the same block, by `required: true`.
FIELD_BLOCK = re.compile(r"^  - type: .*?(?=^  - type: |\Z)", re.MULTILINE | re.DOTALL)


def field_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for block in FIELD_BLOCK.findall(text):
        identifier = re.search(r"^    id: (\S+)$", block, re.MULTILINE)
        if identifier is not None:
            blocks[identifier.group(1)] = block
    return blocks


class CodeOfConductTest(unittest.TestCase):
    def test_contributor_covenant_with_a_reachable_contact(self) -> None:
        text = (REPOSITORY_ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
        self.assertIn("Contributor Covenant", text)
        self.assertIn("version 2.1", text)
        # The upstream text ships with a placeholder. Shipping it unfilled is
        # the single most common way this file becomes decoration.
        for placeholder in (
            "[INSERT CONTACT METHOD]",
            "INSERT CONTACT",
            "TODO",
            "example.com",
        ):
            self.assertNotIn(placeholder, text)
        self.assertIn(MAINTAINER_EMAIL, text)
        # A one-person project cannot pretend to have an escalation committee.
        self.assertIn("maintained by one person", text)


class IssueTemplateTest(unittest.TestCase):
    def test_the_three_forms_exist_and_blank_issues_are_disabled(self) -> None:
        names = sorted(path.name for path in ISSUE_TEMPLATES.glob("*.yml"))
        self.assertEqual(
            names,
            [
                "bug_report.yml",
                "config.yml",
                "documentation.yml",
                "feature_request.yml",
            ],
        )
        configuration = (ISSUE_TEMPLATES / "config.yml").read_text(encoding="utf-8")
        self.assertIn("blank_issues_enabled: false", configuration)
        # A reporter who has a question or a vulnerability must be sent
        # somewhere other than the issue tracker.
        self.assertIn("/discussions", configuration)
        self.assertIn("/security/advisories/new", configuration)
        self.assertIn("SUPPORT.md", configuration)

    def test_every_form_requires_the_environment_support_md_asks_for(self) -> None:
        for template in ("bug_report.yml", "feature_request.yml", "documentation.yml"):
            text = (ISSUE_TEMPLATES / template).read_text(encoding="utf-8")
            blocks = field_blocks(text)
            with self.subTest(template=template):
                self.assertIn("name:", text)
                self.assertIn("description:", text)
                for field in REQUIRED_FIELDS:
                    self.assertIn(field, blocks, f"{template} has no {field} field")
                    self.assertIn(
                        "required: true",
                        blocks[field],
                        f"{template}: {field} is optional",
                    )
                self.assertIn("noetrail doctor", text)

    def test_every_form_warns_against_pasting_private_data(self) -> None:
        for path in sorted(ISSUE_TEMPLATES.glob("*.yml")):
            if path.name == "config.yml":
                continue
            text = (path.read_text(encoding="utf-8")).casefold()
            with self.subTest(template=path.name):
                self.assertIn("no personal vault content", text)


class PullRequestTemplateTest(unittest.TestCase):
    def test_it_asks_for_the_four_things_contributing_asks_for(self) -> None:
        template = (
            REPOSITORY_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
        ).read_text(encoding="utf-8")
        for heading in (
            "## User-visible behavior and compatibility",
            "## Security and privacy boundaries",
            "## Tests run",
            "## Migration, rollback, and documentation",
        ):
            self.assertIn(heading, template)
        self.assertIn("- [ ] `make check` passes locally", template)
        self.assertIn("synthetic data only", template)
        self.assertIn("Apache License 2.0", template)

        # CONTRIBUTING.md is the source; the template must not drift from it.
        contributing = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("The pull-request template has one", contributing)


class CodeownersTest(unittest.TestCase):
    def test_every_owned_path_exists_and_has_an_owner(self) -> None:
        codeowners = (REPOSITORY_ROOT / ".github" / "CODEOWNERS").read_text(
            encoding="utf-8"
        )
        rules = [
            line.split()
            for line in codeowners.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(rules)
        self.assertEqual(rules[0][0], "*", "CODEOWNERS has no catch-all rule")
        for rule in rules:
            pattern, owners = rule[0], rule[1:]
            with self.subTest(pattern=pattern):
                self.assertTrue(owners, f"{pattern} has no owner")
                for owner in owners:
                    self.assertTrue(owner.startswith("@"), owner)
                if pattern == "*":
                    continue
                # A rule for a path that no longer exists silently stops
                # requesting a review for anything.
                self.assertTrue(
                    (REPOSITORY_ROOT / pattern.strip("/")).exists(),
                    f"{pattern} does not exist",
                )


class SecurityPolicyTest(unittest.TestCase):
    def test_it_makes_a_concrete_promise_instead_of_deferring_one(self) -> None:
        policy = (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertNotIn("No fixed response-time guarantee", policy)
        self.assertNotIn("will be listed here once", policy)
        # An acknowledgement window and a disclosure deadline, both stated.
        self.assertIn("72 hours", policy)
        self.assertIn("90 days", policy)
        # Both reporting channels, and the private one first.
        self.assertIn("security/advisories/new", policy)
        self.assertIn("Private Vulnerability Reporting", policy)
        self.assertIn(MAINTAINER_EMAIL, policy)
        self.assertLess(
            policy.index("security/advisories/new"),
            policy.index(MAINTAINER_EMAIL),
            "the public mailbox is offered before the private form",
        )
        # A supported-version table, not a promise to write one later.
        self.assertIn("## Supported versions", policy)
        self.assertIn("0.10.x", policy)


class RenameTest(unittest.TestCase):
    """The project was `knowledge` before it was Noetrail.

    Most surviving occurrences are deliberate and recorded in ADR 0002: the
    `.knowledge/` format directory, the `knowledge*` console aliases, the
    `KNOWLEDGE_*` variables, the ZeroClaw server key and its `knowledge__*`
    tools, the legacy distribution name. And "knowledge" is an ordinary noun
    this project cannot avoid using. So this does not try to allowlist what may
    stay; it names the specific forms that were actually left behind, so they
    cannot come back a sentence at a time.
    """

    FORBIDDEN = (
        # The component is the Noetrail MCP server. The ZeroClaw server key is
        # separately still `knowledge`, written as such and lowercase.
        (re.compile(r"[Kk]nowledge MCP"), "call it the Noetrail MCP"),
        # `KnowledgeLayout` and `KnowledgeServer` outlived the rename by a week.
        (re.compile(r"\bKnowledge[A-Z]\w*"), "rename the identifier to Noetrail*"),
        (re.compile(r"PersonalKnowledge(?!-)"), "rename it to Noetrail*"),
        # The canonical deployment paths are /opt/noetrail, /etc/noetrail, and
        # /srv/noetrail-data.
        (re.compile(r"/srv/knowledge"), "use /srv/noetrail"),
    )

    # ADR 0002 quotes the old names to explain the rename, and the changelog
    # records the aliases a deployment still calls.
    EXEMPT = {
        "docs/adr/0002-noetrail-name-and-compatibility.md",
        # A changelog records what a name used to be. Naming the old form is
        # the entry's whole point.
        "CHANGELOG.md",
        # This file has to spell the forbidden forms in order to look for them.
        "tests/test_governance.py",
    }

    def test_nothing_carries_the_old_project_name(self) -> None:
        # The file list comes from Git so that an ignored vault, a build
        # directory, or a local virtual environment is not scanned. The suite
        # also runs from an unpacked source distribution, which is the same
        # tree without a repository; there is nothing to guard against there,
        # because the archive is built from what this check already passed.
        if GIT is None or not (REPOSITORY_ROOT / ".git").exists():
            self.skipTest("not a Git checkout")
        listing = subprocess.run(
            [GIT, "ls-files", "-z"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        failures: list[str] = []
        for name in listing.stdout.split("\0"):
            if not name or name in self.EXEMPT or name.startswith("vault/"):
                continue
            path = REPOSITORY_ROOT / name
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for pattern, remedy in self.FORBIDDEN:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    failures.append(f"{name}:{line}: {match.group(0)} -- {remedy}")
        self.assertEqual(failures, [])


class AiAuthorshipDisclosureTest(unittest.TestCase):
    """The disclosure is only worth something where a reader arrives first.

    Buried in a linked page it is a formality; a reader who never opens
    `docs/` never learns it. So the three entry documents have to say it
    themselves, and the README has to say it above the fold rather than in the
    document list at the bottom.
    """

    PAGE = Path("docs") / "ai-authorship.md"

    def test_the_page_says_what_it_does_and_does_not_change(self) -> None:
        page = (REPOSITORY_ROOT / self.PAGE).read_text(encoding="utf-8")
        self.assertIn("written by AI coding agents", re.sub(r"\s+", " ", page))
        # A disclosure that only confesses is half of one. These are the parts
        # that let a reader act on it.
        self.assertIn("Co-Authored-By", page)
        self.assertIn("## Accountability and reports", page)
        self.assertIn("not a guarantee of correctness", page)
        for gate in ("ruff", "mypy", "check_git_boundary.py"):
            self.assertIn(gate, page)

    def test_the_entry_documents_state_it_in_their_own_terms(self) -> None:
        link = "docs/ai-authorship.md"
        for name in ("README.md", "CONTRIBUTING.md", "SECURITY.md"):
            document = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("AI coding agents", re.sub(r"\s+", " ", document), name)
            self.assertIn(link, document, name)

    def test_the_readme_states_it_before_the_feature_list(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertLess(
            readme.index("AI-developed, maintainer-directed"),
            readme.index("## Get started"),
            "the disclosure has slipped below the first installation step",
        )


if __name__ == "__main__":
    unittest.main()
