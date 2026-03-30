---
id: ch-17t
title: Fix tests making live API calls via user config
status: open
type: bug
priority: 1
---

## Context

8 test files load real user config (`.chunkhound.json`) and make live API calls to VoyageAI/OpenAI during `uv run pytest tests/`. This causes hangs (no timeout on live calls), burns API credits, and makes tests non-deterministic.

Entry points:
- `tests/test_utils._find_config_file()` — reads `.chunkhound.json` for real API keys
- `tests/test_utils.get_api_key_for_tests()` — 7 test files
- `tests/test_utils.get_embedding_config_for_tests()` — 5 test files
- `tests/provider_configs.Config()` — loads full config directly, 2 test files

Affected test files:
- `tests/test_multi_hop_semantic_search.py` (the one that hangs)
- `tests/test_dynamic_expansion_real.py`
- `tests/test_database_consistency.py`
- `tests/test_search_cli.py`
- `tests/test_mcp_server_directory_isolation.py`
- `tests/test_qa_deterministic.py`
- `tests/test_oversized_chunk_reproduction.py`
- `tests/test_mcp_integration.py`
- `tests/test_embedding_pipeline_integration.py`

## Requirements

- Tests NEVER load real user config or make live API calls
- All embedding/reranking in tests uses mock fixtures with canned data
- `_find_config_file()` removed or made inert in test context
- `get_reranking_providers()` returns mock providers, not real ones
- Full test suite (`uv run pytest tests/`) completes without network access

## Success Criteria
- [ ] `_find_config_file()` and `Config()` never called from test code
- [ ] All 9 affected test files use mock/fake providers only
- [ ] `uv run pytest tests/ --timeout=60` completes (no hangs)
- [ ] Zero live API calls during test suite (no network needed)
- [ ] All existing test assertions still pass with mock data
