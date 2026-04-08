---
id: ch-eun
title: Reclassify mismarked integration tests with fixture conversion
status: active
type: task
priority: 1
owner: Seth
---






## Context

118 integration-marked files (2975 tests in `unit or integration`, 871 integration-only). The `integration` marker should mean multi-module collaboration. Most tests call a single external system and test our logic against it.

Pre-commit suite (`-m "unit or integration"`) takes ~5 minutes. Goal: seconds.

### Tier definitions

- **unit**: One module, its logic, no external system calls. External deps (DuckDB, LanceDB, tree-sitter, git, filesystem) are replaced by fixtures or mocks.
- **integration**: Multiple modules collaborating — indexing coordinator + provider + embedding service.
- **e2e**: Full process entry points — CLI, MCP, daemon lifecycle.

### Audit results (Phase 1 complete)

| Verdict | Files | Tests | Action |
|---------|-------|-------|--------|
| ALREADY UNIT (mocked/pure) | 28 | ~152 | Change marker only |
| CONVERT TO UNIT (fixture) | 67 | ~552 | Capture fixtures, rewire, change marker |
| KEEP INTEGRATION (multi-module) | 12 | ~57 | No change |
| REMARK E2E (subprocess) | 11 | ~26 | Change marker only |
| DELETE | 0 | 0 | All tests test our logic |

### CONVERT TO UNIT file lists (67 files, ~552 tests)

#### Tree-sitter → capture Chunk output to JSON (12 files, ~259 tests)

```
test_constant_extraction (93)         test_elixir_parser (25)
test_lua_parser (15)                  test_vue_parser (21)
test_vue_integration (37)             test_python_docstring_indexing (10)
test_python_import_resolution (41)    test_schema_symbols (5)
test_oversized_chunk_reproduction (3) test_makefile_integration (3)
unit/test_rapid_yaml_parser (5)       unit/test_skip_unchanged_files (1)
```

Note: `test_python_import_resolution` also uses filesystem (tmp_path file creation for import resolution).

#### DuckDB → capture provider responses or swap to FakeDatabaseProvider (18 files, ~128 tests)

```
unit/test_coordinator_per_file_txn (1)       unit/test_db_metrics_logging (1)
unit/test_disk_usage_limit (22)              unit/test_duckdb_delete_chunks_batch (1)
unit/test_json_filtering (6)                 unit/test_reindexing_workflow (8)
unit/test_global_gitignore (24)              integration/test_chunk_metadata_roundtrip (8)
integration/test_duckdb_graph_protocol (14)  integration/test_duckdb_skip_unchanged (7)
integration/test_duckdb_symbol_protocol (10) integration/test_delete_file_cascade (2)
integration/test_root_file_discovery_defaults (1)
integration/test_cli_timeout_prompt_real (1)
test_parallel_discovery (11)                 test_path_filter_monorepo_mismatch (1)
test_path_prefix_scoping (7)                 test_performance_discovery (4)
```

Note: `unit/test_global_gitignore` also uses git subprocess. `integration/test_cli_timeout_prompt_real` uses monkeypatch but still instantiates real DuckDB.

#### LanceDB → capture provider responses (8 files, ~71 tests)

```
unit/test_chunk_hashing (19)                 unit/test_lancedb_dimension_detection (10)
integration/test_lancedb_checksums (2)       integration/test_lancedb_deduplication (9)
integration/test_lancedb_graph_protocol (13) integration/test_lancedb_regex (4)
integration/test_lancedb_schema_optimization (4)
integration/test_lancedb_symbol_protocol (10)
```

Note: LanceDB tests use shared `lancedb_provider` fixture from `tests/integration/conftest.py` — that fixture must be updated.

#### Git subprocess → capture command output to JSON (17 files, ~38 tests)

