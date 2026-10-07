# Architecture decision records

One file per decision that would otherwise have to be reconstructed from a
diff: what the situation was, what was chosen, what it costs, and what was
rejected. A record is not updated when the decision changes — a new record
supersedes it, so the reasoning at the time stays readable.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-core-and-schema-packs.md) | A stable core envelope plus declarative, non-executable schema packs | accepted |
| [0002](0002-noetrail-name-and-compatibility.md) | The name Noetrail, and the compatibility surface the rename keeps | accepted |
| [0003](0003-in-process-reads.md) | Bounded MCP reads run in the server process; mutations keep a separate process | accepted |
| [0004](0004-no-search-index.md) | No search index until a measured trigger is reached | superseded by 0005 |
| [0005](0005-lexical-bm25-index.md) | A derived BM25 index as an opt-in cache, and an embedding interface with no shipped provider | accepted; its default-ranking decision superseded by 0007 |
| [0006](0006-relation-level-valid-time.md) | Valid time and supersession on the typed relation, not on the entry or the field | accepted |
| [0007](0007-hybrid-retrieval-default.md) | Both text predicates run by default, ranked by BM25 | accepted; empty-result behavior extended by 0008 |
| [0008](0008-word-form-fallback.md) | Labelled word-form candidates only after an empty filtered hybrid search | accepted |

Measurements referenced by 0003, 0004 and 0005 are reproducible with
`tools/measure_search.py`, and the retrieval-quality numbers in 0005 with
`tools/measure_retrieval.py`. Both are recorded in
[Limits and scaling](../limits.md).
The retrospective record of how these decisions were reached is in
[the decision log](../internal/decision-log.md).
