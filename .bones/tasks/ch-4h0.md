---
id: ch-4h0
title: semantic_diff — behavior-level change classification
status: active
type: task
priority: 1
owner: Seth
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
- Test: binary file in diff → excluded (delta.is_binary skip)
- Test: renamed file → uses delta.new_file.path, lines collected normally
- Test: empty string ref → error dict (not crash)
- Test: patch with only deletions (no "+" lines) → file excluded from result (empty line list skipped)
Mock: pygit2.Repository (mock the diff object structure: patches → hunks → lines)

### Step 2: Implement `_git_changed_lines`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `_git_changed_lines(repo_path: str, base: str, head: str) -> dict[str, list[int]] | dict[str, str]`
Use pygit2: open repo, resolve refs via `repo.revparse_single(ref).peel(pygit2.Commit)`, compute diff, iterate patches. For each patch: skip if `delta.is_binary` or `delta.status == GIT_DELTA_DELETED` (constant=2). For all other statuses (ADDED=1, MODIFIED=3, RENAMED=4, COPIED=5): use `delta.new_file.path` as the file key, collect `line.new_lineno - 1` (convert to 0-based) for lines with `origin == "+"`. Return `{file_path: sorted_line_numbers}`. Catch `KeyError` (invalid refs) and `pygit2.GitError` (bad repo path) → error dict. Skip files with empty line lists after collection (deletion-only patches). Defensively check `new_lineno > 0` before 0-based conversion.

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
For each file in changed_lines: query symbols overlapping the changed range: `SELECT fqn, name, kind, file_path, type_signature, range_start, range_end FROM symbols WHERE file_path = ? AND range_start <= ? AND range_end >= ?` where `?` is max(changed_lines) and min(changed_lines) respectively. For each symbol: intersect its [range_start, range_end] with changed_lines → `changed_lines` list. **Exclude symbols with empty intersection** (broad SQL returns candidates, Python narrows). Classify remaining: if range_start in changed_lines → "signature_change", else "body_only".

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
1. **Clamp depth 1-10** (matching impact_cascade pattern)
2. Resolve workspace_root from config
3. `_git_changed_lines(workspace_root, base, head)` → if error, return it
4. `_map_lines_to_symbols(services, changed_lines)` → changed_symbols
5. For each changed symbol: `_graph_walk(services, symbol=fqn, depth=depth, edge_kind="called_by", limit=100, directed=True)`. **Check `if "error" in walk_result: continue`** before accessing results (symbol may be in diff but not in graph). Collect callers, deduplicate by FQN, keep min hop_distance. Track which changed symbol triggered each caller.
6. `_annotate_type_signatures(services, affected_callers_list)`
7. Build summary: count signature_changes vs body_only
8. Return structured output

### Step 7: Wire — update expected tool count
File: `tests/mcp_server/test_adversarial_decomp.py`
Add `"semantic_diff"` to `EXPECTED_TOOLS` set. Update docstring count (10→11).

