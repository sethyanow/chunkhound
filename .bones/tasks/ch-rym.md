---
id: ch-rym
title: Restructure test_lsp_population.py into tests/lsp/ with quality fixes
status: open
type: task
priority: 1
parent: ch-0um
---


## Context
`tests/test_lsp_population.py` — 3627 lines, 33 classes, 68+ tests across unrelated concerns. Grew organically across ch-5b3, ch-nvc, ch-zlg, ch-ko4 without decomposition. Not just too big — the tests have accumulated quality issues that should be fixed during restructuring.

## Requirements
1. Create `tests/lsp/` module with cohesion-based test files
2. Extract shared setup into proper pytest fixtures in conftest.py
3. Fix test quality issues found during review (not just move code)
4. Resolve LSP diagnostics in the source service file
5. No renaming test classes or methods (git blame preservation)

## Design

### Target structure
```
tests/lsp/
├── __init__.py
├── conftest.py                  # fixtures + shared helpers
├── test_core.py                 # 9 classes: PopulateFileCore, GracefulSkip, FQNConstruction,
│                                #   DeleteFileSymbols, UnknownSymbolKind, Adversarial,
│                                #   PopulateResult, WorkspaceRootResolution, BatchInsert(revised)
├── test_type_signatures.py      # 3 classes: CollectTypeSignatures, PopulateFileTypeSignature,
│                                #   AdversarialHover
├── test_edges.py                # 6 classes: CollectEdges, PopulateFileEdges, DeleteFileEdges,
│                                #   EdgeDeduplication, AdversarialEdges,
│                                #   DeleteSymbolsWithCrossFileEdges
├── test_resolve.py              # 2 classes: ResolveSymbol, ExecuteQueryAsync
├── test_workspace_symbols.py    # 4 classes: WorkspaceSymbolsParsing,
│                                #   PopulateFilesWorkspaceSymbols, WorkspaceSymbolEdgeCases,
│                                #   AdversarialWorkspaceSymbols
├── test_wiring.py               # 4 classes: BatchWiring, RealtimeWiring,
│                                #   MCPServerWiring, CLIWiring
└── test_resilience.py           # 5 classes: PopulateFilesResilience, PopulateFilesTypedCatch,
                                 #   PopulateFilesAdversarial, IncrementalRefresh,
                                 #   MultiLanguagePopulation
```

### Fixture extraction (conftest.py)
Current module-level helpers become proper fixtures where appropriate:
- `_make_provider(tmp_path)` — stays as function (takes arg)
- `_insert_file(provider, ...)` — stays as function (takes args)
- `_make_mock_pool(symbols)` — stays as function but return a namedtuple or dataclass instead of bare tuple
- `_sample_symbols()` — fixture candidate (no args, same data every time)
- `_insert_symbols(provider, ...)` — stays as function (takes args)
- `pytestmark = pytest.mark.unit` at conftest level for the directory

### Quality fixes during restructuring

**TestBatchInsert** — Currently tests implementation detail (counts INSERT calls via mock spy). Revise: use a deeper/wider symbol tree (varied nesting, multiple children) and assert all 12 columns on every row — catches the real batch failure mode where tuple flattening shifts values across column boundaries.

**TestRealtimeWiring** — `asyncio.sleep(0.5)` is timing-dependent. Replace with proper event/signal-based synchronization.

**TestMCPServerWiring / TestCLIWiring boilerplate** — The `_TestServer(MCPServerBase)` inner class + Config setup is copy-pasted ~5 times (~200 lines of duplication). Extract into conftest fixture.

**Wiring test placement** — These test MCP server and CLI constructor wiring, not LSP population behavior. They live in `tests/lsp/test_wiring.py` because the population service is the subject being wired, but the shared `_TestServer` fixture makes the duplication manageable.

**LSP diagnostics in source** — `lsp_population.py` has two diagnostics to resolve:
- Line 430:41 — `Object of type 'object' is not callable` (ty) — the `operation` variable from the `ops` list needs proper typing
- Line 78:9 — `edges` is unused (ty) — declared before try block but only assigned inside it

## Implementation

### Step 1: Fix LSP diagnostics in source
- `lsp_population.py:78` — remove unused `edges` declaration, move into the try block where it's assigned (L112)
- `lsp_population.py:391-413` — type the `ops` list properly so `operation` calls pass type checking

### Step 2: Create tests/lsp/ scaffolding
- `tests/lsp/__init__.py` (empty)
- `tests/lsp/conftest.py` with helpers, fixtures, shared imports, `pytestmark`

### Step 3: Extract test files (one at a time, verify after each)
For each target file:
1. Move relevant classes to new file with their imports
2. Remove those classes from the original file
3. Import helpers from conftest
4. Run `uv run pytest tests/lsp/<file>.py -v` to verify
5. Repeat for next file

Order: test_core → test_type_signatures → test_edges → test_resolve → test_workspace_symbols → test_wiring → test_resilience

### Step 4: Fix quality issues in extracted files
- Revise TestBatchInsert: deeper symbol tree, assert all 12 columns per row
- Fix TestRealtimeWiring sleep
- Extract _TestServer fixture for wiring tests
- Deduplicate wiring test setup

### Step 5: Add missing behavioral tests
- **test_resolve.py** — Overlapping symbols: class containing a method, both covering the same line. Verify `_resolve_symbol` returns the innermost (smallest range) via the ORDER BY clause.
- **test_workspace_symbols.py** — Skip-existing path: run `populate_file` first, then `_populate_workspace_symbols`. Verify workspace symbols that overlap with documentSymbol results are skipped (the `SELECT id FROM symbols WHERE fqn = ? AND file_path = ?` guard).
- **test_core.py** — didClose under failure: mock `_batch_insert` to raise after didOpen. Verify `notify_did_close` is still called (the `finally` block contract).

### Step 6: Delete original and verify
- Delete `tests/test_lsp_population.py` (should be empty after Step 3)
- `uv run pytest tests/lsp/ -v` — all tests pass

### Step 7: Full suite and commit
- `uv run pytest -m "unit or integration or e2e" tests/ -v`
- Commit and push

## Success Criteria
- [ ] `tests/lsp/` module exists with 7 test files + conftest + __init__
- [ ] Each test file covers a single cohesion seam
- [ ] Shared setup in conftest.py (no duplication across files)
- [ ] TestBatchInsert revised to test flattening correctness across varied symbol trees
- [ ] TestRealtimeWiring has no timing-dependent sleep
- [ ] MCPServer/CLI wiring boilerplate extracted to shared fixture
- [ ] LSP diagnostics in lsp_population.py resolved
- [ ] Overlapping-symbol resolve test covers innermost selection
- [ ] Workspace symbol skip-existing test covers dedup guard
- [ ] didClose-under-failure test covers finally block contract
- [ ] All tests pass: `uv run pytest tests/lsp/ -v`
- [ ] Original file deleted
- [ ] Full test suite passes

## Anti-Patterns
- NO renaming test classes or methods (breaks git blame)
- NO adding tests for coverage sake — fix what's there, don't pad
- NO "pure code motion" mentality — improve while restructuring
- NO ignoring LSP diagnostics

## Log

- [2026-04-01T04:27:33Z] [Seth] Skeleton rewritten after test quality review. Key findings: TestBatchInsert tests impl detail not behavior, TestRealtimeWiring uses sleep(0.5), MCPServer/CLI wiring tests have ~200 lines of duplicated _TestServer boilerplate. Two LSP diagnostics in source (unused var L78, untyped callable L430). Restructure is a cleanup pass, not pure code motion.
