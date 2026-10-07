#!/usr/bin/env python3
"""Record `demo/session.sh` and draw its transcript as a static SVG terminal.

Why this exists
---------------
The project had no image at all. An asciinema cast is a recording of a human
typing, which cannot be regenerated or checked in CI; a hand-drawn "screenshot"
of invented output is worse than none. This script runs the demo session for
real and draws exactly what came back, so the picture in the README is a build
artifact with a test behind it (`tests/test_demo_recording.py`).

Nothing here invents text. The only transformation applied to the captured
bytes is wrapping at a fixed terminal width, which is what a terminal does.

Determinism
-----------
The session runs against a disposable copy of the synthetic demo vault, whose
entries have fixed IDs and timestamps, so its read output is identical on every
machine. The one capture it performs necessarily produces a fresh entry ID, a
fresh revision, and the current date. `--check` therefore compares a new
recording against the committed one with exactly those run-specific values
masked, and fails on any other difference.

Two files are written. `demo.txt` is the recording itself, plain text, which is
what the comparison runs against; `demo.svg` is a pure function of it. Keeping
them apart means the check never has to guess where a wrapped line ended.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCRIPT = REPOSITORY_ROOT / "demo" / "session.sh"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "docs" / "assets" / "demo.svg"

COLUMNS = 100
FONT_SIZE = 13
# 0.6 em is the advance width of the DejaVu Sans Mono / Menlo / Consolas
# family this falls back through. It only has to be close: the drawing places
# whole lines, not individual glyphs.
CHARACTER_WIDTH = FONT_SIZE * 0.6
LINE_HEIGHT = 18
PADDING = 16
TITLE_BAR = 30

BACKGROUND = "#15181e"
TITLE_BAR_FILL = "#20242c"
TITLE_TEXT = "#7d8794"
PROMPT_COLOR = "#8fbf7f"
COMMAND_COLOR = "#e8ebf0"
OUTPUT_COLOR = "#aeb6c2"

# Values that necessarily change from run to run. Masked only for `--check`;
# the committed image keeps the real ones.
VOLATILE = (
    (re.compile(r"kn_[0-9a-f]{32}"), "kn_<id>"),
    (re.compile(r"sha256:[0-9a-f]{64}"), "sha256:<digest>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+-]+"), "<timestamp>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}--"), "<date>--"),
    (re.compile(r"--[0-9a-f]{8}\.md"), "--<short-id>.md"),
)


def launcher(directory: Path) -> Path:
    """A `noetrail` on PATH that runs this checkout, not an installed copy."""

    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "noetrail"
    script.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -m noetrail.cli "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def record(script: Path) -> str:
    """Run the demo session and return its combined output."""

    if shutil.which("bash") is None:
        raise SystemExit("bash is required to record the demo session")
    with tempfile.TemporaryDirectory() as raw:
        binaries = Path(raw) / "bin"
        launcher(binaries)
        environment = dict(os.environ)
        environment["PATH"] = f"{binaries}{os.pathsep}{environment['PATH']}"
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
        environment["NOETRAIL_BUILTINS_ROOT"] = str(REPOSITORY_ROOT / ".knowledge")
        # A locale or a colour-capable TERM would make the output depend on the
        # machine that recorded it.
        environment["LC_ALL"] = "C.UTF-8"
        environment["TERM"] = "dumb"
        environment.pop("NO_COLOR", None)
        completed = subprocess.run(
            ["bash", str(script)],  # noqa: S607
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    if completed.returncode != 0:
        raise SystemExit(
            f"{script} exited {completed.returncode}:\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    if completed.stderr.strip():
        raise SystemExit(f"{script} wrote to stderr:\n{completed.stderr}")
    return completed.stdout


def wrap(transcript: str, columns: int = COLUMNS) -> list[tuple[str, bool]]:
    """Hard-wrap at the terminal width, the way a terminal does.

    Each result is the line and whether it belongs to a typed command, so the
    continuation of a wrapped command is not coloured as output.
    """

    lines: list[tuple[str, bool]] = []
    for line in transcript.rstrip("\n").split("\n"):
        text = line.rstrip()
        command = text.startswith("$ ")
        if not text:
            lines.append(("", False))
            continue
        while len(text) > columns:
            lines.append((text[:columns], command))
            text = text[columns:]
        lines.append((text, command))
    return lines


def escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def render(lines: list[tuple[str, bool]], title: str) -> str:
    width = int(COLUMNS * CHARACTER_WIDTH) + 2 * PADDING
    height = TITLE_BAR + PADDING + len(lines) * LINE_HEIGHT + PADDING

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(title)}" font-family="ui-monospace, '
        f'SFMono-Regular, Menlo, Consolas, DejaVu Sans Mono, monospace" '
        f'font-size="{FONT_SIZE}" xml:space="preserve">',
        f"<title>{escape(title)}</title>",
        "<desc>A recorded terminal session; every line below a prompt is the "
        "real output of the command above it.</desc>",
        f'<rect width="{width}" height="{height}" rx="8" fill="{BACKGROUND}"/>',
        f'<path d="M0 8a8 8 0 0 1 8-8h{width - 16}a8 8 0 0 1 8 8v{TITLE_BAR - 8}'
        f'H0Z" fill="{TITLE_BAR_FILL}"/>',
        f'<text x="{PADDING}" y="20" fill="{TITLE_TEXT}" font-size="11">'
        f"{escape(title)}</text>",
    ]

    baseline = TITLE_BAR + PADDING + FONT_SIZE
    for index, (line, command) in enumerate(lines):
        if not line:
            continue
        y = baseline + index * LINE_HEIGHT
        if line.startswith("$ "):
            parts.append(
                f'<text x="{PADDING}" y="{y}" fill="{PROMPT_COLOR}">$ '
                f'<tspan fill="{COMMAND_COLOR}">{escape(line[2:])}</tspan>'
                "</text>"
            )
        else:
            colour = COMMAND_COLOR if command else OUTPUT_COLOR
            parts.append(
                f'<text x="{PADDING}" y="{y}" fill="{colour}">'
                f"{escape(line)}</text>"
            )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def mask(document: str) -> str:
    for pattern, replacement in VOLATILE:
        document = pattern.sub(replacement, document)
    return document


def differences(committed: str, recorded: str, label: str) -> list[str]:
    import difflib

    return [
        f"--- committed {label}",
        f"+++ recorded {label}",
        *list(
            difflib.unified_diff(
                committed.splitlines(),
                recorded.splitlines(),
                lineterm="",
                n=1,
            )
        )[2:42],
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="SVG to write; the transcript goes beside it as .txt",
    )
    parser.add_argument(
        "--title",
        default="noetrail - one agent turn against the synthetic demo vault",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-record and fail if the committed recording no longer matches",
    )
    args = parser.parse_args(argv)

    transcript_path = args.output.with_suffix(".txt")
    recorded = record(args.script)

    if not args.check:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(recorded, encoding="utf-8")
        document = render(wrap(recorded), args.title)
        args.output.write_text(document, encoding="utf-8")
        print(f"wrote {transcript_path} and {args.output}")
        return 0

    missing = [path for path in (transcript_path, args.output) if not path.is_file()]
    if missing:
        print(
            "missing: "
            + ", ".join(str(path) for path in missing)
            + "; run without --check",
            file=sys.stderr,
        )
        return 1

    committed_transcript = transcript_path.read_text(encoding="utf-8")
    failures: list[str] = []
    if mask(committed_transcript) != mask(recorded):
        failures.extend(
            differences(
                mask(committed_transcript), mask(recorded), transcript_path.name
            )
        )
    # The image has to be the drawing of the committed transcript, not of some
    # earlier one: a transcript update with a forgotten re-render is exactly
    # the stale-screenshot failure this whole script exists to prevent.
    expected = render(wrap(committed_transcript), args.title)
    if expected != args.output.read_text(encoding="utf-8"):
        failures.append(f"{args.output.name} is not the rendering of the transcript")

    if not failures:
        print(f"{transcript_path.name} and {args.output.name} are current")
        return 0
    print("\n".join(failures), file=sys.stderr)
    print(
        "regenerate with `python3 tools/render_terminal_svg.py`",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
