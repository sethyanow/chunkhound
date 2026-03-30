---
id: ch-52s
title: Test suite mixes unit/integration/e2e — 6+ min full run blocks pre-commit
status: open
type: bug
priority: 0
---


## Context
Full test suite (`uv run pytest tests/ -v`) takes 6+ minutes and 2307 tests. It mixes true unit tests with file I/O heavy tests, daemon process tests, parser loading tests, and integration/e2e tests. Pre-commit full suite runs are unusable at this speed.

Existing state:
- `tests/integration/` and `tests/e2e/` directories exist but many integration/e2e tests live in root `tests/` and other dirs
- `@pytest.mark.integration` exists and is wired to `CHUNKHOUND_RUN_INTEGRATION_TESTS` env var (added in ch-17t) — 35 tests across 7 files use it
- Slow test categories found during ch-a58: daemon smoke tests (process start/stop), realtime functional tests (file watchers), parser loading tests (all 32 grammars), code mapper tests (full pipeline), CLI tests with actual subprocess calls
- `pyproject.toml` has pytest config — marker definitions, timeout settings

## Requirements

R1. Categorize all tests with pytest markers: `unit` (pure logic, mocks, no I/O), `integration` (file I/O, DuckDB, subprocess), `e2e` (full pipeline, daemon, MCP server). Every test file gets exactly one marker.

R2. Configure pytest so bare `uv run pytest` runs only `unit` tests. `uv run pytest -m integration` for integration. `uv run pytest -m e2e` for e2e. `uv run pytest -m "unit or integration or e2e"` for everything.

R3. `uv run pytest` (unit only) completes in under 60 seconds. This is a structural outcome — not a numeric target to game. If unit tests are slow, they're miscategorized.

R4. Update CLAUDE.md `KEY_COMMANDS` section: `smoke` stays as-is, `test` becomes unit-only, add `test-all` for full suite.

R5. Zero test deletion. Every existing test keeps running — just under the right marker.

## Implementation

### Step 1: Audit and classify every test file
Walk `tests/` and classify each file as unit/integration/e2e based on what it actually does (not where it lives). Output: classification list for review.

### Step 2: Apply markers
Add `pytestmark = pytest.mark.<category>` to each test file. Files already marked `integration` keep that marker.

### Step 3: Configure pyproject.toml
Add marker definitions for `unit`, `integration`, `e2e`. Set `addopts` or default mark expression so bare pytest runs unit only.

### Step 4: Update CLAUDE.md commands
Update KEY_COMMANDS to reflect new test tiers.

### Step 5: Verify
- `uv run pytest` runs only unit-marked tests, completes fast
- `uv run pytest -m integration` runs integration tests
- `uv run pytest -m e2e` runs e2e tests
- `uv run pytest -m "unit or integration or e2e"` runs everything, count matches original 2307

## Success Criteria
- [ ] Every test file has exactly one marker (unit, integration, or e2e)
- [ ] `uv run pytest` runs only unit tests and completes in under 60 seconds
- [ ] `uv run pytest -m "unit or integration or e2e"` collects all 2307 tests
- [ ] CLAUDE.md KEY_COMMANDS updated with test tiers
- [ ] Zero tests deleted
- [ ] All tests still pass under their respective markers

## Anti-Patterns
- NO deleting tests to make the suite faster
- NO changing test behavior — only adding markers
- NO guessing categories — read each file and classify by what it does
- NO numeric target gaming — if unit tests are slow, reclassify them
