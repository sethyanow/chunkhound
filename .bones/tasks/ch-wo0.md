---
id: ch-wo0
title: cross_language_check — exported symbol mismatches across scopes
status: closed
type: task
priority: 1
owner: Seth
parent: ch-dar
---





## Context
Third Phase 4 fusion tool. Given two scope prefixes (file path prefixes),
queries symbols in each scope, matches by name, and compares arity to find
binding mismatches. Pure symbols-table comparison — no graph walks, no LLM.
Reuses `escape_like`, `@register_tool`, and `make_mock_services` patterns.

**Blocked by:** ch-zj0 (test_targeting, closed — establishes scope query + escape patterns)
**Unlocks:** `semantic_diff` (last fusion tool) + Phase 4 shared criteria

## Requirements
From ch-dar (Phase 4 epic) success criteria:
- `cross_language_check(scope_a, scope_b)` compares exported symbols across scopes, returns mismatches in name/signature
- Works across different languages (e.g., Python bindings vs C extensions)
- Comparison is structural (name + arity), not string equality (ch-8e7 Key Considerations)
- Deterministic — no LLM, no embeddings
- Structured data output, not prose

## Design

**Composition:** `_query_scope_symbols(scope)` × 2 → `_compare_scope_symbols(a, b)` (uses `_extract_arity`) → structured output

**Key decisions:**
1. Scopes are file_path LIKE prefixes — the scope defines the language boundary (e.g., `"bindings/python/"` vs `"src/core/"`)
2. Symbol matching is by `name` field — if two symbols in different scopes share a name, they're candidates
3. Arity extraction from `type_signature` is heuristic: find content between `(` and matching `)`, strip `self`/`cls` first param, count remaining by `,` splits
4. Mismatch types: `"arity"` (param count differs), `"kind"` (Function vs Class with same name). Both arities `None` → not a mismatch (insufficient data)
5. Missing symbols: name in scope_a but not scope_b → `missing_in_b` (and vice versa)
6. Multiple symbols with same name in one scope (overloads, different kinds) → report all pairs

**Output shape:**
```json
{
  "scope_a": "bindings/python/",
  "scope_b": "src/core/",
  "mismatches": [
    {
      "name": "process_data",
      "scope_a": {"fqn": "...", "kind": "Function", "language": "python", "type_signature": "(data: bytes) -> str", "arity": 1},
      "scope_b": {"fqn": "...", "kind": "Function", "language": "c", "type_signature": "int process_data(const char*, int)", "arity": 2},
      "mismatch_type": "arity"
    }
  ],
  "missing_in_a": [{"name": "core_only", "fqn": "...", "kind": "Function"}],
  "missing_in_b": [{"name": "binding_only", "fqn": "...", "kind": "Function"}],
  "total_compared": 15,
  "total_mismatches": 3
}
```

## Implementation

### Step 1: Write failing test — query scope symbols
File: `tests/mcp_server/test_fusion_tools.py` (extend)
Test class: `TestQueryScopeSymbols`
- Test: scope prefix returns `dict[str, list[dict]]` mapping name→[{fqn, kind, language, file_path, type_signature}]
- Test: multiple symbols with same name (overloads) → all in the list for that name
- Test: empty result → empty dict
- Test: scope LIKE-escaped (verify LIKE + ESCAPE in query via `scope_filter`)
Mock: `services.provider.execute_query` returns canned symbol rows

### Step 2: Implement `_query_scope_symbols`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `_query_scope_symbols(services: Any, scope: str) -> dict[str, list[dict[str, Any]]]`
Use `scope_filter(scope)` from `queries/common.py` to generate the LIKE clause with proper ESCAPE handling. Query: `SELECT name, fqn, kind, language, file_path, type_signature FROM symbols WHERE {scope_sql}` with scope params. Group rows by `name` into lists using `dict.setdefault`. Return dict.

