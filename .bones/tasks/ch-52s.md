---
id: ch-52s
title: Test suite mixes unit/integration/e2e — 6+ min full run blocks pre-commit
status: closed
type: bug
priority: 0
owner: Seth
depends_on: [ch-b60]
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

**pytestmark collision:** 17 files already have `pytestmark = pytest.mark.skipif(...)`. When adding a category marker to these files, use list form: `pytestmark = [pytest.mark.unit, pytest.mark.skipif(...)]`. Single assignment overwrites existing markers.

**Function-level markers:** 3 files use `@pytest.mark.integration` on individual functions (not module-level). If a file has mixed unit+integration functions, split is acceptable but prefer module-level classification by the file's dominant concern. The function-level `@pytest.mark.integration` decorators stay as-is — they already work.

### Step 3: Configure pyproject.toml
- Add `e2e` marker definition (only one missing — `unit` and `integration` already defined).
- Add `-m unit` to `addopts` so bare `uv run pytest` runs only unit-marked tests.

### Step 4: Reconcile conftest.py integration skip
Root `conftest.py` has `pytest_collection_modifyitems` that auto-skips `integration` tests unless `CHUNKHOUND_RUN_INTEGRATION_TESTS=1`. With `-m unit` in addopts, integration tests are already deselected by default. Remove the env-var skip mechanism — it's redundant and creates double-gating where `uv run pytest -m integration` would still skip tests. Keep the `heavy` env-var skip as-is (heavy is orthogonal).

### Step 5: Classify tests/manual/
`tests/manual/` contains API-key-gated tests (`test_anthropic_thinking.py`, `test_gemini_thinking.py`). These require live API keys. Mark as `integration` (they depend on external services). The existing `skipif` conditions already gate execution.

### Step 6: Update CLAUDE.md commands
Update KEY_COMMANDS to reflect new test tiers.

### Step 7: Verify
- `uv run pytest` runs only unit-marked tests, completes fast
- `uv run pytest -m integration` runs integration tests (no env var needed)
- `uv run pytest -m e2e` runs e2e tests
- `uv run pytest -m "unit or integration or e2e"` runs everything, count matches original 2307

## Success Criteria
- [x] Every test file has exactly one marker (unit, integration, e2e, or acceptance)
- [x] `uv run pytest` runs only unit tests and completes in under 60 seconds (16s, 1402 tests)
- [x] `uv run pytest -m "unit or integration or e2e or acceptance"` collects all 2307 tests
- [x] CLAUDE.md KEY_COMMANDS updated with test tiers
- [x] Zero tests deleted
- [x] All tests still pass under their respective markers (unit: 1399 passed, 3 skipped; acceptance: 12 deselected pending VCR ch-b60)

## Anti-Patterns
- NO deleting tests to make the suite faster
- NO changing test behavior — only adding markers
- NO guessing categories — read each file and classify by what it does
- NO numeric target gaming — if unit tests are slow, reclassify them
- NO overwriting existing pytestmark with single assignment — use list form when skipif/other markers present
- NO leaving conftest.py env-var skip alongside -m filtering — remove redundant mechanism

## Key Considerations
- 269 test files across 11+ directories. Batch classification by directory first (tests/unit/ → unit, tests/integration/ → integration, tests/e2e/ → e2e), then classify remaining root and subdirectory files individually.
- Files in tests/unit/ already have correct classification by convention — verify they're truly unit tests (no file I/O, no subprocess, no DuckDB) before bulk-marking.
- Files in tests/integration/ already have correct classification — verify they genuinely do I/O or external work.
- Some test files use `conftest.py` fixtures that create tmp dirs, DuckDB instances, etc. The TEST is unit-like but the FIXTURE does I/O. Classify by test behavior, not fixture implementation — if the test logic is pure assertions on mock/fixture data, it's unit.
- **pytest -m override confirmed:** CLI `-m` overrides addopts `-m` (last wins). `addopts = ["-m", "unit"]` is the correct mechanism. Tested on pytest 8.4.2.
- **Misclassification propagation:** A file that LOOKS unit (no obvious I/O imports) may create temp files via fixtures or call subprocess via helpers. Check fixture chains, not just import statements.
- **Partial application risk:** If interrupted mid-marking, `-m unit` in addopts silently skips all unmarked files. The total count verification (Step 7) catches this. Run it BEFORE committing.
- **conftest.py env-var removal is safe:** `CHUNKHOUND_RUN_INTEGRATION_TESTS` is used only in `tests/conftest.py` — no CI, Makefile, or script references. Keep `CHUNKHOUND_RUN_HEAVY_TESTS` logic intact (surgical edit).
- **tests/manual/ → integration:** API-key-gated tests depend on external services. Mark as integration. Existing skipif conditions already prevent accidental execution.

## Log

- [2026-03-30T15:21:12Z] [Seth] P0 — MUST be first task next session. Was wrongly deprioritized in favor of acceptance task. User directive: this is the next thing, no exceptions.
