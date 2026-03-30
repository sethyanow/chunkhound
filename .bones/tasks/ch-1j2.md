---
id: ch-1j2
title: 'Phase 1 Acceptance: Foundation — LSP Client + Schema + Fixes'
status: open
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
CLI commands with observable outcomes (see below).

## Success Criteria
- [ ] CLAUDE.md updated: LSP client module location, config format, new DuckDB tables documented
- [ ] AGENTS.md updated: new tables, new module, key commands for LSP
- [ ] User has run walkthrough commands and confirmed observable outcomes
- [ ] No information in CLAUDE.md/AGENTS.md contradicts actual code state

## Walkthrough

### 1. LSP client connects to pyright and returns documentSymbol for a Python file

```bash
# Run the LSP client integration test that verifies pyright connection + documentSymbol
uv run pytest tests/test_lsp_client.py::test_document_symbols -v
```
**Expected:** Test passes — pyright spawns, initializes, returns symbols for fixture file.

### 2. LSP client connects to at least one non-Python language server

```bash
# Check the registry covers all tree-sitter languages
uv run pytest tests/test_lsp_client.py::test_registry_covers_all_tree_sitter_languages -v
```
**Expected:** Test passes — registry has entries for all 32 languages with tree-sitter grammars.

Note: Live connection tests beyond pyright require those language servers installed. The registry CONFIGS are verified; live connection requires the binaries on PATH.

### 3. DuckDB tables exist after fresh index

```bash
# Index a small directory, then verify tables
uv run chunkhound index chunkhound/lsp/ --db /tmp/ch-acceptance-test
python3 -c "
import duckdb
conn = duckdb.connect('/tmp/ch-acceptance-test/chunks.db')
for table in ['files', 'chunks', 'symbols', 'symbol_edges', 'schema_version']:
    count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    print(f'{table}: {count} rows')
version = conn.execute('SELECT MAX(version) as v FROM schema_version').fetchone()[0]
print(f'Schema version: {version}')
conn.close()
"
rm -rf /tmp/ch-acceptance-test
```
**Expected:** `symbols` and `symbol_edges` tables exist (0 rows — Phase 2 populates them). Schema version = 2.

### 4. Semantic search path scoping returns no cross-directory leakage

```bash
# Run the path scoping tests
uv run pytest tests/test_path_scoping.py -v 2>/dev/null || \
uv run pytest tests/ -k "path_scop" -v
```
**Expected:** Tests pass — prefix matching prevents cross-directory leakage.

## Anti-Patterns
- NO code changes in this task — documentation only
- NO checking off walkthrough criteria without actually running the commands
- NO inventing documentation claims — verify against actual code first

## Log

- [2026-03-30T15:21:14Z] [Seth] BLOCKED: Created prematurely — agent followed skill mechanics instead of addressing P0 ch-52s. User rejected. Do not execute until ch-52s is closed.