## Success Criteria
- [ ] `_git_changed_lines` returns file→line_numbers mapping from pygit2 diff
- [ ] `_git_changed_lines` handles invalid refs with error dict (not exception)
- [ ] `_git_changed_lines` excludes deleted files, includes new files, skips binary files
- [ ] `_git_changed_lines` handles renamed/copied files using delta.new_file.path
- [ ] `_git_changed_lines` converts to 0-based line numbers (matching DB schema)
- [ ] `_map_lines_to_symbols` maps changed lines to symbols via range overlap
- [ ] `_map_lines_to_symbols` classifies changes: range_start touched → "signature_change", else "body_only"
- [ ] `semantic_diff_impl` identifies changed symbols and walks their caller graph
- [ ] `semantic_diff_impl` clamps depth to 1-10
- [ ] `semantic_diff_impl` skips graph walk errors for individual symbols (doesn't abort)
- [ ] `semantic_diff_impl` deduplicates affected callers by FQN with min hop_distance
- [ ] `semantic_diff_impl` returns structured output with summary counts
- [ ] `semantic_diff_impl` registered in TOOL_REGISTRY via `@register_tool`
- [ ] Zero LLM/embedding calls — deterministic only
- [ ] All new code has failing tests before implementation
- [ ] `uv run pytest tests/mcp_server/test_fusion_tools.py -v` → all pass
- [ ] `uv run pytest tests/test_smoke.py -v -n auto` → all pass (inherited from ch-dar gate)

## Key Considerations
- pygit2 is already a dependency (v1.19.0). The diff API provides `Patch.delta.new_file.path`, `Hunk.lines[].new_lineno`, and `Line.origin` ("+", "-", " "). New file lines are origin "+".
- Line numbers in pygit2 are 1-based (`new_lineno`). Symbols table uses 0-based (`range_start`, `range_end`). Must convert: `new_lineno - 1`.
- Signature change detection is a heuristic: if the symbol's declaration line (range_start) is in the changed lines, it's a "signature_change". This catches function signature changes, class definition changes, but may miss some edge cases (e.g., decorator-only changes above the def line). Acceptable for v1.
- `_git_changed_lines` only collects addition-side lines (origin "+"). Deletions appear in old_lineno and don't map to current symbols table (which reflects HEAD state).
- For refs: pygit2's `revparse_single` handles branch names, tags, SHAs, HEAD~N, etc. It raises `KeyError` on invalid refs.
- The `_graph_walk` + `_annotate_type_signatures` composition is identical to impact_cascade's pattern. No new graph logic needed.
- `affected_callers` includes a `triggered_by` field showing which changed symbol's caller graph reached this caller. If multiple changed symbols reach the same caller, use the one with shortest hop_distance.
- **Binary files:** `delta.is_binary` is True for binary patches — skip them (no meaningful line numbers). Verified in pygit2 1.19.0.
- **Renamed/copied files:** `delta.status` can be RENAMED(4) or COPIED(5). These are valid — use `delta.new_file.path` (works for all statuses). Don't filter by status whitelist; just skip DELETED and is_binary.
- **Empty intersection filter:** The broad SQL `range_start <= max AND range_end >= min` catches all candidate symbols in the changed range. After per-symbol intersection with actual changed_lines, exclude symbols with zero overlapping lines.

### Adversarial Failure Catalog (SRE)

**_git_changed_lines — empty ref strings:** pygit2's `revparse_single("")` behavior is undefined. Validate non-empty before calling pygit2. KeyError catch covers most cases but explicit guard is cleaner.

**_git_changed_lines — non-UTF-8 filenames:** Git allows arbitrary byte sequences in filenames. pygit2 on Python 3 returns str (UTF-8 decoded) — non-UTF-8 paths may raise. Catch and skip.

**_git_changed_lines — GitError on bad repo_path:** If workspace_root isn't a git repo, pygit2 raises `GitError`, not `KeyError`. Error handler must catch both `KeyError` and `pygit2.GitError`.

**_git_changed_lines — negative new_lineno edge case:** `origin == "+"` filter should exclude deletion-side lines, but defensively check `new_lineno > 0` before converting to 0-based.

**_map_lines_to_symbols — empty line list per file:** A patch with only deletions (file modified but no additions) produces a file key with empty list. `min([])` / `max([])` raises `ValueError`. Guard: `if not lines: continue`.

**_map_lines_to_symbols — path format mismatch:** pygit2 returns repo-relative paths. Symbols table stores paths as set during indexing. Paths match when indexed from repo root (standard flow). Stale or differently-rooted indexes silently miss symbols.

**semantic_diff_impl — missing depth clamp:** Skeleton has `depth: int = 3` but no clamping. Must clamp 1-10, matching impact_cascade pattern.

**semantic_diff_impl — _graph_walk error dict:** `_graph_walk` can return `{"error": ...}` when a symbol exists in diff but not in graph. Must check `if "error" in walk_result: continue` before accessing `walk_result["results"]`, same as test_targeting (fusion.py:597).

**semantic_diff_impl — large symbol count:** 200+ changed symbols → 200+ sequential `_graph_walk` calls. Functional but slow. Accept for v1; batch/parallelize is future optimization.

## Anti-Patterns
- NO hardcoded ref names — accept any valid git ref string
- NO shelling out to git CLI — use pygit2 (already available, tested)
- NO live LSP calls — use indexed symbols table only
- NO LLM/embedding calls — deterministic comparison
- NO prose output — structured dicts only
- NO reading file content to detect signature changes — use range_start heuristic
- NO processing binary files — skip patches where delta.is_binary is True
