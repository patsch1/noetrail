"""The embedding interface: a file contract with no provider behind it.

What is being tested is a boundary, not a model. Noetrail must read vectors a
user produced elsewhere, must refuse the combinations that would produce a
confident but meaningless number, must degrade to lexical order rather than to
a wrong order, and must not acquire a way to send the vault anywhere.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from noetrail import cli, vectors
from noetrail.errors import InvalidRequest
from tests import temporary_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILTINS_ROOT = REPOSITORY_ROOT / ".knowledge"


class VectorFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.base / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_cosine_treats_a_zero_vector_as_no_information(self) -> None:
        self.assertEqual(vectors.cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        self.assertAlmostEqual(vectors.cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(vectors.cosine([1.0, 0.0], [-1.0, 0.0]), -1.0)

    def test_a_malformed_query_vector_is_a_caller_error(self) -> None:
        for text in (
            "not json",
            '{"format": "something-else", "model": "m", "dimensions": 2, '
            '"vector": [1, 0]}',
            '{"format": "noetrail-vectors-1", "dimensions": 2, "vector": [1, 0]}',
            '{"format": "noetrail-vectors-1", "model": "m", "dimensions": 2, '
            '"vector": [1, 0, 0]}',
            '{"format": "noetrail-vectors-1", "model": "m", "dimensions": 2, '
            '"vector": [1, "x"]}',
            '{"format": "noetrail-vectors-1", "model": "m", "dimensions": 2, '
            '"vector": [1, 1e400]}',
        ):
            with self.assertRaises(InvalidRequest):
                vectors.load_query_vector(self.write("q.json", text))
        with self.assertRaises(InvalidRequest):
            vectors.load_query_vector(self.base / "absent.json")

    def test_a_sidecar_from_another_model_is_refused_not_used(self) -> None:
        """Two vector spaces compared by cosine produce nonsense with a score.

        Refusing is the only safe answer: silently using them would look like
        a ranking, and a caller has no way to notice.
        """

        sidecar = self.write(
            "vectors.jsonl",
            json.dumps(
                {"format": vectors.VECTOR_FORMAT, "model": "a", "dimensions": 2}
            )
            + "\n",
        )
        query = self.write(
            "q.json",
            json.dumps(
                {
                    "format": vectors.VECTOR_FORMAT,
                    "model": "b",
                    "dimensions": 2,
                    "vector": [1.0, 0.0],
                }
            ),
        )
        with self.assertRaises(InvalidRequest) as raised:
            vectors.rerank([], sidecar=sidecar, query=query)
        self.assertIn("different models", str(raised.exception))

    def test_a_record_for_an_older_revision_is_ignored(self) -> None:
        entry = "kn_" + "0" * 32
        current = "sha256:" + "a" * 64
        older = "sha256:" + "b" * 64
        sidecar = self.write(
            "vectors.jsonl",
            "\n".join(
                [
                    json.dumps(
                        {
                            "format": vectors.VECTOR_FORMAT,
                            "model": "m",
                            "dimensions": 2,
                        }
                    ),
                    json.dumps(
                        {"id": entry, "revision": older, "vector": [1.0, 0.0]}
                    ),
                ]
            )
            + "\n",
        )
        query = self.write(
            "q.json",
            json.dumps(
                {
                    "format": vectors.VECTOR_FORMAT,
                    "model": "m",
                    "dimensions": 2,
                    "vector": [1.0, 0.0],
                }
            ),
        )
        ranked = vectors.rerank(
            [(0, entry, current, 3.5)], sidecar=sidecar, query=query
        )
        # Not reranked, and it keeps its lexical score rather than a cosine
        # computed from text the entry no longer holds.
        self.assertEqual(ranked, {0: (vectors.NOT_RERANKED, 3.5)})


class RerankedSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = temporary_root(self.temporary)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.assertEqual(self.run_cli("init"), 0)
        self.alpha = self.capture("Alpha", "basalt cadence in one line")
        self.beta = self.capture("Beta", "basalt basalt basalt everywhere")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> int:
        self.stdout = io.StringIO()
        with contextlib.redirect_stdout(self.stdout):
            return cli.run_command(
                [
                    "--data-root",
                    str(self.data),
                    "--config-root",
                    str(self.config),
                    "--builtins-root",
                    str(BUILTINS_ROOT),
                    *arguments,
                ]
            )

    def json_cli(self, *arguments: str) -> dict:
        self.assertEqual(self.run_cli(*arguments), 0, self.stdout.getvalue())
        return json.loads(self.stdout.getvalue())

    def capture(self, title: str, text: str) -> dict:
        return self.json_cli(
            "capture", "--type", "note", "--title", title, "--text", text
        )

    def sidecar(self, assignments: dict[str, list[float]]) -> Path:
        path = self.base / "vectors.jsonl"
        lines = [
            json.dumps(
                {"format": vectors.VECTOR_FORMAT, "model": "demo", "dimensions": 2}
            )
        ]
        for entry, vector in assignments.items():
            created = self.alpha if entry == "alpha" else self.beta
            lines.append(
                json.dumps(
                    {
                        "id": created["id"],
                        "revision": created["revision"],
                        "vector": vector,
                    }
                )
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def query_vector(self, vector: list[float]) -> Path:
        path = self.base / "query.json"
        path.write_text(
            json.dumps(
                {
                    "format": vectors.VECTOR_FORMAT,
                    "model": "demo",
                    "dimensions": 2,
                    "vector": vector,
                }
            ),
            encoding="utf-8",
        )
        return path

    def titles(self, *arguments: str) -> list[str]:
        return [
            str(item["title"])
            for item in self.json_cli("search", *arguments)["items"]
        ]

    def test_vectors_reorder_the_lexical_candidates(self) -> None:
        sidecar = self.sidecar({"alpha": [1.0, 0.0], "beta": [0.0, 1.0]})
        self.assertEqual(self.titles("--rank", "bm25", "basalt"), ["Beta", "Alpha"])
        self.assertEqual(
            self.titles(
                "--rank",
                "bm25",
                "--rerank-vectors",
                str(sidecar),
                "--query-vector",
                str(self.query_vector([1.0, 0.0])),
                "basalt",
            ),
            ["Alpha", "Beta"],
        )

    def test_vectors_never_add_a_candidate_the_words_did_not_match(self) -> None:
        """The limit that keeps a result explainable from the file itself."""

        sidecar = self.sidecar({"alpha": [1.0, 0.0], "beta": [0.0, 1.0]})
        results = self.json_cli(
            "search",
            "--rank",
            "bm25",
            "--rerank-vectors",
            str(sidecar),
            "--query-vector",
            str(self.query_vector([1.0, 0.0])),
            "cadence",
        )
        self.assertEqual(results["total"], 1)
        self.assertEqual(results["items"][0]["title"], "Alpha")

    def test_an_entry_without_a_vector_falls_back_to_its_lexical_rank(self) -> None:
        sidecar = self.sidecar({"alpha": [1.0, 0.0]})
        self.assertEqual(
            self.titles(
                "--rank",
                "bm25",
                "--rerank-vectors",
                str(sidecar),
                "--query-vector",
                str(self.query_vector([1.0, 0.0])),
                "basalt",
            ),
            ["Alpha", "Beta"],
        )

    def test_an_edited_entry_stops_being_reranked_until_vectors_are_redone(
        self,
    ) -> None:
        sidecar = self.sidecar({"alpha": [1.0, 0.0], "beta": [0.0, 1.0]})
        query = self.query_vector([1.0, 0.0])
        self.json_cli("tag", str(self.alpha["id"]), "changed")
        # Alpha's stored vector describes text that no longer exists, so it
        # drops out of the reranked tier and BM25 decides again.
        self.assertEqual(
            self.titles(
                "--rank",
                "bm25",
                "--rerank-vectors",
                str(sidecar),
                "--query-vector",
                str(query),
                "basalt",
            ),
            ["Beta", "Alpha"],
        )

    def test_the_arguments_are_refused_in_combinations_that_do_nothing(
        self,
    ) -> None:
        sidecar = self.sidecar({"alpha": [1.0, 0.0]})
        query = self.query_vector([1.0, 0.0])
        for arguments in (
            ("--rerank-vectors", str(sidecar)),
            ("--query-vector", str(query)),
        ):
            with self.assertRaises(InvalidRequest):
                self.run_cli("search", "--rank", "bm25", "basalt", *arguments)
        # Vectors reorder a ranking. A literal scan produces none, so passing
        # them there is an argument that would silently do nothing.
        with self.assertRaises(InvalidRequest):
            self.run_cli(
                "search",
                "--rank",
                "substring",
                "--rerank-vectors",
                str(sidecar),
                "--query-vector",
                str(query),
                "basalt",
            )
        # Both modes that do produce one accept them.
        for rank in ("bm25", "hybrid"):
            self.assertEqual(
                self.run_cli(
                    "search",
                    "--rank",
                    rank,
                    "--sort",
                    "relevance",
                    "--rerank-vectors",
                    str(sidecar),
                    "--query-vector",
                    str(query),
                    "basalt",
                ),
                0,
            )
        with self.assertRaises(InvalidRequest):
            self.run_cli(
                "search",
                "--rank",
                "bm25",
                "--sort",
                "title_asc",
                "--rerank-vectors",
                str(sidecar),
                "--query-vector",
                str(query),
                "basalt",
            )


if __name__ == "__main__":
    unittest.main()
