---
id: ch-4h0
title: semantic_diff — behavior-level change classification
status: open
type: task
priority: 1
parent: ch-dar
---

## Context
Fourth and final Phase 4 fusion tool. Given two git refs (base and head),
identifies changed symbols, walks their impact, and classifies affected callers
as signature-change or body-only. Uses pygit2 for diff, symbol range overlap
for mapping, and existing fusion helpers for impact walking. Deterministic — no LLM.

**Blocked by:** ch-wo0 (cross_language_check, closed — establishes scope_filter, _extract_arity patterns)
**Unlocks:** Phase 4 shared criteria (all 4 fusion tools complete) + Phase 4 acceptance task

## Requirements
From ch-dar (Phase 4 epic) success criteria:
- `semantic_diff(base, head)` identifies changed symbols, walks their impact, classifies affected callers
- `semantic_diff` distinguishes between signature changes and body-only changes
- Deterministic — no LLM, no embeddings
- Structured data output, not prose

From ch-8e7 (parent epic) Key Considerations:
- "semantic_diff needs git integration to determine changed files/symbols between base and head refs. Use pygit2 or git CLI to get the diff, then map changed lines to symbols via range overlap."

## Design

**Composition:** `_git_changed_lines(base, head)` → `_map_lines_to_symbols(changed_lines)` → per-symbol: `_classify_change(symbol, base_sig, head_sig)` + `_graph_walk` caller tree → structured output

**Key decisions:**
1. Use pygit2 (already a dependency, v1.19.0) for diff — provides line-level hunks with line numbers
2. Changed lines are mapped to symbols via range overlap: `WHERE file_path = ? AND range_start <= ? AND range_end >= ?` where `?` is the max changed line in that file. A symbol is "changed" if ANY of its lines are in the diff.
3. Signature change detection: query `type_signature` from symbols table at HEAD. To detect signature changes vs body-only, compare the symbol's type_signature at base vs head. Since symbols table reflects current (HEAD) state, base signatures come from a separate query against the base commit's symbols. **Simplification for v1:** if the indexed DB only has HEAD state, we can't directly compare base signatures. Instead, classify based on whether the changed lines include the symbol's first line (range_start) — changes at the declaration line are "signature_change", others are "body_only". This is a heuristic.
4. Impact walking reuses `_graph_walk` + `_annotate_type_signatures` from impact_cascade
5. Refs are git ref strings (branch names, commit SHAs, HEAD~1, tags). pygit2 resolves them.
6. If a ref can't be resolved, return error dict (not raise)

**Output shape:**
```json
{
  "base": "main",
  "head": "feature-branch",
  "changed_symbols": [
    {
      "fqn": "mod::process_data",
      "name": "process_data",
      "kind": "Function",
      "file_path": "src/mod.py",
      "change_type": "signature_change",
      "type_signature": "(data: bytes, encoding: str) -> str",
      "changed_lines": [10, 11, 15, 16]
    }
  ],
  "affected_callers": [
    {
      "fqn": "tests::test_process",
      "name": "test_process",
      "kind": "Function",
      "file_path": "tests/test_mod.py",
      "hop_distance": 1,
      "type_signature": "() -> None",
      "triggered_by": "mod::process_data"
    }
  ],
  "total_changed": 3,
  "total_affected": 7,
  "summary": {"signature_changes": 1, "body_only": 2}
}
```

## Implementation

### Step 1: Write failing test — git changed lines
File: `tests/mcp_server/test_fusion_tools.py` (extend)
Test class: `TestGitChangedLines`
- Test: two refs with known diff → returns `dict[str, list[int]]` mapping file_path → sorted list of changed line numbers (new-side, additions only — 0-based to match DB range_start/range_end)
- Test: identical refs (same commit) → empty dict
- Test: invalid ref → returns error dict with `"error"` key
- Test: deleted file (in base, not in head) → excluded (no symbols to map)
- Test: new file (not in base, in head) → all lines are changed
Mock: pygit2.Repository (mock the diff object structure: patches → hunks → lines)

### Step 2: Implement `_git_changed_lines`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `_git_changed_lines(repo_path: str, base: str, head: str) -> dict[str, list[int]] | dict[str, str]`
Use pygit2: open repo, resolve refs via `repo.revparse_single(ref).peel(pygit2.Commit)`, compute diff, iterate patches. For each patch: skip deleted files (delta.status == GIT_DELTA_DELETED). For added/modified files: collect `line.new_lineno - 1` (convert to 0-based) for lines with `origin == "+"`. Return `{file_path: sorted_line_numbers}`. Catch pygit2 errors → error dict.

### Step 3: Write failing test — map changed lines to symbols
File: `tests/mcp_server/test_fusion_tools.py`
Test class: `TestMapLinesToSymbols`
- Test: changed lines overlapping a symbol range → symbol included with its changed_lines
- Test: changed lines outside any symbol range → no symbols returned
- Test: multiple symbols in same file, different lines changed → both returned
- Test: symbol's range_start line changed → `change_type: "signature_change"`
- Test: only body lines changed (not range_start) → `change_type: "body_only"`
- Test: empty changed_lines dict → empty result
Mock: `services.provider.execute_query` returns canned symbol rows with range_start, range_end

