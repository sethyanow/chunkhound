---
id: ch-nvc
title: 'Hover per symbol: type_signature population'
status: closed
type: task
priority: 1
owner: Seth
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
- [x] `hover` called for each symbol returned by `documentSymbol`
- [x] `type_signature` column populated with hover contents for symbols that have hover data
- [x] Symbols without hover data have `type_signature = NULL` (not empty string)
- [x] Hover failure on one symbol doesn't prevent other symbols from being populated
- [x] Batch INSERT includes `type_signature` as 12th column
- [x] Server without hover capability → all type_signatures NULL, no exceptions logged per symbol
- [x] All existing ch-5b3 tests updated and passing (tuple size change)
- [x] `uv run pytest tests/test_lsp_population.py -v` → all pass

## Key Considerations (SRE)
- **Error catch breadth:** `_collect_type_signatures` must catch `Exception` (broad) per symbol, not just `LSPError`. Hover can fail from JSON parsing, unicode, or other unexpected errors — none should crash the file's population.
- **Recursive traversal:** `_collect_type_signatures` must visit `SymbolInfo.children` recursively (matching the recursion pattern in `_flatten_symbols`). Nested method/function type signatures are the highest-value data.
- **type_signatures dict threading:** The dict returned by `_collect_type_signatures` is passed to `_flatten_symbols` unchanged through its recursive calls — no per-level re-collection needed.
- **Existing test tuple breakage:** Every test that constructs `_sample_symbols()` and asserts DB rows will need the mock client to gain a `hover` mock, and DB assertions may need updating for the 12th column. The skeleton's step 9 covers this but the blast radius is ~10 tests.

## Key Considerations (Adversarial)
- **Capability gate:** `_collect_type_signatures` should check hover capability ONCE at entry (via `client.capabilities` or similar), returning empty dict immediately if not supported. Without this, every symbol in every file for that language triggers an `LSPCapabilityError` — N×M wasted exceptions and noisy logs.
- **Server degradation mid-file:** If server transitions to DEGRADED during hover collection (transport error), remaining symbols all fail. This is acceptable — partial type_signature data is better than none. The per-symbol catch handles it.
- **Position key collision:** Two symbols at the same `(line, char)` (decorators, overloads) → last-write wins in the dict. Acceptable tradeoff vs. duplicating FQN construction.
- **Large files:** A 500-symbol file means 500 sequential hover round-trips (~25s at 50ms each). Acceptable for background population. Future batching/concurrency is a separate concern.
- **Negative position sentinels:** Some servers return `range_start_line = -1` for synthetic symbols. Hover call may fail (caught by broad Exception). Dict key `(-1, -1)` is valid but harmless.

## Anti-Patterns
- NO hover calls after `didClose` — file must still be open for hover to return results
- NO blocking on hover failures — log and skip, set type_signature to NULL
- NO parsing/transforming hover contents — store raw markdown string as-is
- NO hover calls in a separate `populate_file` pass — integrate into existing single-pass flow
- NO hover on only top-level symbols — must recursively include children (nested methods/functions are highest-value targets)
- NO per-symbol capability checks — check once at method entry, return empty dict if hover unsupported
