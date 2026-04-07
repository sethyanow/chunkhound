---
id: ch-eun
title: Reclassify mismarked integration tests with fixture conversion
status: active
type: task
priority: 1
owner: Seth
---



## Context

864 tests marked `integration` are mostly misclassified. The `integration` marker should mean multi-module collaboration (orchestrator + provider + embedding). Most of these tests call a single external system (tree-sitter, DuckDB, LanceDB, git, filesystem) and test our logic against it.

Pre-commit suite (`-m "unit or integration"`) takes ~5 minutes. Goal: seconds.

### Inventory (from handoff)

| Category | Tests | What they call |
|---|---|---|
| parser/chunker | ~325 | tree-sitter parse + our extraction logic |
| git/fs | ~113 | temp dirs, git repos, filesystem walks |
| db_provider | ~95 | DuckDB/LanceDB provider methods |
| cli/process | ~41 | subprocesses, daemon guards |
| docsite | ~25 | file generation, template rendering |
| search_pipeline | ~19 | orchestrator chains (LEGIT integration) |
| config | ~11 | config loading/validation |
| mcp | ~6 | MCP server startup |
| realtime | ~4 | file watching |

### Agreed tier definitions

- **unit**: One module, its logic, no external system calls. External deps (DuckDB, LanceDB, tree-sitter, git, filesystem) are replaced by fixtures or mocks.
- **integration**: Multiple modules collaborating — indexing coordinator + provider + embedding service.
- **e2e**: Full process entry points — CLI, MCP, daemon lifecycle.

## Requirements

1. **Audit before converting.** For each test file: is it testing our logic (keep with fixtures) or testing the external system's behavior (delete)?
2. **Capture fixtures.** Run each kept test once, capture external system output as JSON fixtures.
3. **Rewrite tests.** Load fixtures instead of live calls.
4. **Re-mark.** Only change marker AFTER the test no longer calls the external system.
5. **Pre-commit speed.** Unit suite should run in seconds, not minutes.

## Phases

### Phase 1: Audit

Go through every integration-marked test file. For each, determine:
- **KEEP**: tests our logic, external system is just the mechanism. Convert to fixture-based.
- **DELETE**: tests the external system's behavior, not ours. Remove.
- **ALREADY UNIT**: no external calls at all, just mismarked. Re-mark immediately.
- **E2E**: spawns subprocesses, MCP servers, daemons. Re-mark as e2e.

Deliver: audit table with file, test count, verdict, rationale.

### Phase 2: Convert by category

Work one category at a time. For each file in the category:
1. Run the test, capture external system output as JSON fixture
2. Rewrite test to load fixture, remove external call
3. Verify test still passes against fixture
4. Change marker to unit
5. Verify full unit suite still passes

Order by impact (largest categories first):
1. parser/chunker (~325 tests)
2. git/fs (~113 tests)
3. db_provider (~95 tests)
4. cli/process (~41 → e2e, not fixture conversion)
5. remaining categories

### Phase 3: Cleanup

- Delete tests marked for deletion in audit
- Verify pre-commit suite runs in target time
- Update AGENTS.md test commands if needed

## Success Criteria

- [ ] Audit table delivered and reviewed by user
- [ ] Every converted test loads fixtures, no live external calls
- [ ] No marker changes without corresponding fixture conversion (except pure-logic tests and e2e reclassification)
- [ ] `uv run pytest -m unit` runs in <30s (from current ~minutes)
- [ ] `uv run pytest -m "unit or integration"` still passes — no tests lost without audit justification
- [ ] ~19 tests remain marked integration (legitimate multi-module tests)

## Anti-Patterns

- Swapping markers without converting the test (already did this, got reverted)
- Building capture infrastructure instead of doing the audit first
- Skipping the audit and jumping to fixture capture
- Snapshot-testing where property assertions are stronger
- Converting fast deterministic tests where the effort exceeds the value

## Key Considerations

- Tree-sitter AST nodes are C objects, not JSON-serializable. Fixture boundary must be at Chunk output level or higher.
- Some tests assert on semantic properties ("contains symbol X") which is better than snapshot comparison. Audit should flag these — fixture conversion may weaken the test.
- The audit may reveal that some categories don't need fixture conversion if they're already fast and deterministic. Surface this as a decision, don't assume.