### Step 4: Implement `_map_lines_to_symbols`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `_map_lines_to_symbols(services: Any, changed_lines: dict[str, list[int]]) -> list[dict[str, Any]]`
For each file in changed_lines: query symbols overlapping the changed range: `SELECT fqn, name, kind, file_path, type_signature, range_start, range_end FROM symbols WHERE file_path = ? AND range_start <= ? AND range_end >= ?` where `?` is max(changed_lines) and min(changed_lines) respectively. For each symbol: intersect its [range_start, range_end] with changed_lines → `changed_lines` list. Classify: if range_start in changed_lines → "signature_change", else "body_only".

### Step 5: Write failing test — full tool integration
File: `tests/mcp_server/test_fusion_tools.py`
Test class: `TestSemanticDiffImpl`
- Test: base→head with one signature change, one body-only → correct output structure with changed_symbols, affected_callers, summary counts
- Test: no changed symbols (diff in non-code files) → empty changed_symbols, empty affected_callers
- Test: invalid base ref → error dict
- Test: tool registered in TOOL_REGISTRY
Mock: pygit2.Repository (via monkeypatch), services.provider.execute_query (symbol queries + graph walk queries)

### Step 6: Implement `semantic_diff_impl`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `async def semantic_diff_impl(services: Any, config: Any, base: str, head: str, depth: int = 3) -> dict[str, Any]`
`@register_tool(name="semantic_diff")`. Compose:
1. Resolve workspace_root from config
2. `_git_changed_lines(workspace_root, base, head)` → if error, return it
3. `_map_lines_to_symbols(services, changed_lines)` → changed_symbols
4. For each changed symbol: `_graph_walk(services, symbol=fqn, depth=depth, edge_kind="called_by", limit=100, directed=True)` → collect callers. Deduplicate by FQN, keep min hop_distance. Track which changed symbol triggered each caller.
5. `_annotate_type_signatures(services, affected_callers_list)`
6. Build summary: count signature_changes vs body_only
7. Return structured output

### Step 7: Wire — update expected tool count
File: `tests/mcp_server/test_adversarial_decomp.py`
Add `"semantic_diff"` to `EXPECTED_TOOLS` set. Update docstring count (10→11).

## Success Criteria
- [ ] `_git_changed_lines` returns file→line_numbers mapping from pygit2 diff
- [ ] `_git_changed_lines` handles invalid refs with error dict (not exception)
- [ ] `_git_changed_lines` excludes deleted files, includes new files
- [ ] `_git_changed_lines` converts to 0-based line numbers (matching DB schema)
- [ ] `_map_lines_to_symbols` maps changed lines to symbols via range overlap
- [ ] `_map_lines_to_symbols` classifies changes: range_start touched → "signature_change", else "body_only"
- [ ] `semantic_diff_impl` identifies changed symbols and walks their caller graph
- [ ] `semantic_diff_impl` deduplicates affected callers by FQN with min hop_distance
- [ ] `semantic_diff_impl` returns structured output with summary counts
- [ ] `semantic_diff_impl` registered in TOOL_REGISTRY via `@register_tool`
- [ ] Zero LLM/embedding calls — deterministic only
- [ ] All new code has failing tests before implementation
- [ ] `uv run pytest tests/mcp_server/test_fusion_tools.py -v` → all pass

## Key Considerations
- pygit2 is already a dependency (v1.19.0). The diff API provides `Patch.delta.new_file.path`, `Hunk.lines[].new_lineno`, and `Line.origin` ("+", "-", " "). New file lines are origin "+".
- Line numbers in pygit2 are 1-based (`new_lineno`). Symbols table uses 0-based (`range_start`, `range_end`). Must convert: `new_lineno - 1`.
- Signature change detection is a heuristic: if the symbol's declaration line (range_start) is in the changed lines, it's a "signature_change". This catches function signature changes, class definition changes, but may miss some edge cases (e.g., decorator-only changes above the def line). Acceptable for v1.
- `_git_changed_lines` only collects addition-side lines (origin "+"). Deletions appear in old_lineno and don't map to current symbols table (which reflects HEAD state).
- For refs: pygit2's `revparse_single` handles branch names, tags, SHAs, HEAD~N, etc. It raises `KeyError` on invalid refs.
- The `_graph_walk` + `_annotate_type_signatures` composition is identical to impact_cascade's pattern. No new graph logic needed.
- `affected_callers` includes a `triggered_by` field showing which changed symbol's caller graph reached this caller. If multiple changed symbols reach the same caller, use the one with shortest hop_distance.

## Anti-Patterns
- NO hardcoded ref names — accept any valid git ref string
- NO shelling out to git CLI — use pygit2 (already available, tested)
- NO live LSP calls — use indexed symbols table only
- NO LLM/embedding calls — deterministic comparison
- NO prose output — structured dicts only
- NO reading file content to detect signature changes — use range_start heuristic