### Step 3: Write failing test — extract arity from type signature
File: `tests/mcp_server/test_fusion_tools.py`
Test class: `TestExtractArity`
- Test: `"(self, x: int, y: str) -> bool"` → 2 (strips self)
- Test: `"(cls, x: int) -> Foo"` → 1 (strips cls)
- Test: `"(x: number, y: string) => boolean"` → 2 (TS-style)
- Test: `"() -> None"` → 0
- Test: `"(x: int) -> int"` → 1
- Test: `None` → `None`
- Test: `""` → `None`
- Test: `"int process_data(const char*, int)"` → 2 (C-style, parens in middle)
- Test: `"(data: dict[str, int]) -> bool"` → 1 (comma inside generic brackets)
- Test: `"(items: list[tuple[int, str]]) -> None"` → 1 (nested generics)
- Test: `"(callback: Callable[[int, str], bool]) -> None"` → 1 (Callable with nested brackets)
- Test: `"(x: int,) -> None"` → 1 (trailing comma, common in Python)
- Test: `"(x: int, y:"` → `None` (unbalanced parens, truncated signature)

### Step 4: Implement `_extract_arity`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `_extract_arity(type_signature: str | None) -> int | None`
Heuristic: return `None` if input is None/empty. Find first `(` and its matching `)` (track paren nesting). If no matching `)` found → return `None`. Extract content between them. Strip leading `self,`/`cls,` (Python convention). If empty after strip → 0. Otherwise count commas at nesting depth 0 only — track `[`, `(`, `{`, `<` nesting to skip commas inside generic type parameters (e.g., `dict[str, int]` has one param, not two). Split on top-level commas, filter out empty/whitespace-only segments (handles trailing commas), count remaining. No parens found → `None`.

### Step 5: Write failing test — compare scope symbols
File: `tests/mcp_server/test_fusion_tools.py`
Test class: `TestCompareScopeSymbols`
- Test: same name, same arity → no mismatch (matched)
- Test: same name, different arity → mismatch with `mismatch_type: "arity"`
- Test: same name, different kind (Function vs Class) → mismatch with `mismatch_type: "kind"`
- Test: name only in scope_a → `missing_in_b`
- Test: name only in scope_b → `missing_in_a`
- Test: both arities None (no type_signature) → no mismatch (insufficient data)
- Test: both scopes empty → empty output

### Step 6: Implement `_compare_scope_symbols`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `_compare_scope_symbols(symbols_a: dict[str, list[dict[str, Any]]], symbols_b: dict[str, list[dict[str, Any]]]) -> dict[str, Any]`
Pure function. Name sets: `names_a`, `names_b`. Missing: symmetric difference. Matched: intersection. For each matched name: cross-product of entries from both sides. Compare kind first (mismatch if different). Then compare arity via `_extract_arity` (mismatch if both non-None and different). Return `{mismatches, missing_in_a, missing_in_b}`.

### Step 7: Write failing test — full tool integration
File: `tests/mcp_server/test_fusion_tools.py`
Test class: `TestCrossLanguageCheckImpl`
- Test: two scopes with overlapping names, one arity mismatch → correct structured output
- Test: no overlapping names → only missing lists populated, mismatches empty
- Test: empty scopes → `total_compared: 0, total_mismatches: 0`
- Test: tool registered in TOOL_REGISTRY
Mock: sequential `execute_query` calls (scope_a symbols, scope_b symbols)

### Step 8: Implement `cross_language_check_impl`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `async def cross_language_check_impl(services: Any, config: Any, scope_a: str, scope_b: str) -> dict[str, Any]`
`@register_tool(name="cross_language_check")`. Compose: query scope_a → query scope_b → compare → build output with `{scope_a, scope_b, mismatches, missing_in_a, missing_in_b, total_compared, total_mismatches}`. `total_compared` = number of names in intersection.

### Step 9: Wire — update expected tool count
File: `tests/mcp_server/test_adversarial_decomp.py`
Add `"cross_language_check"` to `EXPECTED_TOOLS` set. Update docstring count (9→10).

