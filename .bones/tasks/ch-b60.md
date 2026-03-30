---
id: ch-b60
title: Acceptance tests require live API keys — convert to VCR cassettes
status: closed
type: bug
priority: 0
owner: Seth
---







## Context
Discovered during ch-52s test categorization. Two test files were hidden behind a conftest.py env-var skip (`CHUNKHOUND_RUN_INTEGRATION_TESTS`). When that mechanism was removed (replaced by `-m unit` default in addopts), these tests started running and failing because they need live API keys (OpenAI, VoyageAI).

Files: `tests/test_embedding_pipeline_integration.py` (6 tests), `tests/test_mcp_integration.py` (6 test methods in TestMCPIntegration). Now marked `acceptance` so they're excluded from all standard test tiers.

Both files test full application workflows — embedding pipeline end-to-end and MCP server with realtime indexing. They use async `httpx` under the hood (via the `openai` SDK and direct `httpx` in VoyageAI provider).

**Root cause:** Commit dd62921 replaced real credential discovery (env vars → `.chunkhound.json`) in `tests/test_utils.py` with hardcoded fakes (`base_url: "http://fake.test/v1"`, `model: "fake-embeddings"`). This was correct for unit/integration tests but stranded acceptance tests that need real embedding API responses. The skipif guard in `test_mcp_semantic_search_finds_new_files` is also broken — `get_api_key_for_tests()[0]` is never None now.

## Requirements
R1. Install `pytest-recording` (wraps vcrpy) as dev dependency.
R2. Restore real credential discovery for acceptance tests only — env vars (`CHUNKHOUND_EMBEDDING__*`) override fake defaults. Keep fake config as default for all other test tiers.
R3. Record cassettes for both test files against live APIs (one-time, checked into repo). **USER-GATED: agent sets up everything, user runs the record command with real API keys.**
R4. Configure `vcr_config` fixture to filter API keys from cassettes (`filter_headers: [authorization, x-api-key]`).
R5. Tests replay from cassettes by default (`record_mode=none`). No network calls, no API keys needed.
R6. Both files keep `acceptance` marker — they test full application specs, not isolated components.
R7. Fix broken skipif guard in `test_mcp_semantic_search_finds_new_files` (remove it — VCR handles the no-key case).
R8. Fix ty lint errors in `test_mcp_integration.py` (4 unresolved-attribute errors on `.get()` union type).

## Implementation
1. `uv add --dev pytest-recording`
2. Add `acceptance_embedding_config` fixture to `tests/conftest.py` — reads `CHUNKHOUND_EMBEDDING__API_KEY`, `CHUNKHOUND_EMBEDDING__PROVIDER`, `CHUNKHOUND_EMBEDDING__MODEL`, `CHUNKHOUND_EMBEDDING__BASE_URL` env vars; falls back to fake config when unset. Only used by acceptance tests.
3. Update both acceptance test files to use `acceptance_embedding_config` instead of `get_embedding_config_for_tests()`.
4. Add `vcr_config` fixture to `tests/conftest.py` filtering auth headers (`authorization`, `x-api-key`).
5. Add `@pytest.mark.vcr` to each test in both files.
6. Remove broken `skipif(get_api_key_for_tests()[0] is None)` from `test_mcp_semantic_search_finds_new_files`.
7. Fix 4 ty lint errors in `test_mcp_integration.py` — type-narrow `execute_tool` return before calling `.get()`.
8. **STOP — USER-GATED STEP:** Present recording command to user:
   ```bash
   CHUNKHOUND_EMBEDDING__API_KEY=<key> CHUNKHOUND_EMBEDDING__PROVIDER=openai \
   CHUNKHOUND_EMBEDDING__MODEL=text-embedding-3-small \
   CHUNKHOUND_EMBEDDING__BASE_URL=https://api.openai.com/v1 \
   uv run pytest -m acceptance --record-mode=once -v
   ```
   User runs this with their real API key. Agent does NOT proceed until cassettes are recorded.
9. After user confirms recording succeeded: verify cassettes exist in `tests/cassettes/`, grep for leaked keys.
10. Verify replay: unset API key env vars, run `uv run pytest -m acceptance -v` — should pass from cassettes.
11. Commit cassettes + all changes.

