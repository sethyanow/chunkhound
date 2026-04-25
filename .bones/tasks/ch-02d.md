---
id: ch-02d
title: Benchmark jina-v5-nano vs small vs voyage-4 on chunkhound repo + todomvc fixture
status: open
type: task
priority: 2
depends_on: [ch-qvg, ch-g7w]
---

## Goal

Empirical quality comparison: jina-v5-text-nano vs jina-v5-text-small vs voyage-4 (current default) on retrieval quality across two public corpora. Goal is signal — does jina-v5 measurably hold up against voyage on real codebases? — not a publication-grade benchmark.

## Method

- **Public repos only** — no private repos in this workflow (user direction)
- **Corpus 1**: this chunkhound repo (already indexed with voyage; preserve via DB folder swap)
- **Corpus 2**: a small TodoMVC variant (or equivalent compact JS/TS repo) — fetch as a test fixture
- **DB folder swap pattern**: each (corpus × provider) combo gets its own `.chunkhound/` folder. Swap symlink/folder name between runs. Preserves indexing work; no re-indexing on switch.

## Workflow

1. Pull the TodoMVC fixture into a known location (e.g., `tests/fixtures/todomvc-vanilla/`)
2. Index each corpus with each provider:
   - chunkhound × voyage-4 (already done — preserve)
   - chunkhound × jina-v5-nano (new index, separate DB folder)
   - chunkhound × jina-v5-small (new index, separate DB folder)
   - todomvc × voyage-4 (new)
   - todomvc × jina-v5-nano (new)
   - todomvc × jina-v5-small (new)
3. Define a fixed query set (~10-20 queries per corpus, mix of literal/semantic/conceptual)
4. Run each query against each (corpus, provider) DB; capture top-5 results
5. Manual eyeball comparison: which provider returned more relevant results per query
6. Document findings in a markdown report under `docs/benchmarks/`

## Deliverables

- `tests/fixtures/todomvc-vanilla/` (fixture repo, gitignored content but checked-in download script)
- `scripts/bench_swap_db.sh` — utility to swap which DB folder is "active" for a corpus
- `scripts/bench_run_queries.sh` — utility to run the fixed query set
- `docs/benchmarks/jina-vs-voyage-2026-04.md` — findings report

## Decisions made

- Method: ad-hoc spot-check (10-20 queries, eyeball top-5) — KISS, signal-rich for pilot
- Per user: NO private repos in this task; public OSS only
- DB folder swap chosen over re-indexing — voyage indexing for this repo took meaningful time, don't redo
- Voyage rerank stays in the loop (`rerank-2.5`) for all configs — fair comparison since rerank is the same downstream stage
- TodoMVC chosen over more-complex fixtures — small, well-known, polyglot enough to stress jina's multilingual retrieval

## Out of scope

- Formal NDCG@10 / MRR@10 metrics with ground-truth labels
- Latency benchmarks (covered in ch-qvg)
- Cross-language retrieval beyond what TodoMVC variants offer
- Rerank model comparison (separate concern — ch-ycc)

## Blocked by

- ch-agj: TEIEmbeddingProvider must exist
- ch-qvg: TEI server must be running with one of the three configs
- ch-g7w: config template needed to point ChunkHound at TEI

## Anti-patterns to avoid

- DO NOT use private repos as a benchmark corpus
- DO NOT delete the existing voyage `.chunkhound/` for this repo before confirming the swap pattern works
- DO NOT spend time on a formal harness before knowing whether jina is a contender — eyeball first
- DO NOT index with rerank disabled "for fairness" — the user's actual use case includes rerank
