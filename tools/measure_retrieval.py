#!/usr/bin/env python3
"""Measure retrieval quality of substring matching against BM25.

The corpus, the queries, and the relevance judgements live in
``tools/retrieval_set.json`` and were written for this repository. That is the
whole caveat and it is not a small one: a number produced here compares two
ranking modes on a set its own author designed, so it says which mode finds
the entries *he* meant, and nothing about how Noetrail compares to another
system. The public memory benchmarks that such a comparison would need --
LoCoMo, LongMemEval, BEAM -- cannot be fetched or run in this build
environment, and no result is presented as if they had been.

What the set is good for is a regression: it fails visibly if a change to the
tokenizer or to the scoring quietly makes retrieval worse.

Reported per mode:

* Recall@k -- of the entries judged relevant, how many are in the first k
  results. Both modes return one page, so a relevant entry that ranks
  fifty-first is lost either way.
* MRR -- the reciprocal rank of the first relevant entry, averaged over
  queries. This is the metric that separates "found it" from "found it first",
  which is the difference an agent with a bounded context actually feels.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from noetrail.cli import run_command  # noqa: E402

DEFAULT_SET = Path(__file__).resolve().parent / "retrieval_set.json"
CUTOFFS = (1, 3, 5, 10)


def run(data: Path, config: Path, *arguments: str) -> object:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        run_command(
            [
                "--data-root",
                str(data),
                "--config-root",
                str(config),
                *arguments,
            ]
        )
    return json.loads(stream.getvalue())


def build_vault(data: Path, config: Path, entries: list[dict]) -> dict[str, str]:
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
    identifiers: dict[str, str] = {}
    for entry in entries:
        arguments = [
            "capture",
            "--type",
            str(entry.get("type", "note")),
            "--title",
            str(entry["title"]),
            "--text",
            str(entry["text"]),
        ]
        for tag in entry.get("tags", []):
            arguments.extend(["--tag", str(tag)])
        for alias in entry.get("aliases", []):
            arguments.extend(["--alias", str(alias)])
        created = run(data, config, *arguments)
        assert isinstance(created, dict)
        identifiers[str(entry["key"])] = str(created["id"])
    return identifiers


def evaluate(
    data: Path,
    config: Path,
    queries: list[dict],
    identifiers: dict[str, str],
    rank: str,
) -> dict[str, object]:
    per_query: list[dict[str, object]] = []
    recalls = {cutoff: 0.0 for cutoff in CUTOFFS}
    reciprocal = 0.0
    for item in queries:
        relevant = {identifiers[key] for key in item["relevant"]}
        response = run(
            data,
            config,
            "search",
            "--rank",
            rank,
            "--limit",
            str(max(CUTOFFS)),
            "--",
            str(item["query"]),
        )
        assert isinstance(response, dict)
        returned = [str(result["id"]) for result in response["items"]]
        found = {
            cutoff: len(relevant & set(returned[:cutoff])) / len(relevant)
            for cutoff in CUTOFFS
        }
        rank_of_first = next(
            (
                position
                for position, entry_id in enumerate(returned, start=1)
                if entry_id in relevant
            ),
            None,
        )
        for cutoff in CUTOFFS:
            recalls[cutoff] += found[cutoff]
        reciprocal += 1.0 / rank_of_first if rank_of_first else 0.0
        per_query.append(
            {
                "query": item["query"],
                "relevant": len(relevant),
                "total_matches": response["total"],
                "first_relevant_rank": rank_of_first,
                "recall": {str(cutoff): round(found[cutoff], 3) for cutoff in CUTOFFS},
            }
        )
    count = len(queries)
    return {
        "rank": rank,
        "queries": count,
        "recall_at": {
            str(cutoff): round(recalls[cutoff] / count, 3) for cutoff in CUTOFFS
        },
        "mrr": round(reciprocal / count, 3),
        "per_query": per_query,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="measure-retrieval")
    parser.add_argument("--set", dest="path", type=Path, default=DEFAULT_SET)
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="print the comparison table instead of the full JSON report",
    )
    arguments = parser.parse_args()

    definition = json.loads(arguments.path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="noetrail-retrieval-") as directory:
        base = Path(directory)
        data = base / "data"
        config = base / "config"
        data.mkdir()
        config.mkdir()
        identifiers = build_vault(data, config, definition["entries"])
        # Built once and left in place: the indexed and the scanned BM25 paths
        # are required to return the same order, so quality is a property of
        # the ranking, not of whether the cache happened to exist.
        run(data, config, "index", "rebuild")
        report = {
            "set": definition["name"],
            "entries": len(definition["entries"]),
            "modes": [
                evaluate(data, config, definition["queries"], identifiers, rank)
                for rank in ("substring", "bm25", "hybrid")
            ],
        }

    if not arguments.markdown:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    modes = report["modes"]
    assert isinstance(modes, list)
    print("| Mode | " + " | ".join(f"Recall@{k}" for k in CUTOFFS) + " | MRR |")
    print("| --- | " + " | ".join("---:" for _ in CUTOFFS) + " | ---: |")
    for mode in modes:
        recall = mode["recall_at"]
        print(
            f"| {mode['rank']} | "
            + " | ".join(f"{recall[str(k)]:.3f}" for k in CUTOFFS)
            + f" | {mode['mrr']:.3f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