```
unit/test_git_hardening (4)             unit/test_checksum_verify_flow (1)
ignore/test_git_parity (1)              ignore/test_git_parity_broadened (3)
ignore/test_git_parity_nested (1)       ignore/test_git_parity_slash_anchor (1)
ignore/test_libgit2_backend_parity (1)  ignore/test_repo_aware_engine (1)
ignore/test_walk_with_repo_aware_engine (1)
ignore/test_windows_normalization (2)   discovery/test_git_discovery_basic (2)
realtime/test_realtime_git_aware (6)    realtime/test_realtime_ignore_parity (2)
realtime/test_realtime_repo_boundary (2)
integration/test_git_pathspec_pushdown (1)
integration/test_discovery_layouts (8)  code_mapper/test_scope_collect_files (1)
```

Note: `ignore/` tests use `tests/ignore/conftest.py` which has `_require_git_available` session fixture — converted tests must not depend on this.

#### Filesystem/autodoc → capture rendered output (12 files, ~55 tests)

```
autodoc/test_autodoc_assets_only (6)         autodoc/test_autodoc_audience (2)
autodoc/test_autodoc_auto_map (4)            autodoc/test_autodoc_out_dir_safety (2)
autodoc/test_autodoc_site_assets_resources (4)
autodoc/test_docsite_generation (9)          autodoc/test_docsite_ia_artifacts (5)
autodoc/test_generator_output_manifest (2)   autodoc/test_site_writer_output_manifest (2)
test_project_detection (13)                  test_ssl_connection_error (3)
cli/test_verify_database_exists_returns_canonical_path (3)
```

### Existing infrastructure

- `tests/fixtures/fake_providers.py` — `FakeEmbeddingProvider`, `FakeLLMProvider`, `ConstantEmbeddingProvider`, `ValidatingEmbeddingProvider`
- `tests/fixtures/` — source files for vue, elixir, lua, yaml, cpp, csharp, java, kotlin, php, pdf
- `tests/provider_configs.py` — `get_reranking_providers()` already returns ONLY `FakeEmbeddingProvider`, no real API calls

### Shared fixtures that create real providers (must update)

- `tests/integration/conftest.py` — `lancedb_provider` fixture creates real `LanceDBProvider`. All LanceDB integration tests use this. Needs a `fake_lancedb_provider` equivalent or replacement.
- `tests/integration/conftest.py` — `fragmented_lancedb_provider`, `heavily_fragmented_lancedb_provider`, `provider_with_duplicate_chunks` — all build on real `lancedb_provider`
- `tests/ignore/conftest.py` — `_require_git_available` session fixture asserts git is on PATH. Tests being converted to unit should not hit this.

### Shared test helpers (cross-file imports)

- `tests/integration/code_mapper_scope_helpers.py` — `write_scope_repo_layout()`, imported by `test_code_mapper_duckdb_scope`, `test_code_mapper_lancedb_scope`, `test_code_mapper_pipeline`
- `tests/fixtures/fragmentation_helpers.py` — `create_fragmented_state()`, `create_file_with_duplicates_across_fragments()`, imported by integration conftest
- `tests/test_utils.py` — `get_api_key_for_tests()`, `get_embedding_config_for_tests()`, imported by ~19 files (mostly e2e/acceptance tests, not conversion targets)

### Pytest configuration

- `pyproject.toml` addopts: `["-m", "unit"]` — bare `pytest` runs unit only by default
- `--strict-markers` — undefined markers error
- Defined markers: `unit`, `integration`, `e2e`, `acceptance`, `slow`, `heavy`, `asyncio`
- Implication: after conversion, newly-remarked unit tests automatically enter the default `pytest` run

### File lists per verdict

#### ALREADY UNIT (28 files) — change marker only

