---
id: ch-1j2
title: 'Phase 1 Acceptance: Foundation — LSP Client + Schema + Fixes'
status: closed
type: task
priority: 1
depends_on: [ch-52s]
parent: ch-7j0
---










## Context
Final task for Phase 1 (ch-7j0). All 3 implementation tasks closed:
- ch-jsj: Path scoping fix (prefix default) + git-aware indexing
- ch-u1s: LSP client manager (standalone async JSON-RPC, 32 language configs, pyright verified)
- ch-a58: DuckDB symbols + symbol_edges tables (schema v2, migration from v1)

This task has two deliverables: documentation updates and a user walkthrough proving the work.

## Requirements

### Agent Documentation
Update CLAUDE.md and AGENTS.md with Phase 1 deliverables:

**AGENTS.md additions:**
- `chunkhound/lsp/` module: standalone async LSP client (zero chunkhound imports)
  - `types.py`: ServerConfig, ServerState, LSPCapability, error hierarchy, response dataclasses
  - `protocol.py`: JsonRpcTransport — Content-Length framed async stdio JSON-RPC 2.0
  - `client.py`: LSPClient (lifecycle, state machine, capability gating, 8 operations) + LSPClientPool
  - `registry.py`: LANGUAGE_SERVER_REGISTRY — 32 language configs (ServerConfig per Language enum member)
- New DuckDB tables: `symbols` (15 cols) and `symbol_edges` (11 cols) in schema v2
- Path scoping: prefix match default, `fuzzy_path=True` param for substring opt-in
- Git-aware indexing: `SimpleEventHandler._should_index()` uses pygit2 to check git state

**Key commands to document:**
- `uv run pytest tests/test_lsp_client.py -v` — LSP client integration tests (requires pyright-langserver)
- `uv run pytest tests/test_schema_symbols.py -v` — schema v2 tests

### User Walkthrough
Reusable demo script (`scripts/demo_lsp.py`) that dogfoods Phase 1 deliverables against the live codebase.

## Success Criteria
- [x] CLAUDE.md updated: references AGENTS.md; no new content needed (derivable from code/bones per user direction)
- [x] AGENTS.md updated: fixed stale pre-push test command to include all 4 tiers
- [x] `scripts/demo_lsp.py` exists and runs successfully against this repo
- [x] User has run the demo script and confirmed observable outcomes
- [x] No information in CLAUDE.md/AGENTS.md contradicts actual code state

## Walkthrough

Single command:
```bash
uv run scripts/demo_lsp.py [FILE]
```

The script demos all Phase 1 deliverables against this codebase:

1. **LSP Client** — spawns pyright, connects, gets documentSymbol for the target file (default: `chunkhound/lsp/client.py`), prints symbols with kind/name/line-range in the same format as editor LSP
2. **Registry** — lists all 32 language configs, checks which servers are available on PATH
3. **Schema v2** — connects to the project's DuckDB, shows tables + row counts + schema version, confirms `symbols` and `symbol_edges` exist
4. **Path scoping** — runs a scoped regex search via the ChunkHound library and verifies no results leak outside the prefix

Reusable for future phases: Phase 2 extends to show populated symbol/edge counts, Phase 3 adds MCP tool comparison.

## Dogfooding Lessons

**Re-index required after schema changes.** The v2 migration (ch-a58) adds `symbols` and `symbol_edges` tables, but existing databases don't gain them until re-indexed. The demo script caught this — the live DB had no Phase 1 tables because it predates the migration. Users upgrading ChunkHound need to re-index to get v2 schema. Document this in AGENTS.md.

**DuckDB lock when MCP is running.** The demo script can't open the DB read-only while the MCP server holds a write lock. Solved by snapshotting to a temp file. This is a known concurrency limitation (see memory 55566).

**Semantic search path scoping leaks through MCP layer.** Discovered during demo — `search_semantic(path="chunkhound/lsp/")` returns results from `tests/test_lsp_client.py`. SQL LIKE prefix at DB level is clean; the leak is in the MCP tool routing. Tracked as ch-u62.

## Anti-Patterns
- NO inventing documentation claims — verify against actual code first
- NO checking off walkthrough criteria without actually running the script

## Log

- [2026-03-30T15:21:14Z] [Seth] BLOCKED: Created prematurely — agent followed skill mechanics instead of addressing P0 ch-52s. User rejected. Do not execute until ch-52s is closed.
- [2026-03-31T00:44:58Z] [Seth] lgtm
- [2026-03-31T00:46:26Z] [Seth] Debrief: Demo script approach replaced pytest walkthrough. Pyright needs didOpen before documentSymbol. DB snapshot workaround for MCP lock. Discovered ch-u62 (path leak). Reflections: user corrected agent-oriented walkthrough twice — both corrections saved to memory. Phase 1 complete, ch-7j0 closed.