## Success Criteria
- [x] `_query_scope_symbols` returns grouped symbols by name for a scope prefix
- [x] `_extract_arity` parses parameter count from Python, TS, and C-style signatures
- [x] `_extract_arity` strips `self`/`cls` from Python signatures
- [x] `_extract_arity` returns `None` for missing/unparseable signatures (including unbalanced parens)
- [x] `_extract_arity` correctly handles generic types with commas (`dict[str, int]` → 1)
- [x] `_extract_arity` correctly handles trailing commas (`(x: int,)` → 1)
- [x] `_compare_scope_symbols` detects arity mismatches between matched names
- [x] `_compare_scope_symbols` detects kind mismatches (Function vs Class)
- [x] `_compare_scope_symbols` reports missing symbols (name in one scope but not other)
- [x] `_compare_scope_symbols` does NOT report mismatch when both arities are None
- [x] `cross_language_check_impl` registered in TOOL_REGISTRY via `@register_tool`
- [x] Output is structured: `{scope_a, scope_b, mismatches, missing_in_a, missing_in_b, total_compared, total_mismatches}`
- [x] Zero LLM/embedding calls — deterministic only
- [x] All new code has failing tests before implementation
- [x] `uv run pytest tests/mcp_server/test_fusion_tools.py -v` → all pass

## Key Considerations
- Arity extraction is a heuristic, not a parser. It handles common patterns (Python, TS, C) including generic type parameters with commas (e.g., `dict[str, int]` → 1 param, not 2) via nesting-aware comma counting. Will fail on truly exotic signatures (function pointers as params, C++ template metaprogramming). Acceptable for v1 — the tool flags potential mismatches, not proven ones.
- `self`/`cls` stripping is Python-specific. Other languages with receiver parameters (Go, Rust) may need similar handling in future.
- Multiple symbols with same name in one scope (e.g., a Function and a Class both named `Config`) generate a cross-product of comparisons. For N×M entries this could be chatty, but typical codebases have few same-name collisions.
- The tool doesn't filter by "exported" in a language-specific sense (Python `__all__`, JS `export`). It compares ALL symbols in each scope. Users narrow scope prefixes to compare only what matters.
- `language` column in symbols table comes from LSP — it's the language_id used for server routing. Reliable for determining which language each side is.

### Adversarial Findings

**`_extract_arity` — trailing commas:** Python allows `(x: int,)` — trailing comma after last param. Naive comma counting gives arity 2. Fix: after splitting on top-level commas, filter out empty/whitespace-only segments before counting.

**`_extract_arity` — unbalanced parens:** LSP may return truncated signatures like `"(x: int, y:"`. If no matching `)` found, return `None` (unparseable) rather than extracting garbage content to end-of-string.

**`_query_scope_symbols` — empty scope:** Empty string scope → `LIKE '%'` → matches all files. Returns entire symbols table. This is correct behavior (compare everything), but could be slow on large DBs. Not a bug — document as expected.

**`_compare_scope_symbols` — `__init__` fan-out:** Common names like `__init__` appear in many classes. 20×15 cross-product = 300 comparisons for one name. Noisy but correct. Agent filters by relevance. No code change needed.

**`cross_language_check_impl` — overlapping scopes:** `scope_a="src/"`, `scope_b="src/core/"` — scope_b is a subset. Symbols in overlap appear in both results. Comparison produces no mismatches for self-matches but may confuse users. Not a code-level fix — document in tool description.

## Anti-Patterns
- NO hardcoded language-specific type parsing — arity extraction is language-agnostic heuristic
- NO live LSP calls — use indexed symbols table only
- NO LLM/embedding calls — deterministic comparison
- NO string equality for type_signature comparison — structural (arity) only
- NO prose output — structured dicts only

## Log

- [2026-04-03T13:17:22Z] [Seth] SRE review: APPROVED with updates. (1) MAJOR: _extract_arity design upgraded to nesting-aware comma counting — dict[str,int] was miscounted. Added 5 test cases: generics, trailing comma, unbalanced parens. (2) MINOR: Step 2 switched from hand-rolled escape_like to scope_filter helper. (3) Adversarial: trailing commas, unbalanced parens, empty scope, __init__ fan-out, overlapping scopes — all addressed in skeleton.
- [2026-04-03T13:36:34Z] [Seth] Debrief: SRE caught comma-in-generics design bug before implementation. scope_filter helper used instead of hand-rolled escape. All 15 criteria met, 86 tests (49 functional + 13 adversarial for cross_language_check). Next task ch-4h0 (semantic_diff) scoped — last fusion tool, uses pygit2 for git diff + range overlap for symbol mapping.
