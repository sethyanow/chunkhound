---
id: ch-b60
title: Acceptance tests require live API keys — convert to VCR cassettes
status: open
type: bug
priority: 0
---


## Context
Discovered during ch-52s test categorization. Two test files were hidden behind a conftest.py env-var skip (`CHUNKHOUND_RUN_INTEGRATION_TESTS`). When that mechanism was removed (replaced by `-m unit` default in addopts), these tests started running and failing because they need live API keys (OpenAI, VoyageAI).

Files: `tests/test_embedding_pipeline_integration.py` (5 tests), `tests/test_mcp_integration.py` (7 tests). Now marked `acceptance` so they're excluded from all standard test tiers.

Both files test full application workflows — embedding pipeline end-to-end and MCP server with realtime indexing. They use `httpx` under the hood (via the `openai` SDK and direct `httpx` in VoyageAI provider).

## Requirements
R1. Install `pytest-recording` (wraps vcrpy) as dev dependency.
R2. Record cassettes for both test files against live APIs (one-time, checked into repo).
R3. Configure `vcr_config` fixture to filter API keys from cassettes (`filter_headers: [authorization, x-api-key]`).
R4. Tests replay from cassettes by default (`record_mode=none`). No network calls, no API keys needed.
R5. Both files keep `acceptance` marker — they test full application specs, not isolated components.

## Implementation
1. `uv add --dev pytest-recording`
2. Add `vcr_config` fixture to `tests/conftest.py` filtering auth headers
3. Add `@pytest.mark.vcr` to each test in both files
4. Record cassettes: `uv run pytest -m acceptance --record-mode=once` (needs real API key once)
5. Commit cassettes to `tests/cassettes/`
6. Verify replay: `uv run pytest -m acceptance` passes with no API key set

## Key Considerations
- vcrpy 8.1.1 patches httpcore (httpx's transport layer) — async httpx calls intercepted transparently
- The `openai` Python SDK uses httpx internally — cassettes capture those calls
- VoyageAI provider uses httpx directly — also covered
- Cassettes go stale if test inputs change (re-record with `--record-mode=rewrite`)
- `--block-network` flag available to enforce no-network during replay

## Success Criteria
- [ ] `pytest-recording` in dev dependencies
- [ ] `tests/cassettes/` directory with recorded YAML cassettes
- [ ] `uv run pytest -m acceptance` passes with NO API keys set
- [ ] API keys filtered from all cassette files (grep for key patterns returns nothing)
- [ ] Both test files retain `acceptance` marker
