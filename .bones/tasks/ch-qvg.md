---
id: ch-qvg
title: Deploy TEI for jina-v5 in 3 configs (VPS Docker CPU / Modal CPU / Modal L4) and benchmark
status: open
type: task
priority: 1
depends_on: [ch-agj]
---

## Goal

Stand up Hugging Face Text Embeddings Inference (TEI) v1.9+ hosting `jina-embeddings-v5-text-nano` (and/or `-small`) in three deployment modes and benchmark startup time, request latency, and throughput. Pick a winner for ongoing pilot use.

## Three configurations to deploy

1. **VPS Docker CPU** — this AMD EPYC 16c / 62GB RAM box, no GPU
   - Stays warm, free, single-tenant
   - Slow on jina-v5-small (677M); jina-v5-nano (239M) more reasonable
2. **Modal CPU app** — `@modal.web_server` wrapping TEI
   - ~$0.13/hr when warm; scale-to-zero
   - 7-day I/O TTL on Modal storage (privacy caveat for private repos later)
   - Cold start ~10-30s expected
3. **Modal L4 GPU** — `@modal.web_server` wrapping TEI on L4
   - ~$0.80/hr; ~50ms latency
   - Scale-to-zero; cold start dominated by VRAM load
   - Overkill for nano, appropriate for small

## Deliverables

- `deploy/tei/vps-docker.yaml` (or wherever ChunkHound's deploy artifacts live; may not exist yet)
- `deploy/tei/modal_cpu.py` — Modal app file
- `deploy/tei/modal_gpu_l4.py` — Modal app file
- README with startup commands and expected URLs
- Benchmark table: cold-start, warm latency p50/p95, throughput (docs/sec) per config
- Recommendation: which config to use as default for ongoing pilot

## Decisions made

- TEI v1.9+ confirmed to support jina-v3/v5 task hint via OpenAI-compat `/v1/embeddings` `extra_body={"task": "retrieval.passage|query"}` (per ch-agj's research)
- Pin TEI image version to a specific tag (avoid `:latest`)
- Pin model weights via Modal Volume to avoid re-downloads on every cold start
- Set `HF_XET_HIGH_PERFORMANCE=1` for HF Hub downloads (per modal-llm-serving skill)
- Use ad-hoc benchmarks (curl + bash timing) initially; if signal is mixed, tighten to a Python harness later

## Out of scope

- Production-grade autoscaling/observability
- Dimensions tuning / Matryoshka truncation (post-pilot)
- Auth tokens (use TEI's `--api-key` only if exposed beyond localhost)
- Reranker deployment (separate task — ch-ycc)

## Blocked by

- ch-agj: `TEIEmbeddingProvider` must exist before this is usable end-to-end. Server can be deployed first, but benchmarking via ChunkHound waits for the client class.

## Anti-patterns to avoid

- DO NOT skip pinning model weights to a Volume — cold start dominated by HF download otherwise
- DO NOT bake HF tokens into images
- DO NOT include private repo content in benchmark queries — public OSS only (this chunkhound repo + a small fixture, per ch-02d)
