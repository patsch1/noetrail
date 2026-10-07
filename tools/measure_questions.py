"""Diagnose everyday queries, including lexical misses and absent answers.

This is a locally authored synthetic set, not an independent benchmark.
No relevance judgement is rejected merely because the current engine misses it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.measure_retrieval import build_vault, run  # noqa: E402 - script entry point

DEFAULT_SET = Path(__file__).with_name("question_set.json")


def build(data: Path, config: Path, definition: dict) -> dict[str, str]:
    identifiers = build_vault(data, config, definition["entries"])
    for relation in definition.get("relations", []):
        options = []
        for name in ("valid_from", "valid_until"):
            if name in relation:
                options.extend(["--" + name.replace("_", "-"), relation[name]])
        run(
            data,
            config,
            "relate",
            identifiers[relation["source"]],
            relation["predicate"],
            identifiers[relation["target"]],
            *options,
        )
    return identifiers


def summarize(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["relevant"]]
    absent = [r for r in rows if not r["relevant"]]
    returned = sum(r["returned"] for r in rows)
    return {
        "queries": len(rows),
        "answerable": len(answerable),
        "unanswerable": len(absent),
        "recall_at_5": (
            sum(r["recall_at_5"] for r in answerable) / len(answerable)
            if answerable
            else None
        ),
        "precision_at_5": (
            sum(r["hits"] for r in rows) / returned if returned else None
        ),
        "mrr_at_5": (
            sum(r["reciprocal_rank"] for r in answerable) / len(answerable)
            if answerable
            else None
        ),
        "correct_empty_rate": (
            sum(r["total"] == 0 for r in absent) / len(absent) if absent else None
        ),
        "irrelevant_results": sum(r["returned"] - r["hits"] for r in rows),
    }


def evaluate(
    data: Path, config: Path, definition: dict, identifiers: dict[str, str], rank: str
) -> dict:
    rows = []
    for question in definition["queries"]:
        options = ["--rank", rank, "--limit", "5"]
        for name, value in question.get("filters", {}).items():
            if name == "related_id":
                value = identifiers[value]
            options.extend(["--" + name.replace("_", "-"), value])
        response = run(data, config, "search", *options, "--", question["query"])
        relevant = {identifiers[key] for key in question["relevant"]}
        found = [item["id"] for item in response["items"]]
        hits = len(set(found) & relevant)
        first = next((i for i, key in enumerate(found, 1) if key in relevant), None)
        rows.append(
            {
                "query": question["query"],
                "category": question["category"],
                "relevant": len(relevant),
                "total": response["total"],
                "returned": len(found),
                "hits": hits,
                "recall_at_5": hits / len(relevant) if relevant else None,
                "reciprocal_rank": 1 / first if first else 0,
            }
        )
    return {
        "rank": rank,
        **summarize(rows),
        "categories": {
            category: summarize([r for r in rows if r["category"] == category])
            for category in sorted({r["category"] for r in rows})
        },
        "per_query": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=Path, default=DEFAULT_SET)
    args = parser.parse_args()
    definition = json.loads(args.set.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="noetrail-questions-") as temporary:
        base = Path(temporary)
        data, config = base / "data", base / "config"
        data.mkdir()
        config.mkdir()
        identifiers = build(data, config, definition)
        run(data, config, "index", "rebuild")
        report = {
            "set": definition["name"],
            "about": definition["about"],
            "modes": [
                evaluate(data, config, definition, identifiers, rank)
                for rank in ("substring", "bm25", "hybrid")
            ],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
