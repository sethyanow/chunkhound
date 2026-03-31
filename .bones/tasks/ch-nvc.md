---
id: ch-nvc
title: 'Hover per symbol: type_signature population'
status: open
type: task
priority: 1
parent: ch-0um
---



## Context
Second task for Phase 2 (ch-0um). ch-5b3 delivered `LSPPopulationService` with `populate_file` that calls `documentSymbol` and writes rows to the `symbols` table. The `type_signature` column is currently always NULL.

This task adds `hover` calls per symbol to populate `type_signature`. The hover call happens between `documentSymbol` and `didClose` — file is already open.

**Blocked by:** ch-5b3 (closed)
**Unlocks:** Edge population (next task — definition/references/calls), and type_signature availability for graph tools in Phase 3.

## Requirements
From parent epic R3, scoped to hover/type_signature:
- `hover` called per symbol → `type_signature` populated on `symbols` rows
- Hover failures for individual symbols must not block the file's other symbols
- `type_signature` stored as the raw hover content string (markdown)

## Design

### Modification to `populate_file` in `chunkhound/services/lsp_population.py`

Current flow: `didOpen → documentSymbol → didClose → flatten → batch INSERT`

New flow: `didOpen → documentSymbol → hover per symbol → didClose → flatten (with type_sig) → batch INSERT`

Hover calls happen while the file is still open (between documentSymbol and didClose). The `_flatten_symbols` method gains a `type_signatures: dict[tuple[int, int], str]` parameter mapping `(line, char) → hover_contents`.

### Hover collection: `_collect_type_signatures`

New private method on `LSPPopulationService`:
- Signature: `async def _collect_type_signatures(self, client: LSPClient, uri: str, symbols: list[SymbolInfo]) -> dict[tuple[int, int], str]`
- Iterates all symbols (including nested children) recursively
- For each symbol: `client.hover(uri, symbol.range_start_line, symbol.range_start_char)`
- If `HoverResult` is not None: store `result.contents` keyed by `(range_start_line, range_start_char)`
- If hover raises or returns None: skip silently (some symbol kinds like Module/Namespace don't have hover data)
- Returns the mapping for `_flatten_symbols` to consume

### Batch INSERT change

Current INSERT has 11 columns. Add `type_signature` as 12th:
```sql
INSERT INTO symbols (fqn, name, kind, language, file_id, file_path,
    range_start, range_end, parent_fqn, confidence, lsp_server, type_signature)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

`_flatten_symbols` tuple grows from 11 to 12 elements, last element is `type_signatures.get((sym.range_start_line, sym.range_start_char))` (None if no hover data).

## Implementation

1. Write test: `_collect_type_signatures` calls hover per symbol and returns mapping
2. Write test: hover failure on one symbol doesn't prevent others from getting type_signature
3. Write test: `populate_file` stores type_signature in symbols table for symbols that have hover data
4. Write test: symbols without hover data have type_signature = NULL
5. Implement `_collect_type_signatures` method
6. Modify `_flatten_symbols` to accept and use `type_signatures` dict
7. Modify `_batch_insert` to include 12th column (type_signature)
8. Modify `populate_file` to call `_collect_type_signatures` between documentSymbol and didClose
9. Verify existing tests still pass (no regression from tuple size change)

## Success Criteria
- [ ] `hover` called for each symbol returned by `documentSymbol`
- [ ] `type_signature` column populated with hover contents for symbols that have hover data
- [ ] Symbols without hover data have `type_signature = NULL` (not empty string)
- [ ] Hover failure on one symbol doesn't prevent other symbols from being populated
- [ ] Batch INSERT includes `type_signature` as 12th column
- [ ] All existing ch-5b3 tests updated and passing (tuple size change)
- [ ] `uv run pytest tests/test_lsp_population.py -v` → all pass

## Anti-Patterns
- NO hover calls after `didClose` — file must still be open for hover to return results
- NO blocking on hover failures — log and skip, set type_signature to NULL
- NO parsing/transforming hover contents — store raw markdown string as-is
- NO hover calls in a separate `populate_file` pass — integrate into existing single-pass flow
