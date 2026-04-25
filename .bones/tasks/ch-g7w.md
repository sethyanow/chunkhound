---
id: ch-g7w
title: Add .chunkhound.json template for TEI + jina-v5 with rerank pass-through
status: open
type: task
priority: 2
depends_on: [ch-qvg]
---

## Goal

Commit a documented example `.chunkhound.json` template demonstrating TEI provider configuration with jina-v5, including rerank pass-through (Voyage today, Modal Qwen3 once ch-ycc lands).

## Deliverables

- `docs/configs/tei-jina-v5.example.json` (or matching ChunkHound docs convention — verify path during work)
- README section or inline comments explaining each field
- Documented variants: nano vs small, with/without Voyage rerank pass-through

## Shape (fields confirmed via ch-agj)

```
{
  "embedding": {
    "provider": "tei",
    "model": "jinaai/jina-embeddings-v5-text-nano",   // or -small
    "base_url": "http://localhost:8080/v1",            // TEI server URL (from ch-qvg winner)
    "dims": 512,                                       // model-dependent
    "rerank_format": "voyageai",                       // or "tei" once ch-ycc lands
    "rerank_model": "rerank-2.5"
  },
  "database": { "path": ".chunkhound" }
}
```

## Decisions made

- Template is documentation, NOT a runtime config — committed under `docs/`, not at repo root
- Show two variants (nano, small) so users can pick based on hardware
- Rerank stays Voyage by default for public/non-sensitive repos; Modal Qwen3 once ch-ycc lands
- Confirmed via ch-agj: schema accepts `provider="tei"`, base_url required, dims required

## Out of scope

- Runtime config for THIS chunkhound repo (stays Voyage — see ch-agj's "don't disturb" guarantee)
- Auth-token wiring (separate concern; TEI deployments handle their own auth)
- Multi-provider switching tooling

## Blocked by

- ch-agj: schema must accept `provider="tei"` first
- ch-qvg: need a known-good TEI URL pattern to document

## Anti-patterns to avoid

- DO NOT commit a real API key in the template
- DO NOT bake the user's specific TEI URL — use placeholders documented as such
- DO NOT skip the dims field — TEI doesn't auto-discover it
