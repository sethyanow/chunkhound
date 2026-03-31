---
id: ch-zlg
title: 'Edge population: definition/references/calls → symbol_edges'
status: open
type: task
priority: 1
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
- Signature: `async def _collect_edges(self, client: LSPClient, uri: str, symbols: list[SymbolInfo], file_path: str, symbol_id_map: dict[tuple[int, int], int]) -> list[tuple]`
- `symbol_id_map`: mapping of `(range_start_line, range_start_char) → symbol_id` for the current file's just-inserted symbols
- Capability gate per operation type (DEFINITION, REFERENCES, IMPLEMENTATION, CALL_HIERARCHY) — skip missing capabilities
- Recursively walks symbols via `_edges_recursive` (same pattern as `_hover_recursive`)
- For each symbol: looks up `from_symbol_id` from `symbol_id_map`
- Calls each LSP operation, for each result:
  - `_resolve_symbol(result.uri, result.range_start_line)` → `(to_symbol_id, to_fqn, to_file)` or skip
  - Create edge tuple: `(from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server)`
- Deduplication: collect into a set keyed by `(from_fqn, to_fqn, edge_kind)`, last-write wins
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

After `_batch_insert(rows)`:
1. Query back inserted symbol IDs: `SELECT id, range_start, range_end FROM symbols WHERE file_id = ?`
2. Build `symbol_id_map: dict[tuple[int, int], int]` from `(range_start, range_end) → id`

Wait — the symbols table stores `range_start` (line) and `range_end` (line), but the map key should be `(range_start_line, range_start_char)` to match SymbolInfo. However, `range_start_char` isn't stored in the symbols table. Use `(range_start, fqn) → id` instead — FQN is unique per file.

Revised: Build `fqn_to_id: dict[str, int]` from `SELECT id, fqn FROM symbols WHERE file_id = ?`. Then `_collect_edges` uses FQN lookup for `from_symbol_id`.

3. Call `_collect_edges(client, uri, symbols, file_path, fqn_to_id)` inside the try block (file still open)
4. Call `_batch_insert_edges(edges)` after didClose

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
- [ ] `go_to_definition` → edge with kind `defines` linking symbol to its definition
- [ ] `find_references` → edges with kind `references` for each reference location
- [ ] `go_to_implementation` → edges with kind `implements`
- [ ] `incoming_calls` → edges with kind `called_by`
- [ ] `outgoing_calls` → edges with kind `calls`
- [ ] Edge deduplication on (from_fqn, to_fqn, edge_kind)
- [ ] Missing capability → that operation skipped, others still run
- [ ] Target symbol not in DB → edge skipped (no crash, no orphan FK violation)
- [ ] `_resolve_symbol` picks innermost (most specific) symbol at a line
- [ ] `delete_file_edges` removes edges referencing a file's symbols
- [ ] All existing tests pass (zero regression)
- [ ] `uv run pytest tests/test_lsp_population.py -v` → all pass

## Anti-Patterns
- NO edge inserts with NULL symbol_id — FK constraint will reject; skip if resolution fails
- NO per-edge INSERT — batch all edges for a file in one statement
- NO edge collection after didClose — LSP operations need the file open
- NO blocking on edge failures — log and skip per symbol, same pattern as hover
- NO trusting that target symbols exist — always resolve via `_resolve_symbol`, skip if None
- NO deleting symbols before edges — edges reference symbol IDs; delete edges first
