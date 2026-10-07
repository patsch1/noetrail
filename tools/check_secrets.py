#!/usr/bin/env python3
"""Detect common credential material without printing the secret value."""

from __future__ import annotations

import argparse
from pathlib import Path
import re

SKIP_DIRECTORIES = {
    ".git",
    "__pycache__",
    ".knowledge/cache",
    ".knowledge/index",
    "imports/work",
    # Local virtual environments are Git-ignored and full of vendored CA
    # bundles, which are not project secrets.
    ".venv",
    "venv",
    ".devenv",
}
SENSITIVE_FILENAMES = {
    ".env",
    "config.toml",
    "credentials",
    "credentials.json",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
SECRET_PATTERNS = {
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    ),
    "OpenAI-style API key": re.compile(
        r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"
    ),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
}


def relative_directory_is_skipped(relative: Path) -> bool:
    value = relative.as_posix()
    return any(
        value == skipped or value.startswith(f"{skipped}/")
        for skipped in SKIP_DIRECTORIES
    )


def candidate_paths(root: Path) -> list[Path]:
    candidates = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative_directory_is_skipped(relative):
            continue
        candidates.append(path)
    return sorted(candidates)


def scan(root: Path) -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in candidate_paths(root):
        relative = path.relative_to(root)
        if (
            path.name in SENSITIVE_FILENAMES
            and path.name != "security-profiles.example.toml"
        ):
            findings.append((relative, 0, "sensitive filename"))
        if path.suffix.casefold() in SENSITIVE_SUFFIXES:
            findings.append((relative, 0, "sensitive key-file extension"))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    findings.append((relative, line_number, kind))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(prog="check-secrets")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    findings = scan(root)
    for path, line, kind in findings:
        location = f"{path}:{line}" if line else str(path)
        print(f"ERROR {location}: possible {kind}")
    if findings:
        print(f"Secret scan failed with {len(findings)} finding(s).")
        return 1
    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