```
unit/test_batch_processor_timeout         unit/test_codex_overlay_modes
unit/test_cli_timeout_prompt              unit/test_codex_skip_git_fallback
unit/test_codex_error_redaction           unit/test_codex_stdin_first
unit/test_codex_overlay_cleanup           unit/test_claude_code_cli_provider
unit/llm/test_opencode_cli_provider       unit/test_daemon_overlap_guard
unit/test_file_patterns_wildcard_dirs     unit/test_hashing_performance
unit/test_symlink_path_resolution         autodoc/test_autodoc_cli_autorun_options
operations/test_code_mapper_integration   operations/test_code_mapper_config_root
operations/test_code_mapper_overview_artifact
config/test_code_mapper_config_discovery  config/test_exclude_from_local_config_overlay
test_embeddings                           test_mcp_startup_error_response
test_config_integration                   test_config_missing_behavior
code_mapper/test_hyde_prompt_snippets     code_mapper/test_orchestrator_scope_resolution
code_mapper/test_scope_collect_files_indexing_semantics
integration/test_mcp_code_research_codex_provider
integration/test_lancedb_embeddings
```

#### REMARK E2E (11 files) — change marker only

```
test_e2e_chunk_size_constraints           test_search_cli
test_mcp_server_directory_isolation       services/test_eval_search_integration
integration/test_codex_exec_help          integration/test_nonrepo_workspace_gitignore
integration/test_root_indexing_default_patterns
integration/test_prune_pipeline           integration/test_yaml_cli_indexing
integration/test_config_exclude_mixed_root_repo_subtree
diagnose/test_diagnose_default_exclude_mismatch
```

#### KEEP INTEGRATION (12 files) — no change

| File | Why multi-module |
|------|-----------------|
| test_core_workflow | DuckDB + parser + coordinator + search |
| test_multi_hop_semantic_search | DuckDB + FakeEmbedding + search + coordinator |
| test_dynamic_expansion_real | DuckDB + FakeEmbedding + search (parametrized) |
| test_database_consistency | database_factory + config + embedding service |
| test_pdf_indexing_integration | PDF parser + DuckDB + coordinator + search |
| services/test_scoped_deep_research | deep_research_impl + DuckDB + FakeLLM + FakeEmbedding |
| services/test_discover_repo_boundary | Config + registry + coordinator |
| code_mapper/test_code_mapper_pipeline | Full pipeline: DuckDB + FakeLLM + FakeEmbedding + coordinator + search |
| code_mapper/test_orchestrator | Orchestrator + DuckDB + FakeEmbedding + search |
| integration/test_directory_service_root_indexing | DirectoryIndexingService + coordinator + DuckDB |
| integration/test_code_mapper_duckdb_scope | Code mapper scope + DuckDB (uses scope_helpers) |
| integration/test_code_mapper_lancedb_scope | Code mapper scope + LanceDB (uses scope_helpers) |

### Fixture boundaries per category

| Category | What to mock/capture | Boundary point | Output shape |
|----------|---------------------|----------------|-------------|
| Service-level | `DatabaseProvider` protocol | Provider interface | `FakeDatabaseProvider` (dict-backed, no fixture files) |
| Tree-sitter | Parser output | `create_parser_for_language()` → `parser.parse()` | `list[Chunk]` serialized to JSON |
| DuckDB provider | Provider method returns | Each `provider.method()` call | `{method_name: {args: return_value}}` per test |
| LanceDB provider | Provider method returns | Each `provider.method()` call | Same shape as DuckDB |
| Git subprocess | `subprocess.run` for git | `subprocess.run(["git", ...])` | `{command: CompletedProcess(stdout, stderr, returncode)}` |
| Filesystem/autodoc | Rendered file contents | `write_astro_site()` and similar | Rendered output files as strings |

### tmp_path boundary rule

`tmp_path` itself is not an external system — it's pytest test infrastructure. The boundary:
- Writing a config file or source file to tmp_path as **test setup** → not external, fine in unit tests
- Calling a function that **writes output** to the filesystem (autodoc rendering, site generation) and asserting on the output → the write IS the behavior under test, needs fixture if the function involves external systems (tree-sitter, DuckDB)
- Pure path manipulation, directory creation → not external

