#!/usr/bin/env python3
"""Measure where search time is actually spent, before optimizing anything.

Generates a synthetic vault of a requested size and reports where the time
goes: process startup, the scan itself, a full CLI subprocess call, and a
representative agent turn answered through both Noetrail MCP read paths.
Attributing latency to the right layer decides whether an index is worth its
consistency cost, or whether the cost is dominated by process startup.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from noetrail.cli import build_parser, run_command  # noqa: E402
from noetrail.entries import load_entries  # noqa: E402
from noetrail.index import index_path, scan_signatures  # noqa: E402
from noetrail.index import refresh as refresh_index  # noqa: E402
from noetrail.layout import resolve_layout  # noqa: E402
from noetrail.mcp import NoetrailServer  # noqa: E402
from noetrail.migrations import CURRENT_SCHEMA_VERSION  # noqa: E402
from noetrail.schema import SchemaRegistry  # noqa: E402

WORDS = (
    "harbour lantern quarry meridian basalt cadence thistle fathom "
    "obsidian trellis vellum kestrel sandstone paddock lucerne"
).split()


def vocabulary(size: int) -> list[str]:
    """Deterministic word list of a requested size.

    The default fifteen words keep every previously published number
    reproducible. They are also a corpus no lexical index can be judged on:
    with a vocabulary that small every query matches nearly everything, so
    posting lists are as long as the vault and the index is as expensive to
    read as the scan. A realistic profile therefore passes `--vocabulary` and
    `--body-words`; the generated words stay deterministic so a rerun measures
    the same corpus.
    """

    if size <= len(WORDS):
        return WORDS[:size]
    letters = "abcdefghijklmnopqrstuvwxyz"
    extra = []
    index = 0
    while len(WORDS) + len(extra) < size:
        value = index
        word = ""
        for _ in range(5):
            word += letters[value % 26]
            value //= 26
        extra.append(word)
        index += 1
    return [*WORDS, *extra]


def body_words(words: list[str], index: int, count: int) -> list[str]:
    """Pick `count` words for entry `index` with a stable, skewed spread.

    The stride is a prime step through the vocabulary, so different entries get
    different words while the same entry always gets the same ones. Squaring
    the offset biases selection towards the front of the list, which gives the
    frequency skew a real corpus has and that BM25's IDF term exists for.
    """

    size = len(words)
    return [
        words[(index * 7 + offset * offset * 13 + offset) % size]
        for offset in range(count)
    ]


def synthesize(
    data: Path,
    config: Path,
    count: int,
    *,
    words: list[str] | None = None,
    length: int = 12,
) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "noetrail.cli",
            "--data-root",
            str(data),
            "--config-root",
            str(config),
            "init",
        ],
        check=True,
        capture_output=True,
        env={"PYTHONPATH": str(REPOSITORY_ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    inbox = data / "vault" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    selected = words if words is not None else WORDS
    for index in range(count):
        if words is None and length == 12:
            # The original generator, kept byte-for-byte so the numbers in
            # docs/limits.md stay reproducible with the documented command.
            entry_words = [
                WORDS[(index + offset) % len(WORDS)] for offset in range(12)
            ]
        else:
            entry_words = body_words(selected, index, length)
        entry_id = f"kn_{index:032x}"
        timestamp = "2026-01-01T00:00:00+00:00"
        (inbox / f"2026-01-01--synthetic-{index:06d}--{index:08x}.md").write_text(
            "---\n"
            f'id: "{entry_id}"\n'
            f"schema_version: {CURRENT_SCHEMA_VERSION}\n"
            'type: "thought"\n'
            "type_version: 1\n"
            "attributes: {}\n"
            f'title: "Synthetic entry {index}"\n'
            f'created_at: "{timestamp}"\n'
            f'updated_at: "{timestamp}"\n'
            'status: "active"\n'
            'sensitivity: "personal"\n'
            "tags: []\n"
            "relations: []\n"
            'origin: "conversation"\n'
            "---\n\n"
            f"{' '.join(entry_words)}\n",
            encoding="utf-8",
        )


def agent_turn(server: NoetrailServer, entry_ids: list[str]) -> None:
    """One representative turn: an overview, three searches, two entry reads."""

    server.call_tool("inventory", {})
    for word in ("obsidian lantern", "kestrel thistle", "vellum cadence"):
        server.call_tool("search", {"query": word})
    for entry_id in entry_ids:
        server.call_tool("get_entry", {"id": entry_id})


def cli(data: Path, config: Path, *arguments: str) -> None:
    """Run one command in-process, so the sample is scan cost, not startup.

    A subprocess sample buries the thing under measurement -- how often a
    command walks the vault -- under ~40 ms of interpreter startup that no
    amount of indexing removes.
    """

    with contextlib.redirect_stdout(io.StringIO()):
        run_command(
            [
                "--data-root",
                str(data),
                "--config-root",
                str(config),
                *arguments,
            ]
        )


def mutation_samples(
    data: Path, config: Path, entries: int, repeats: int
) -> dict[str, dict[str, float]]:
    """Time the mutating commands whose cost is dominated by vault scans.

    Each repeat has to change something the previous one did not, otherwise
    `tag` and `relate` refuse the write and the sample measures the refusal.
    """

    first = f"kn_{0:032x}"
    second = f"kn_{entries // 2:032x}"
    counter = {"tag": 0, "capture": 0, "relate": 0}

    def tag_once() -> None:
        counter["tag"] += 1
        cli(data, config, "tag", first, f"measure-{counter['tag']}")

    def capture_once() -> None:
        counter["capture"] += 1
        cli(
            data,
            config,
            "capture",
            "--type",
            "note",
            "--title",
            f"Measure capture {counter['capture']}",
            "--text",
            "Measurement body.",
        )

    def relate_once() -> None:
        counter["relate"] += 1
        predicate = "about" if counter["relate"] % 2 else "related_to"
        cli(data, config, "relate", first, predicate, second)
        cli(data, config, "relate", first, predicate, second, "--remove")

    return {
        "tag": timed(tag_once, repeats),
        "capture": timed(capture_once, repeats),
        # Two calls per sample (add, then remove) so the vault returns to its
        # starting state; halve to read it as one `relate`.
        "relate_add_and_remove": timed(relate_once, repeats),
    }


def timed(function, repeats: int) -> dict[str, float]:
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        function()
        samples.append(time.perf_counter() - start)
    return {
        "median_ms": round(statistics.median(samples) * 1000, 1),
        "max_ms": round(max(samples) * 1000, 1),
    }


def index_samples(
    data: Path,
    config: Path,
    layout,
    entries: int,
    repeats: int,
    queries: tuple[str, ...],
) -> dict[str, object]:
    """Time the index against the scan it is meant to replace.

    Four things decide whether the cache earns its consistency cost: what a
    full build costs, what it costs on disk, what one more entry costs, and
    how much of a query it actually removes. The freshness check is timed on
    its own because every indexed query pays it -- an index that answered
    instantly but took as long as the scan to verify would be worth nothing.
    """

    cli(data, config, "index", "rebuild", "--full")
    stored = index_path(layout)

    def rebuild_full() -> None:
        cli(data, config, "index", "rebuild", "--full")

    counter = {"value": 0}

    def incremental() -> None:
        """One capture with the index present: write plus index maintenance."""

        counter["value"] += 1
        cli(
            data,
            config,
            "capture",
            "--type",
            "note",
            "--title",
            f"Index update {counter['value']}",
            "--text",
            f"harbour lantern measurement {counter['value']}",
        )

    touched = {"value": 0}

    def update_one() -> None:
        """Index maintenance alone, with one entry changed.

        Timed against the library rather than through `capture`, because a
        capture also pays for the vault scan it would pay for without any
        index at all. What this reports is the part the index adds.
        """

        touched["value"] += 1
        target = sorted((data / "vault" / "inbox").glob("*.md"))[touched["value"]]
        target.write_text(
            target.read_text(encoding="utf-8") + "\nharbour lantern addition\n",
            encoding="utf-8",
        )
        refresh_index(layout.data_root, layout, SchemaRegistry.load(layout))

    report: dict[str, object] = {
        "index_build_full": timed(rebuild_full, max(2, repeats // 2)),
        "index_bytes": stored.stat().st_size,
        "index_bytes_per_entry": round(stored.stat().st_size / max(entries, 1), 1),
        "freshness_check": timed(
            lambda: scan_signatures(layout.data_root), repeats
        ),
        "index_update_one_entry": timed(update_one, repeats),
        "capture_with_index": timed(incremental, repeats),
    }
    for query in queries:
        report[f"search_substring[{query}]"] = timed(
            lambda q=query: cli(data, config, "search", q, "--rank", "substring"),
            repeats
        )
        report[f"search_hybrid_indexed[{query}]"] = timed(
            lambda q=query: cli(data, config, "search", q), repeats
        )
        report[f"search_bm25_indexed[{query}]"] = timed(
            lambda q=query: cli(data, config, "search", q, "--rank", "bm25"),
            repeats,
        )
    cli(data, config, "index", "drop")
    report["capture_without_index"] = timed(incremental, repeats)
    for query in queries:
        report[f"search_hybrid_scan[{query}]"] = timed(
            lambda q=query: cli(data, config, "search", q), repeats
        )
        report[f"search_bm25_scan[{query}]"] = timed(
            lambda q=query: cli(data, config, "search", q, "--rank", "bm25"),
            repeats,
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(prog="measure-search")
    parser.add_argument("--entries", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--vocabulary",
        type=int,
        default=len(WORDS),
        help="synthetic vocabulary size (default reproduces published numbers)",
    )
    parser.add_argument(
        "--body-words",
        type=int,
        default=12,
        help="words per synthetic body (default reproduces published numbers)",
    )
    parser.add_argument(
        "--skip-agent-turns",
        action="store_true",
        help="omit the MCP turn samples, which dominate runtime on large vaults",
    )
    arguments = parser.parse_args()
    if arguments.entries < 2 or arguments.repeats < 1:
        parser.error("entries must be at least 2 and repeats at least 1")
    if arguments.vocabulary < 1 or arguments.body_words < 1:
        parser.error("vocabulary and body-words must be positive")

    with tempfile.TemporaryDirectory(prefix="noetrail-measure-") as directory:
        base = Path(directory)
        data = base / "data"
        config = base / "config"
        data.mkdir()
        config.mkdir()
        words = vocabulary(arguments.vocabulary)
        synthesize(
            data,
            config,
            arguments.entries,
            words=None if arguments.vocabulary == len(WORDS) else words,
            length=arguments.body_words,
        )

        layout = resolve_layout(data_root=str(data), config_root=str(config))
        command = [
            sys.executable,
            "-m",
            "noetrail.cli",
            "--data-root",
            str(data),
            "--config-root",
            str(config),
            "search",
            "obsidian",
        ]
        environment = {
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
            "PATH": "/usr/bin:/bin",
        }

        entry_ids = [f"kn_{index:032x}" for index in (0, arguments.entries // 2)]
        in_process_server = NoetrailServer(layout, in_process_reads=True)
        subprocess_server = NoetrailServer(layout, in_process_reads=False)

        report = {
            "entries": arguments.entries,
            "interpreter_startup": timed(
                lambda: subprocess.run(
                    [sys.executable, "-c", "pass"],
                    check=True,
                    capture_output=True,
                ),
                arguments.repeats,
            ),
            "cli_import_and_parse": timed(build_parser, arguments.repeats),
            "in_process_scan": timed(
                lambda: load_entries(layout.data_root), arguments.repeats
            ),
            # Every synthetic entry carries 12 of the 15 words, so "obsidian"
            # is a broad query: it matches almost the whole vault and only the
            # first page is printed. The no-match query is the control -- the
            # difference between the two is the per-match work that the
            # `--limit` slice throws away again.
            "in_process_search_broad": timed(
                lambda: cli(data, config, "search", "obsidian"),
                arguments.repeats,
            ),
            "in_process_search_no_match": timed(
                lambda: cli(data, config, "search", "quernstone-absent"),
                arguments.repeats,
            ),
            "full_subprocess_search": timed(
                lambda: subprocess.run(
                    command, check=True, capture_output=True, env=environment
                ),
                arguments.repeats,
            ),
            "in_process_mutations": mutation_samples(
                data, config, arguments.entries, arguments.repeats
            ),
        }
        if not arguments.skip_agent_turns:
            report.update({
            "agent_turn_subprocess_reads": timed(
                lambda: agent_turn(subprocess_server, entry_ids),
                arguments.repeats,
            ),
            "agent_turn_in_process_reads": timed(
                lambda: agent_turn(in_process_server, entry_ids),
                arguments.repeats,
            ),
            })
        report["vocabulary"] = arguments.vocabulary
        report["body_words"] = arguments.body_words
        # A broad term matches most of the vault, a selective one matches a
        # handful. The index can only skip work the answer does not need, so
        # the two cases have to be reported apart rather than averaged.
        report["bm25"] = index_samples(
            data,
            config,
            layout,
            arguments.entries,
            arguments.repeats,
            ("obsidian", words[min(len(words) - 1, 900)], "obsidian lantern"),
        )
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
