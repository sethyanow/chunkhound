---
id: ch-rym
title: Restructure test_lsp_population.py into tests/lsp/ with quality fixes
status: active
type: task
priority: 1
owner: Seth
parent: ch-0um
---



## Context
`tests/test_lsp_population.py` — 3627 lines, 33 classes, 79 tests across unrelated concerns. Grew organically across ch-5b3, ch-nvc, ch-zlg, ch-ko4 without decomposition. Not just too big — the tests have accumulated quality issues that should be fixed during restructuring.

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

### Step 3: Extract and improve each file (one at a time, verify after each)
For each target file:
1. Create new file with its classes, imports, and conftest helper imports
2. Apply quality fixes for classes in this file (see Design § Quality fixes)
3. Add missing behavioral tests that belong to this file (see below)
4. Remove extracted classes from the original file
5. Run `uv run pytest tests/lsp/<file>.py -v` to verify

**test_core.py** — Revise TestBatchInsert: deeper/wider symbol tree, assert all 12 columns per row. Add didClose-under-failure test (mock `_batch_insert` to raise, verify `notify_did_close` still called).

**test_type_signatures.py** — No quality fixes needed.

**test_edges.py** — No quality fixes needed.

**test_resolve.py** — Add overlapping-symbol test: class containing method both covering same line, verify `_resolve_symbol` returns innermost via ORDER BY.

**test_workspace_symbols.py** — Add skip-existing test: `populate_file` then `_populate_workspace_symbols`, verify overlap skipped via the `SELECT id FROM symbols WHERE fqn = ? AND file_path = ?` guard.

**test_wiring.py** — Extract `_TestServer` + Config setup into conftest fixture. Fix TestRealtimeWiring `asyncio.sleep(0.5)` → event/signal-based synchronization. Deduplicate wiring test setup.

**test_resilience.py** — No quality fixes needed.

### Step 4: Delete original and verify
- `git rm tests/test_lsp_population.py`
- `uv run pytest tests/lsp/ -v` — all tests pass

### Step 5: Full suite and commit
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

## Key Considerations

### File extraction
- **Implicit ordering dependence:** Tests may pass in monolith due to discovery order but fail when split. After extracting ALL files, run full `tests/lsp/` directory to catch cross-file ordering bugs.
- **Import completeness:** Each extracted file needs its own import set from the shared module-level imports. The Step 3 per-file `pytest -v` catches this at collection time.

### conftest.py
- **pytestmark inheritance:** Verify `pytestmark = pytest.mark.unit` in conftest applies to all directory tests via `--collect-only`.
- **_TestServer fixture scope:** Must be `function`-scoped (default). Explicit annotation prevents accidental `session`/`module` escalation that would share dirty state.

### TestBatchInsert revision
- **Symbol tree must be heterogeneous:** At minimum: 3 levels deep, a node with 0 children, a node with 3+ children, siblings at different depths. Regular trees hide column-shift bugs in specific nesting patterns.

### TestRealtimeWiring sleep fix
- **Event must have timeout:** Use `asyncio.wait_for(event.wait(), timeout=5.0)` — catches "never called" and "deadlock" failures. Mock `side_effect` sets the event at exact call moment.

## Anti-Patterns
- NO renaming test classes or methods (breaks git blame)
- NO adding tests for coverage sake — fix what's there, don't pad
- NO "pure code motion" mentality — improve while restructuring
- NO ignoring LSP diagnostics

## Log

- [2026-04-01T04:27:33Z] [Seth] Skeleton rewritten after test quality review. Key findings: TestBatchInsert tests impl detail not behavior, TestRealtimeWiring uses sleep(0.5), MCPServer/CLI wiring tests have ~200 lines of duplicated _TestServer boilerplate. Two LSP diagnostics in source (unused var L78, untyped callable L430). Restructure is a cleanup pass, not pure code motion.
