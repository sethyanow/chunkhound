---
id: ch-ycc
title: Deploy Qwen3-Reranker-4B on Modal T4 and Together AI dedicated, compare side by side
status: open
type: task
priority: 2
---

## Goal

Deploy `Qwen3-Reranker-4B` via TEI v1.9+ on two hosts (Modal T4 and Together AI dedicated) and compare on cold-start latency, warm latency, cost, and privacy posture. The Together AI side is the privacy-conscious option for IP-sensitive repos; Modal is the lower-friction option per last session's architecture target.

## Why two hosts

- **Modal T4** — last session's architecture target. ~$0.59/hr base, fast cold start, scale-to-zero with `scaledown_window=300`. 7-day I/O TTL on Modal control plane storage (the privacy concern that emerged this session)
- **Together AI dedicated** — perplexity research surfaced: zero-retention option, AWS VPC peering, SOC 2 Type 2 compliant. Higher cost. Better fit for genuinely IP-sensitive repos
- Comparing both lets the user pick per-repo: public/non-sensitive → Modal; IP-sensitive private → Together AI

## Deliverables

- `deploy/rerank/modal_t4.py` — Modal app for Qwen3-Reranker-4B on T4
- `deploy/rerank/together_dedicated.md` — Together AI deployment instructions (their workflow is web-console + API, not code-deployable)
- Benchmark table: cold start, warm latency p50/p95, $/1k requests, data retention guarantees
- Both expose OpenAI-compatible `/rerank` endpoint via TEI's HTTP API

## ChunkHound-side wiring (already supported)

`openai_provider.py:1235+` already implements `rerank_via_http` with `rerank_format="tei"` support. Switching from Voyage rerank to a Modal/Together rerank is config-only:

```
"rerank_format": "tei",
"rerank_url": "https://<deploy>.modal.run/rerank"
```

No code changes required — existing rerank-via-HTTP path handles TEI format. Confirmed via ch-agj's codebase verification.

## Decisions made

- Two-host comparison instead of picking one upfront (per user direction)
- Qwen3-Reranker-4B over -8B per last session's research: faster cold start outweighs marginal quality at final ranking stage
- TEI v1.9+ confirmed to fully support Qwen3-Reranker (not just Qwen3-Embedding)
- Pin model weights to Modal Volume / Together storage to avoid re-downloads on cold start
- Scale-to-zero on Modal; Together AI dedicated may not have scale-to-zero — note in comparison

## Out of scope

- Embedding deploy (separate — ch-qvg)
- ChunkHound-side rerank code changes (none needed — config only)
- Production observability (Prometheus, OTEL) — basic logs only for pilot

## Blocked by

- Nothing strictly. Independent of ch-agj, can run in parallel with the embedding-side chain (A → B → C)

## Anti-patterns to avoid

- DO NOT commit Modal/Together API tokens
- DO NOT skip pinning Qwen3-Reranker weights to durable storage (cold start dominated by ~8GB FP16 weight load otherwise)
- DO NOT lock in one host before benchmarking — privacy posture vs. cost is a real tradeoff
- DO NOT include private repo content in benchmark queries — public OSS only
