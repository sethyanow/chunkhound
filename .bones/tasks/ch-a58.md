---
id: ch-a58
title: DuckDB symbols + symbol_edges schema
status: open
type: task
priority: 1
parent: ch-7j0
---



## Context
Phase 1 of ch-8e7 (LSP + Graph Intelligence). Third implementation task after ch-jsj (infra fixes) and ch-u1s (LSP client manager). Delivers the DuckDB schema that Phase 2 (index population) writes into and Phase 3/4 (MCP tools, fusion tools) queries.

**Blocked by:** ch-u1s (closed), ch-jsj (closed)
**Unlocks:** Phase 1 acceptance task (docs + walkthrough), then Phase 2 (ch-0um: background index population)

## Requirements
From parent epic R2: "DuckDB schema additions — `symbols` table (fqn, name, kind, language, file_id, file_path, range_start, range_end, type_signature, parent_fqn, confidence, lsp_server) and `symbol_edges` table (from/to symbol_id + fqn + file, edge_kind, confidence, lsp_server). Symbol-to-chunk resolution via file_id + line range overlap, no FK."

Key constraints:
- No FK between symbols↔chunks (resolution by file_id + line range overlap)
- file_id in symbols references files(id) — same FK pattern as chunks
- symbol_edges references symbols(id) for both from/to
- Denormalized fqn+file on edges for join-free queries
- Additive only — NO changes to existing tables (anti-pattern)

## Design
**Tables added to `_executor_create_schema` in `duckdb_provider.py:416`:**

`symbols`: id (PK, sequence), fqn (TEXT NOT NULL), name (TEXT NOT NULL), kind (TEXT NOT NULL), language (TEXT), file_id (INTEGER REFERENCES files(id)), file_path (TEXT), range_start (INTEGER), range_end (INTEGER), type_signature (TEXT), parent_fqn (TEXT), confidence (FLOAT DEFAULT 1.0), lsp_server (TEXT), created_at, updated_at.

`symbol_edges`: id (PK, sequence), from_symbol_id (INTEGER NOT NULL REFERENCES symbols(id)), from_fqn (TEXT), from_file (TEXT), to_symbol_id (INTEGER NOT NULL REFERENCES symbols(id)), to_fqn (TEXT), to_file (TEXT), edge_kind (TEXT NOT NULL), confidence (FLOAT DEFAULT 1.0), lsp_server (TEXT), created_at.

**Indexes added to `_executor_create_indexes` at `duckdb_provider.py:661`:**
- symbols: fqn, file_id, file_path, kind
- symbol_edges: from_symbol_id, to_symbol_id, edge_kind, from_fqn, to_fqn

**Schema version:** Bump from 1→2. Fresh DBs stamp v2. Existing v1 DBs get additive tables via CREATE IF NOT EXISTS + version bump in `_executor_migrate_schema`.

## Implementation

### Step 1: Write failing test — symbols table schema
File: `tests/test_schema_symbols.py` (new)
Test: `test_symbols_table_created_on_connect` — connect DuckDBProvider to temp DB, verify `symbols` table exists with all R2 columns via `DESCRIBE symbols`. Assert column names and types.

### Step 2: Write failing test — symbol_edges table schema
Same file. Test: `test_symbol_edges_table_created_on_connect` — verify `symbol_edges` with all R2 columns. Assert FK references, NOT NULL constraints.

### Step 3: Write failing test — indexes exist
Test: `test_symbol_indexes_created` — after connect, verify indexes on: symbols(fqn, file_id, file_path, kind), symbol_edges(from_symbol_id, to_symbol_id, edge_kind). Query `duckdb_indexes()`.

### Step 4: Write failing test — schema version
Test: `test_schema_version_is_2` — fresh DB → version 2. Test: `test_v1_db_migrated_to_v2` — create v1 DB (files+chunks only), reconnect, verify symbols exist + version = 2.

### Step 5: Run tests — confirm failures
Command: `uv run pytest tests/test_schema_symbols.py -v`
Expected: all fail (tables don't exist)

### Step 6: Implement symbols table DDL
File: `chunkhound/providers/database/duckdb_provider.py`, `_executor_create_schema` after chunks block (~L474).
Add: `CREATE SEQUENCE IF NOT EXISTS symbols_id_seq` + `CREATE TABLE IF NOT EXISTS symbols(...)`.

### Step 7: Implement symbol_edges table DDL
Same method, after symbols. Add: `CREATE SEQUENCE IF NOT EXISTS symbol_edges_id_seq` + `CREATE TABLE IF NOT EXISTS symbol_edges(...)`.

### Step 8: Implement indexes
`_executor_create_indexes` after chunk indexes (~L682). Add all symbol/edge indexes.

### Step 9: Bump schema version
Fresh DBs: stamp v2. Existing v1: add version bump in `_executor_migrate_schema` after checking `_get_schema_version < 2`.

### Step 10: Run tests — confirm pass
Command: `uv run pytest tests/test_schema_symbols.py -v`

### Step 11: Smoke test — zero regression
Command: `uv run pytest tests/test_smoke.py -v -n auto`

### Step 12: Commit and push

## Success Criteria
- [ ] `symbols` table created with all R2 columns, correct types, file_id FK to files
- [ ] `symbol_edges` table created with all R2 columns, from/to symbol FKs
- [ ] Indexes on symbols(fqn, file_id, file_path, kind) and symbol_edges(from_symbol_id, to_symbol_id, edge_kind, from_fqn, to_fqn)
- [ ] Schema version = 2 on fresh DB
- [ ] Existing v1 DBs gain new tables on reconnect + version bumped to 2
- [ ] All existing tests pass (zero regression)
- [ ] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Anti-Patterns
- NO changes to existing tables (files, chunks, embeddings) — additive only
- NO FK between symbols↔chunks — resolution by file_id + line range overlap
- NO blocking I/O — schema DDL runs in executor thread (existing pattern)

## Log

- [2026-03-30T12:35:38Z] [Seth] Task scoped from hot context after ch-u1s closure. Covers ch-7j0 criteria 6-8 (symbols table, symbol_edges table, schema migration). Codebase verified: _executor_create_schema at L416, _executor_create_indexes at L661, schema version currently 1. Purely additive — no existing table changes.
