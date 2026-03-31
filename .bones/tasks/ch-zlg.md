---
id: ch-zlg
title: 'Edge population: definition/references/calls → symbol_edges'
status: closed
type: task
priority: 1
owner: Seth
parent: ch-0um
---






## Context
Third task for Phase 2 (ch-0um). ch-5b3 delivered documentSymbol → symbols, ch-nvc delivered hover → type_signature. This task populates the `symbol_edges` table using 5 LSP operations per symbol.

**Blocked by:** ch-nvc (closed)
**Unlocks:** Remaining Phase 2 criteria (workspaceSymbol, incremental, multi-language), and graph tools in Phase 3 that query `symbol_edges`.

## Requirements
From parent epic R3, scoped to edge operations:
- `definition`, `references`, `implementation`, `incomingCalls`, `outgoingCalls` called per symbol → edges written to `symbol_edges` with correct edge_kind
- Edge failures for individual symbols must not block the file's other symbols
- Edges reference symbol IDs via FK — both ends must exist in `symbols` table
- Edge deduplication on (from_fqn, to_fqn, edge_kind) — same edge discoverable from both ends

## Design

### Symbol ID resolution: `_resolve_symbol`

New private method on `LSPPopulationService`:
- Signature: `def _resolve_symbol(self, uri: str, line: int) -> tuple[int, str, str] | None`
- Converts URI to relative file path (strip workspace_root prefix)
- Queries: `SELECT id, fqn, file_path FROM symbols WHERE file_path = ? AND range_start <= ? AND range_end >= ? ORDER BY (range_end - range_start) ASC LIMIT 1`
- ORDER BY range size ascending picks the most specific (innermost) symbol at that line
- Returns `(symbol_id, fqn, file_path)` or None if target not indexed

### Edge collection: `_collect_edges`

New async method on `LSPPopulationService`:
- Signature: `async def _collect_edges(self, client: LSPClient, uri: str, symbols: list[SymbolInfo], file_path: str, fqn_to_id: dict[str, int]) -> list[tuple]`
- `fqn_to_id`: mapping of `fqn → symbol_id` for the current file's just-inserted symbols
- Capability gate per operation type (DEFINITION, REFERENCES, IMPLEMENTATION, CALL_HIERARCHY) — skip missing capabilities
- Recursively walks symbols via `_edges_recursive` (same pattern as `_hover_recursive`)
- For each symbol: builds FQN (same logic as `_flatten_symbols`), looks up `from_symbol_id` from `fqn_to_id`
- Calls each LSP operation. Return types differ:
  - `go_to_definition`, `find_references`, `go_to_implementation` return `list[Location]` (`.uri`, `.range_start_line`)
  - `incoming_calls`, `outgoing_calls` return `list[CallHierarchyItem]` (`.uri`, `.range_start_line`, plus `.name`, `.kind`)
  - Both types share the fields needed by `_resolve_symbol`
- For each result:
  - Skip self-edges (from_fqn == resolved to_fqn and same edge_kind)
  - `_resolve_symbol(result.uri, result.range_start_line)` → `(to_symbol_id, to_fqn, to_file)` or skip
  - Create edge tuple: `(from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server)`
- Deduplication: collect into a dict keyed by `(from_fqn, to_fqn, edge_kind)`, last-write wins
- Returns edge tuples for batch insert

### Edge kind mapping

| LSP Operation | edge_kind | from → to |
|---------------|-----------|-----------|
| go_to_definition | `defines` | symbol → its definition location |
| find_references | `references` | symbol → each reference location |
| go_to_implementation | `implements` | symbol → each implementation |
| incoming_calls | `called_by` | symbol → each caller |
| outgoing_calls | `calls` | symbol → each callee |

### Wiring into `populate_file`

**Restructured flow** — current code runs delete + insert after didClose, but edge collection needs the file open AND symbol IDs from the DB. Move DB writes into the try block:

```
try:
    didOpen → documentSymbol → hover             # (existing)
    delete_file_edges(file_id)                    # NEW: edges before symbols
    delete_file_symbols(file_id)                  # (existing, moved earlier)
    flatten → _batch_insert(rows)                 # (existing, moved earlier)
    fqn_to_id = query_fqn_to_id(file_id)         # NEW: SELECT id, fqn FROM symbols WHERE file_id = ?
    edges = _collect_edges(client, uri, symbols, file_path, fqn_to_id)  # NEW
finally:
    didClose
if edges:
    _batch_insert_edges(edges)                    # NEW: DB-only, safe after didClose
```

Key constraint: `_collect_edges` calls LSP operations (needs file open) AND uses `fqn_to_id` (needs symbols inserted). Both requirements satisfied by this ordering. `_batch_insert_edges` is a pure DB write — safe after didClose.

Exception safety: if `_batch_insert` succeeds but `_collect_edges` throws, symbols are populated but edges are missing. Next `populate_file` call will clean up (delete + re-insert). Idempotent by design.

### Batch insert for edges: `_batch_insert_edges`

New method, same pattern as `_batch_insert`:
- 9-column tuples: `(from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server)`
- Single INSERT INTO symbol_edges

### Incremental cleanup: `delete_file_edges`