### FakeDatabaseProvider contract

Must implement `DatabaseProvider` protocol from `chunkhound/interfaces/database_provider.py`. Key method groups called by tests:

**File CRUD**: `insert_file`, `get_file_by_path`, `get_file_by_id`, `update_file`, `delete_file_completely`
**Chunk CRUD**: `insert_chunk`, `insert_chunks_batch`, `get_chunk_by_id`, `get_chunks_by_file_id`, `delete_file_chunks`, `delete_chunks_batch`, `delete_chunk`, `update_chunk`
**Embedding**: `insert_embedding`, `insert_embeddings_batch`, `get_embedding_by_chunk_id`, `get_existing_embeddings`, `delete_embeddings_by_chunk_id`
**Search**: `search_semantic`, `search_regex`, `search_text`, `find_similar_chunks`, `get_chunks_in_range`
**Symbol/Edge**: `insert_symbols_batch`, `delete_symbols_by_file`, `delete_edges_by_file`, `query_symbols_by_file`, `query_symbol_fqns_by_file`, `query_symbols_by_fqn_exists`, `insert_edges_batch`
**Graph**: `graph_walk`, `graph_reachability`, `graph_boundary`, `graph_overview`, `symbol_overlap`, `chunk_resolution`, `symbol_stats`
**Lifecycle**: `connect`, `disconnect`, `create_schema`, `execute_query`, `begin_transaction`, `optimize_tables`
**Stats**: `get_stats`, `get_all_chunks_with_metadata`, `get_scope_stats`, `get_scope_file_paths`

Async variants exist for most methods — implement as `await sync_version()`.

### Semantic assertion tests (don't weaken to snapshots)

Tests that assert on semantic properties ("chunk contains symbol X", "chunk type is FUNCTION") are STRONGER than snapshot comparisons. When converting these to fixtures, preserve the semantic assertions — don't replace with `assert chunks == fixture_chunks`. The fixture provides the data; the assertion stays semantic.

Examples: `test_constant_extraction` asserts on `UniversalConcept` extraction per language. `test_vue_parser` asserts on section types and symbol names. These assertions should survive conversion.

## Requirements

1. **Audit before converting.** ✅ Done — audit table in Context section above.
2. **Capture fixtures.** Run each kept test once, capture external system output as JSON fixtures.
3. **Rewrite tests.** Load fixtures instead of live calls.
4. **Re-mark.** Only change marker AFTER the test no longer calls the external system.
5. **Pre-commit speed.** Unit suite should run in seconds, not minutes.

## Phases

### Phase 1: Audit ✅

Complete. 118 files classified. 0 deletions. Results in Context section.

### Phase 2: Build infrastructure

1. `FakeDatabaseProvider` in `tests/fixtures/fake_providers.py` — dict-backed `DatabaseProvider` protocol implementation, same pattern as existing `FakeEmbeddingProvider`
2. Parser fixture capture utility — run parser, serialize Chunk output to JSON
3. Git subprocess mock — replay captured stdout/stderr from JSON fixtures

### Phase 3: Remark-only changes (no conversion needed)

- 28 files → change marker to `unit` (already mocked/pure logic)
- 11 files → change marker to `e2e` (spawn subprocesses)

### Phase 4: Fixture capture + test rewiring (by category)

Work one category at a time. For each file:
1. Run the test, capture external system output as JSON fixture
2. Rewrite test to load fixture, remove external call
3. Verify test still passes against fixture
4. Change marker to `unit`
5. Verify full unit suite still passes

Order (largest impact first):
1. Tree-sitter parser (12 files, ~259 tests) — capture Chunk output → JSON
2. DuckDB (18 files, ~128 tests) — capture provider responses or swap to FakeDatabaseProvider
3. LanceDB (8 files, ~71 tests) — capture provider responses → JSON
4. Filesystem/autodoc (12 files, ~55 tests) — capture rendered output → JSON
5. Git subprocess (17 files, ~38 tests) — capture command output → JSON