## Key Considerations
- vcrpy (since v4.1.0) patches `httpx.AsyncClient._send_single_request` — async httpx calls intercepted at the client level, not httpcore. Verified via deepwiki/kevin1024/vcrpy.
- The `openai` Python SDK uses async httpx internally — cassettes capture those calls
- VoyageAI provider uses httpx directly — also covered
- Cassettes go stale if test inputs change (re-record with `--record-mode=rewrite`)
- `--block-network` flag available to enforce no-network during replay
- VCR matches requests by URL + method + body. Acceptance tests MUST use real API URLs during both recording and replay for cassettes to match. The `acceptance_embedding_config` fixture handles this by using real URLs (from env vars during recording, baked into cassettes during replay).
- The 4 ty lint errors are `.get()` on `dict[str, Any] | str` union — `execute_tool` return type needs narrowing with `isinstance` check before dict operations.
- **API keys in request bodies:** OpenAI sends key via `Authorization: Bearer` header, but verify VoyageAI doesn't embed keys in POST body. After recording, grep cassette request bodies too, not just headers. Add `filter_post_data_parameters` to `vcr_config` if needed.
- **Incomplete cassettes from async timing:** MCP integration tests use `asyncio.sleep` between file creation and search. If background embedding generation fires after the sleep, those HTTP calls may not be captured in the cassette. Verify by running replay immediately after recording — if a test fails with "no matching interaction," its async timing needs adjustment.
- **Replay is slow but correct:** During replay, VCR returns instantly but `asyncio.sleep(3.0)` calls still execute. Acceptance suite will take ~30s in sleeps alone. This is a known tradeoff, not a bug.

## Anti-Patterns
- NO committing real API keys in cassettes — `vcr_config` must filter auth headers
- NO recording cassettes in CI — replay only (`record_mode=none` is default)
- NO modifying `_FAKE_EMBEDDING_CONFIG` in test_utils.py — acceptance config is a separate fixture
- NO agent-initiated recording — user runs the recording command with their own keys

## Edge Cases
- If openai SDK changes request format, cassettes go stale → re-record with `--record-mode=rewrite`
- If a test creates files with non-deterministic content (timestamps, UUIDs), cassette matching may fail → ensure test fixtures produce deterministic input
- MCP integration tests use `asyncio.sleep` for debounce waits — VCR only intercepts HTTP, not timing. Sleeps still execute during replay.
- `test_mcp_integration.py` tests that only use regex search (no embeddings) may not need VCR at all — but marking all acceptance tests with `@pytest.mark.vcr` is harmless (VCR is a no-op when no HTTP calls match)

## Success Criteria
- [x] `pytest-recording` in dev dependencies
- [x] `acceptance_embedding_config` fixture provides real config from env vars, falls back to VoyageAI defaults
- [x] `tests/cassettes/` directory with 12 recorded YAML cassettes
- [x] `uv run pytest -m acceptance` passes with NO API keys set (12/12, 25s replay)
- [x] API keys filtered from all cassette files — no Bearer, authorization, or key patterns found
- [x] Both test files retain `acceptance` marker
- [x] Broken skipif guard removed from `test_mcp_semantic_search_finds_new_files`
- [x] ty lint errors in `test_mcp_integration.py` resolved (0 unresolved-attribute errors)
- [x] All existing tests pass (1406 passed, 3 skipped)
- [x] All existing tests still pass (`uv run pytest -m unit -x -q` — 1406 passed)

## Log

- [2026-03-30T21:00:52Z] [Seth] VCR cassettes recorded and verified. 12 cassettes against VoyageAI voyage-4. Replay passes without API keys (25s). Auth headers filtered clean. vcr_config record_mode bug found and fixed mid-task (fixture overrode CLI flag). VoyageAI SDK uses requests not httpx — VCR patches both.
- [2026-03-30T21:02:07Z] [Seth] Debrief: vcr_config record_mode fixture-overrides-CLI gotcha was the main surprise. VoyageAI SDK uses requests not httpx. SRE caught config restoration gap. User corrections: use VoyageAI not OpenAI, check git history before speculating, use deepwiki not web search. Memories saved: reference_vcr_cassettes, feedback_check_before_guessing, updated project_test_tiers.
