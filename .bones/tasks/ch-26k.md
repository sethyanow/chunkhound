---
id: ch-26k
title: Convert test_codex_exec_simple_prompt to mocked/VCR — stop hitting live ChatGPT API
status: open
type: bug
priority: 1
---


## Context

`tests/integration/test_codex_exec_help.py::test_codex_exec_simple_prompt` spawns
`codex exec "Output exactly the uppercase string OK and nothing else."` against
the real `codex` CLI. Because the CLI inherits the developer's authenticated
session, every time this test runs it consumes ChatGPT/Codex subscription
tokens against the human user's account.

Discovered while running the `-m "unit or integration"` suite for Phase 5
verification on 2026-04-16. The test actually failed in this environment with:

```
ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",
"message":"The 'gpt-5.1-codex' model is not supported when using Codex with a
ChatGPT account."}}
```

— which is how it surfaced at all. Had the model been supported, it would have
silently succeeded while burning tokens on the user's account.

**Immediate mitigation applied (this task does NOT cover it):** the test was
re-marked `@pytest.mark.e2e` so the default `-m "unit or integration"` gate no
longer invokes it. The module-level `pytestmark = pytest.mark.integration` was
removed; each test now carries its own marker. Other tests in the file
(`test_codex_exec_help_available`, `test_codex_exec_status_reports_overlay_model`)
remain `integration` — see Open Questions.

## Requirements

R1. Eliminate live API consumption from the test. Options:
  - Mock `codex exec` via `asyncio.create_subprocess_exec` / `subprocess.run`
    patch (matches pattern in `tests/unit/test_claude_code_cli_provider.py`
    and `tests/unit/test_codex_stdin_first.py`).
  - OR use VCR cassettes per the `acceptance` tier pattern if the intent is
    true provider contract testing.
  - OR leave the test as `e2e` but gate it behind an explicit opt-in env var
    so CI runs don't burn a human's subscription.
R2. Test must remain useful — it exercises the overlay `CODEX_HOME` + config
    wiring that `CodexCLIProvider._build_overlay_home()` produces. Keep that
    coverage.
R3. Audit `test_codex_exec_status_reports_overlay_model` — verify whether
    `codex exec /status` hits the API or is handled entirely client-side by
    the codex CLI. If it hits the API, apply the same treatment.
R4. Tier invariant: `integration` tests MUST NOT hit live third-party APIs.
    Live API tests belong in `acceptance` (with cassettes) or `e2e` (opt-in).
    Codify this somewhere the next author will see it (AGENTS.md or a
    conftest-level assertion).

## Success Criteria

- [ ] `test_codex_exec_simple_prompt` runs without consuming user subscription tokens
- [ ] `test_codex_exec_status_reports_overlay_model` audited — either confirmed
      client-side-only or similarly mocked/moved
- [ ] Overlay `CODEX_HOME` + config contract coverage preserved (not just deleted)
- [ ] `-m "unit or integration"` suite never invokes live LLM APIs (verify by
      re-running with no credentials in the environment)
- [ ] Documentation note added: `integration` tier forbids live third-party API calls

## Anti-Patterns

- NO simply deleting the test — the overlay config assertions are load-bearing
- NO broad "mock everything" — the test's value is verifying the overlay
  `CODEX_HOME` is what the CLI sees. The mock must assert on the env/args
  passed to `subprocess.run`, not replace the whole flow with a no-op.
- NO leaving it as `e2e` without an opt-in gate — `e2e` is still CI-invocable,
  and CI should not burn a human's personal subscription.

## Key Considerations

- The existing mock pattern in `tests/unit/test_codex_stdin_first.py` uses
  `_DummyPipe` / `_DummyProc` with `monkeypatch.setattr` on
  `CodexCLIProvider._codex_available`. Reuse this.
- Whatever replaces the live call must still exercise
  `provider._build_overlay_home()` and assert on the `config.toml` contents
  (that's the actual regression target, not the "OK" round-trip).
- Claude subscription is NOT currently being hit — `tests/unit/test_claude_code_cli_provider.py`
  already patches `asyncio.create_subprocess_exec`. Use it as a reference.

## Open Questions

- Is `codex exec /status` a local slash-command (no API call) or does it
  initialize a session with the provider? Needs verification before deciding
  if `test_codex_exec_status_reports_overlay_model` needs the same treatment.
- Should `integration` vs `acceptance` vs `e2e` boundaries be enforced by a
  conftest hook (e.g., deny outbound network in integration), or left as a
  documented convention?

## Related

- ch-eun — Reclassify mismarked integration tests with fixture conversion (same family of issue)
- ch-8ud — Redesign live-API embedding tests with frozen corpus and algorithm-level assertions

## Log

- [2026-04-18T06:41:10Z] [Seth] Immediate mitigation: test re-marked @pytest.mark.e2e in tests/integration/test_codex_exec_help.py. Module-level pytestmark removed so each test has its own marker. simple_prompt → e2e; help_available + status_reports remain integration (status may also hit API — see Open Questions).