Deliverables: 1 fake class (`FakeDatabaseProvider`), ~53 fixture JSON files, 67 files converted + 39 files remarked.

### Phase 5: Verify

- `uv run pytest -m unit` runs in seconds
- `uv run pytest -m "unit or integration"` still passes — no tests lost
- ~57 tests remain marked integration (12 files, legitimate multi-module)

## Success Criteria

- [x] Audit table delivered and reviewed by user
- [ ] `FakeDatabaseProvider` built extending existing fake_providers.py pattern
- [ ] Every converted test loads fixtures, no live external calls
- [ ] No marker changes without corresponding fixture conversion (except pure-logic tests and e2e reclassification)
- [ ] `uv run pytest -m unit` runs fast (no external system calls blocking)
- [ ] `uv run pytest -m "unit or integration"` still passes — no tests lost without audit justification
- [ ] ~57 tests remain marked integration (12 files, legitimate multi-module)

## Anti-Patterns

- Swapping markers without converting the test — marker changes ONLY after external call is removed
- "Fast enough" is not unit — if it calls an external system, it needs fixtures/mocks regardless of speed
- Snapshot-testing where property assertions are stronger
- Testing DuckDB/LanceDB's contract — we trust the engine, we test our logic
- Rationalizing away the tier definitions with "pragmatic" interpretations

## Key Considerations

- Tree-sitter AST nodes are C objects, not JSON-serializable. Fixture boundary must be at Chunk output level or higher.
- Some tests assert on semantic properties ("contains symbol X") which is better than snapshot comparison. Preserve semantic assertions where they exist — don't weaken to snapshots.
- DuckDB/LanceDB provider tests verify our SQL/PyArrow against the engine. The response is deterministic — fixture it. Don't confuse "testing our SQL" with "testing DuckDB."
- Existing `tests/fixtures/fake_providers.py` has the established pattern. `FakeDatabaseProvider` follows the same approach.

## Log

- [2026-04-07T01:15:39Z] [Seth] Phase 1 audit v1 — incorrectly rationalized away tier definitions by calling external calls "fast enough." Rejected by user.
- [2026-04-07T02:04:51Z] [Seth] Phase 1 audit v2 — correct tier definitions applied. 28 ALREADY UNIT, 67 CONVERT, 12 KEEP INTEGRATION, 11 REMARK E2E, 0 DELETE.
- [2026-04-07T04:03:00Z] [Seth] Skeleton updated with full audit results, revised phases (5 phases), infrastructure needs (FakeDatabaseProvider + ~53 fixture JSON files), corrected anti-patterns and success criteria.
- [2026-04-07T04:45:00Z] [Seth] Added all 67 CONVERT file lists by name per category, fixture boundaries, FakeDatabaseProvider contract, conftest/shared helper dependencies, tmp_path boundary rule, pytest config, KEEP INTEGRATION rationales, semantic assertion preservation notes. Verified: provider_configs.py returns fake-only, lancedb_provider conftest creates real provider (needs update), addopts defaults to -m unit.
- [2026-04-08T18:40:34Z] [Seth] Phase 2 infrastructure built: FakeDatabaseProvider (dict-backed, 47 tests including reachability semantics from real consumer test_duckdb_graph_protocol), parser fixture utils (Chunk JSON roundtrip, 9 tests), git fixture utils (sequential + args-matching replay, wrap_subprocess recorder, check=True CalledProcessError, 16 tests). Fixed graph_reachability BFS (was returning entry points instead of unreachable), fixed symbol_stats key names to match real provider, execute_query raises NotImplementedError to force test rewrites. Git utils added match_by_args mode for check-ignore oracle tests, wrap_subprocess for recording. 72 new unit tests total.
