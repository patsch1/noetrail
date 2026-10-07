# Embeddings: an interface, not a provider

Noetrail computes no embeddings. It ships no model, has no optional extra that
would install one, and makes no network call for one. What it defines is the
shape of a file that a provider you run writes, and a reranking step that reads
it.

This page defines the sidecar format. Noetrail keeps provider selection and
execution outside the vault runtime for three reasons:

- **Dependency boundary.** An embedding provider introduces its own libraries,
  model files, or service requirements. The core Python package stays usable
  with the standard library alone.
- **Data-only packs.** Schema packs define attributes and validation; installing
  a pack cannot execute a provider or other code. See
  [ADR 0001](adr/0001-core-and-schema-packs.md).
- **Explicit data handling.** You choose whether and where a provider processes
  vault text. A local-first vault does not automatically make a cloud-backed
  provider or AI client private. See [Privacy](privacy.md).

Vector ranking can change with a model version. Supplying vectors explicitly
keeps that dependency separate from reproducible lexical retrieval over the
Markdown files. The design rationale is in
[ADR 0005](adr/0005-lexical-bm25-index.md).

## What the interface can and cannot do

Vectors **reorder** the entries BM25 already matched. They never add one.

That is a real limit, and it is deliberate. It means semantic reranking cannot
surface an entry that shares no word with your query, and it means every result
is still explainable by words that are in the file. If you need recall beyond
the lexical candidates, reformulate or broaden the query. The `--bm25-b` parameter changes length
normalization; it does not supply missing semantic matches.

## Sidecar format

UTF-8, one JSON object per line, conventionally `vectors.jsonl`. The first line
is a header; every following line is one entry.

```
{"format": "noetrail-vectors-1", "model": "<label>", "dimensions": 384}
{"id": "kn_0123...", "revision": "sha256:ab12...", "vector": [0.013, -0.221, ...]}
{"id": "kn_4567...", "revision": "sha256:cd34...", "vector": [0.004,  0.187, ...]}
```

| Field | Meaning |
| --- | --- |
| `format` | must be `noetrail-vectors-1` |
| `model` | an opaque label you choose; Noetrail only compares it for equality |
| `dimensions` | vector length; every record must match it |
| `id` | the entry's stable `kn_` identifier |
| `revision` | the `sha256:` revision the vector was computed from |
| `vector` | array of finite numbers |

The query vector is a single JSON object with the same header fields plus its
own `vector`:

```json
{"format": "noetrail-vectors-1", "model": "<label>", "dimensions": 384,
 "vector": [0.031, -0.004, "..."]}
```

## Rules Noetrail enforces

**A mismatched `model` is refused, not used.** Cosine similarity between
vectors from two different models is a confident-looking number with no
meaning, and a caller has no way to notice. Different `model` or different
`dimensions` fails the command.

**A stale `revision` is skipped, not used.** If the entry's current revision
differs from the one recorded next to the vector, the entry keeps its lexical
rank and is not reranked. A vector computed from text the entry no longer holds
is a wrong answer, not an old one. Re-run your provider for the changed entries
and rewrite their lines.

**Entries without a usable vector sort after the reranked ones,** in BM25
order. With complete coverage — what a provider run produces — that second
group is empty.

**Nothing is discovered implicitly.** Both files are named on the command line.
Noetrail reads no vector file you did not point it at, and writes none.

## Using it

<!-- docs-check: skip - needs a vector file this repository does not ship -->
```sh
noetrail search "how do I fix bitter espresso" \
  --rank bm25 \
  --rerank-vectors ~/vectors/vault.jsonl \
  --query-vector /tmp/query.json
```

`--rerank-vectors` and `--query-vector` must be given together, require a
ranking mode — `--rank hybrid`, the default, or `--rank bm25` — and only apply
to the relevance order. Passing them with `--rank substring`, which produces
no ranking to reorder, or with `--sort title_asc` is refused rather than
silently ignored.

Reranking makes search hash every candidate entry, because that is how the
`revision` check is done. On a broad query over a large vault that is a real
cost; it is paid only when the two arguments are present.

## Writing a provider

A provider is any program you trust with your vault. It needs three things,
none of which require Noetrail internals:

1. **The entries.** `noetrail search "" --limit 50 --offset N` pages through
   every active entry with its `id` and `revision`; `noetrail review <id>`
   returns one entry's body. Reading the Markdown directly is equally valid —
   that is the point of the format.
2. **A model.** Yours, running wherever you decide. Noetrail has no opinion
   beyond the `model` label being stable while a sidecar is in use.
3. **Incremental updates.** Compare each entry's current `revision` against the
   one in your sidecar and recompute only the entries that moved. `noetrail
   search ""` returns revisions for exactly this.

A sensible provider keeps the sidecar sorted by `id`, rewrites it atomically,
and stores it outside the data root — the same boundary the search index uses,
for the same reason: a backup of the vault should not contain a derived file
that a restore could resurrect.

## Related

- [Limits and scaling](limits.md) — measured BM25 and index numbers
- [ADR 0005](adr/0005-lexical-bm25-index.md) — why the index exists and why no
  provider ships with it
- [Privacy](privacy.md) — what leaves the machine, and what does not