- Signature: `async def delete_file_edges(self, file_id: int) -> None`
- Query: `DELETE FROM symbol_edges WHERE from_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?) OR to_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?)`
- Called before `delete_file_symbols` in repopulation path (edges reference symbol IDs)

## Implementation

1. Write test: `_resolve_symbol` finds a symbol by URI + line in the symbols table
2. Write test: `_resolve_symbol` returns None when target not in DB (file not indexed)
3. Implement `_resolve_symbol` — URI→path conversion + DB query
4. Write test: `_collect_edges` calls definition/references per symbol, returns edge tuples with correct edge_kind
5. Write test: edge collection skips operations whose capability is missing (e.g., no CALL_HIERARCHY → no calls/called_by edges, but definition/references still collected)
6. Write test: edge failure on one symbol doesn't block others (broad Exception catch per symbol)
7. Implement `_collect_edges` and `_edges_recursive`
8. Write test: `populate_file` stores edges in `symbol_edges` table with correct from/to symbol_ids, fqns, and edge_kind
9. Wire into `populate_file`: query back symbol IDs → collect edges → batch insert edges
10. Implement `_batch_insert_edges`
11. Write test: `delete_file_edges` removes edges for a file's symbols
12. Implement `delete_file_edges`, wire into `delete_file_symbols` (edges first, then symbols)
13. Write test: edge deduplication — same edge from both ends produces one row
14. Verify existing tests still pass (no regression)

## Success Criteria
- [x] `go_to_definition` → edge with kind `defines` linking symbol to its definition
- [x] `find_references` → edges with kind `references` for each reference location
- [x] `go_to_implementation` → edges with kind `implements`
- [x] `incoming_calls` → edges with kind `called_by`
- [x] `outgoing_calls` → edges with kind `calls`
- [x] Edge deduplication on (from_fqn, to_fqn, edge_kind)
- [x] Missing capability → that operation skipped, others still run
- [x] Target symbol not in DB → edge skipped (no crash, no orphan FK violation)
- [x] `_resolve_symbol` picks innermost (most specific) symbol at a line
- [x] `_resolve_symbol` returns None for non-file:// URIs (stdlib, virtual files)
- [x] Self-edges filtered out (from_fqn == to_fqn with same edge_kind)
- [x] `delete_file_edges` removes edges referencing a file's symbols
- [x] All existing tests pass (zero regression)
- [x] `uv run pytest tests/test_lsp_population.py -v` → all pass

## Key Considerations

### `_resolve_symbol` — URI handling
- Only accept `file://` scheme URIs. Return None for `untitled:`, `jar:`, or any non-file scheme.
- Use `urllib.parse.unquote` for percent-encoded paths before stripping workspace_root.
- If relative path doesn't start within workspace (stdlib, third-party), return None early — don't query.

### `_resolve_symbol` — line number basis
- Verify in first TDD cycle that line numbers from `go_to_definition` results use the same 0-based indexing as `symbols.range_start`/`range_end` stored by `_flatten_symbols`. Off-by-one here silently drops all edges or links to wrong symbols.

### `_collect_edges` — multiple results per operation
- `go_to_definition` returns `list[Location]` — may have multiple locations (overloads, re-exports). Create an edge for each.
- `find_references` can return hundreds of locations for common symbols. All become edges. This is correct but produces high edge volume.

### `_collect_edges` — FQN collision for overloads
- Languages with overloading (TypeScript, C++) may produce multiple symbols with the same FQN. `fqn_to_id` dict last-write-wins is acceptable — edges from either overload point to a valid symbol_id with the correct FQN.

### Cross-file asymmetry during initial indexing
- During full-index, files are populated in arbitrary order. File A→B edges may be dropped if File B isn't populated yet (`_resolve_symbol` returns None). Graph is eventually consistent after full re-index but may have asymmetric edges on single pass. This is inherent to per-file population and addressed by the Phase 2 `workspaceSymbol("")` criterion (separate task).

### LSP call volume
- 5 operations × N symbols per file. For a 500-symbol file, ~2,500 round-trips. Same O(n) pattern as existing `_hover_recursive`. Performance optimization (batching, parallelism) is a separate concern.

## Anti-Patterns
- NO edge inserts with NULL symbol_id — FK constraint will reject; skip if resolution fails
- NO per-edge INSERT — batch all edges for a file in one statement
- NO edge collection after didClose — LSP operations need the file open
- NO blocking on edge failures — log and skip per symbol, same pattern as hover
- NO trusting that target symbols exist — always resolve via `_resolve_symbol`, skip if None
- NO deleting symbols before edges — edges reference symbol IDs; delete edges first

## Log

- [2026-03-31T13:24:21Z] [Seth] Debrief: Implementation followed skeleton exactly after SRE restructure of populate_file flow. No workarounds introduced. lsp_server field resolved via DB query (lightweight, acceptable). 38 tests pass (17 new). Reflections: SRE caught genuinely impossible ordering in wiring section — validates fresh-session review mandate. FQN construction duplication (_flatten_symbols + _edges_recursive) logged on epic as future refactor candidate. Scoped ch-5a3 as final Phase 2 task covering workspaceSymbol, incremental, multi-language, confidence.
