---
id: ch-26k
title: Stop codex overlay tests from burning live API tokens + remove hardcoded default model
status: open
type: bug
priority: 1
---



## Context

`tests/integration/test_codex_exec_help.py` runs three tests that all spawn
`codex exec` as a subprocess with `CODEX_HOME` pointed at an overlay built by
`CodexCLIProvider._build_overlay_home()`. Because the CLI inherits the
developer's authenticated ChatGPT session, two of those tests (`simple_prompt`
and `status_reports_overlay_model`) consumed subscription tokens on every
`-m "unit or integration"` run.

Discovered during Phase 5 verification on 2026-04-16. `simple_prompt` failed
with a 400 from the Codex endpoint — which is how the behavior surfaced at
all. Had the model been supported, it would have silently succeeded while
burning tokens.

**Immediate mitigation (landed 2026-04-18, commit c42848a7):**
`test_codex_exec_simple_prompt` re-marked `@pytest.mark.e2e` so the default
`-m "unit or integration"` gate no longer invokes it. Module-level
`pytestmark = pytest.mark.integration` removed; each test now carries its
own marker.

**Design decided 2026-04-18 (this task):**

The three tests couple two separable concerns:

1. Our overlay builder emits a valid `config.toml` — code we own.
2. The codex CLI honors `CODEX_HOME/config.toml` — third-party behavior.

Decoupling them eliminates the live-API dependency:

- **Unit-test the overlay builder directly.** Parse the emitted `config.toml`
  and assert on contents. Covers the override paths currently exercised by
  live invocation.
- **Keep `test_codex_exec_help_available`.** `codex exec --help` prints local
  usage text; it never hits the API. Legitimate integration coverage for
  CLI-on-PATH + env plumbing.
- **Delete `test_codex_exec_simple_prompt` and
  `test_codex_exec_status_reports_overlay_model`.** Once unit tests cover
  the overlay contract, live round-trips add no CI value.
- **Stop pinning a default model in the overlay.** `_build_overlay_home()`
  currently writes `model = "gpt-5.1-codex"` unconditionally. When that
  model is deprecated (it already is for ChatGPT accounts), every overlay
  directs codex at a dead model. Fix: only emit `model = ...` and
  `model_reasoning_effort = ...` when the resolution source is explicit
  (constructor arg) or env-var override — not when it's falling back to
  our internal default. `describe_model_resolution` and
  `describe_reasoning_effort_resolution` already return the source; this
  is a switch on a return value they already provide.
- **Enforce the tier rule via conftest.** A custom hook blocks outbound TCP
  for `@pytest.mark.integration` tests so the next author cannot slip a
  similar live-API test in by accident.

## Requirements

R1. Add unit tests on `CodexCLIProvider._build_overlay_home()` at
    `tests/unit/test_codex_overlay_config.py`. Tests parse the emitted TOML
    with `tomllib` and cover:
    - **Default construction** — overlay emits `[history] persistence = "none"`,
      no `mcp_servers`, AND does NOT emit `model` or `model_reasoning_effort`
      (because both are default-sourced).
    - **Explicit model via constructor** — `CodexCLIProvider(model="foo")`
      emits `model = "foo"`.
    - **Explicit reasoning effort via constructor** —
      `CodexCLIProvider(reasoning_effort="high")` emits
      `model_reasoning_effort = "high"`.
    - **Env-var model override** — `CHUNKHOUND_CODEX_DEFAULT_MODEL=bar` emits
      `model = "bar"` even with no constructor arg.
    - **Env-var reasoning effort** — `CHUNKHOUND_CODEX_REASONING_EFFORT=medium`
      emits `model_reasoning_effort = "medium"` even with no constructor arg.

    Tests must NOT hardcode string literals like `"gpt-5.1-codex"`. Where a
    default value is being asserted as present/absent, read it from the
    provider's own resolver (e.g., via `describe_model_resolution(None)`) so
    bumping the default in the provider updates the test automatically.

R2. Modify `CodexCLIProvider._build_overlay_home()` at
    `chunkhound/providers/llm/codex_cli_provider.py:214`. Currently emits
    `model` and `model_reasoning_effort` unconditionally. Change to use
    `describe_model_resolution()` and `describe_reasoning_effort_resolution()`,
    inspect the returned `source`, and omit the key when source is `"default"`.
    Emit when source is `"explicit"`, `"env:..."`, or `"fallback"`.

R3. Delete `test_codex_exec_simple_prompt` and
    `test_codex_exec_status_reports_overlay_model` from
    `tests/integration/test_codex_exec_help.py`. The module keeps only
    `test_codex_exec_help_available`.

R4. Add a conftest hook that blocks outbound TCP for tests carrying
    `@pytest.mark.integration`. Custom fixture (autouse via marker filter) in
    `tests/conftest.py` that monkeypatches `socket.socket.connect` to raise
    when the destination is non-loopback. Do NOT add `pytest-socket` as a
    dependency for this; a ~15 line fixture suffices.

    Include a regression test at `tests/integration/test_tier_network_block.py`
    that attempts an outbound TCP connection inside an integration-marked
    test and asserts the connect raises.

R5. Document the tier rule in `AGENTS.md`. New "Test Tiers" section with:
    - `unit`: no subprocess, no I/O beyond tmp_path
    - `integration`: local subprocess and loopback OK, NO outbound network
    - `acceptance`: VCR-cassette-backed, recorded once against real APIs
    - `e2e`: live APIs, opt-in only, not run by CI by default

## Success Criteria

- [ ] `uv run pytest tests/unit/test_codex_overlay_config.py -v` passes with
      5 tests covering the variants in R1
- [ ] `_build_overlay_home()` omits `model` and `model_reasoning_effort`
      keys when resolution source is `"default"` (verified by reading the
      emitted TOML under default construction)
- [ ] `test_codex_exec_simple_prompt` and
      `test_codex_exec_status_reports_overlay_model` are removed from the
      codebase (grep returns no matches)
- [ ] `uv run pytest -m integration tests/integration/test_codex_exec_help.py`
      passes with only `test_codex_exec_help_available` running
- [ ] `tests/integration/test_tier_network_block.py` has a test that opens
      a TCP connection to a non-loopback address and asserts it raises; the
      test passes
- [ ] `uv run pytest -m "unit or integration"` passes in an environment with
      `OPENAI_API_KEY`, `VOYAGE_API_KEY`, and `CODEX_HOME` all unset
- [ ] `AGENTS.md` has a "Test Tiers" section with the four-tier description
      from R5
- [ ] Existing callers of `_build_overlay_home()` still work — the only
      in-repo caller is `_run_exec` at `codex_cli_provider.py:270+`, which
      resolves the effective model via `_resolve_model_name` before passing
      through the `-c` override path, so the overlay omitting a default
      model doesn't break invocation

## Anti-Patterns

- NO deleting the subprocess tests before the unit tests are green. Order:
  write unit tests → implement source-aware overlay → delete subprocess
  tests. Never inverse.
- NO hardcoding model names in the new unit tests. Read from the provider's
  resolvers. The test breaks when the provider intentionally changes; the
  test does NOT break when the default silently shifts.
- NO using `pytest-socket` for R4. Custom conftest fixture only. Scope
  creep kept out.
- NO blanket global `socket.create_connection = lambda *a: None` — must
  ONLY apply to tests carrying the `integration` marker, must allow
  loopback (127.0.0.1, ::1) so local subprocess/IPC is unaffected.
- NO deleting `test_codex_exec_help_available`. It runs `--help`, which
  doesn't touch the API; it's the canonical "CLI on PATH and env plumbing
  works" probe.

## Implementation

1. **TDD RED**: Write `tests/unit/test_codex_overlay_config.py` with 5
   tests per R1. Parse emitted TOML with `tomllib`. Run:
   `uv run pytest tests/unit/test_codex_overlay_config.py -v`
   The "default construction" test will FAIL because current
   `_build_overlay_home()` always emits `model` and `model_reasoning_effort`.

2. **TDD GREEN**: Modify
   `chunkhound/providers/llm/codex_cli_provider.py` at `_build_overlay_home`
   (line 214). Replace the unconditional `cfg_lines` with source-aware
   construction using `describe_model_resolution(self._model)` and
   `describe_reasoning_effort_resolution(self._reasoning_effort)`. Emit a
   `model` line only when the resolution source is not `"default"`; same
   rule for `model_reasoning_effort`. Re-run the unit tests until green.

3. **Refactor check**: Re-run the full provider test suite:
   `uv run pytest tests/unit/test_codex_*.py -v` — verify no regression in
   `test_codex_stdin_first.py` or other existing provider unit tests.

4. Delete `test_codex_exec_simple_prompt` (lines 49-136) and
   `test_codex_exec_status_reports_overlay_model` (lines 139-192) from
   `tests/integration/test_codex_exec_help.py`. Remove now-unused imports
   (`tempfile` appears to already be gone; check `Path` if only the
   deleted tests used it).

5. Verify:
   `uv run pytest -m integration tests/integration/test_codex_exec_help.py -v`
   Only `test_codex_exec_help_available` should collect and pass.

6. Add conftest hook in `tests/conftest.py`. Pattern:

   ```python
   @pytest.fixture(autouse=True)
   def _block_outbound_network_for_integration(request, monkeypatch):
       if "integration" not in request.node.keywords:
           return
       real_connect = socket.socket.connect
       def guarded(self, address):
           host = address[0] if isinstance(address, tuple) else address
           if host not in {"127.0.0.1", "::1", "localhost"}:
               raise RuntimeError(
                   f"Integration tier forbids outbound network; "
                   f"test attempted connect to {host}"
               )
           return real_connect(self, address)
       monkeypatch.setattr(socket.socket, "connect", guarded)
   ```

   Adjust for the existing conftest's structure. If `tests/conftest.py`
   doesn't have an `integration` fixture section, add one.

7. Add `tests/integration/test_tier_network_block.py`:

   ```python
   import socket
   import pytest

   @pytest.mark.integration
   def test_outbound_connect_is_blocked():
       s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
       with pytest.raises(RuntimeError, match="Integration tier forbids"):
           s.connect(("1.1.1.1", 53))
   ```

8. Run full tier gate WITHOUT credentials:
   ```
   env -u OPENAI_API_KEY -u VOYAGE_API_KEY -u CODEX_HOME \
     uv run pytest -m "unit or integration" tests/ -v
   ```
   Expect: all pass.

9. Update `AGENTS.md` with the "Test Tiers" section (R5).

10. Commit in logical chunks: (a) unit tests + overlay source-aware change,
    (b) delete subprocess tests, (c) conftest hook + regression test,
    (d) AGENTS.md update.

## Key Considerations

- `_build_overlay_home()` creates its own tempdir via
  `tempfile.mkdtemp(prefix="chunkhound-codex-overlay-")`. Unit tests must
  clean it up — easiest via a fixture that captures the return value and
  `shutil.rmtree(path, ignore_errors=True)` in finalizer.
- `tomllib` is stdlib from Python 3.11+; chunkhound targets 3.11+ already
  (verify via `python_requires` in pyproject.toml before writing tests).
- The conftest hook must scope to the `integration` marker ONLY. Tests
  with both `unit` and `integration` markers get the block; `unit`-only
  tests do not.
- Loopback allowlist must include IPv4 (127.0.0.1), IPv6 (::1), and the
  hostname `localhost` — tests may use any of the three.
- `_run_exec` (production caller of `_build_overlay_home`) passes `-c
  model=...` on the command line to override, and the resolved model
  flows through `effective_model` separately. An overlay that omits the
  default model key is still correct because `-c` overrides always win;
  and when no override is needed, codex picks its own current default.
  This is actually MORE correct than the current behavior.
- `describe_model_resolution` treats `"codex"` as an alias for "use our
  default" (line 84). The source returned in that case is `"default"` —
  meaning the overlay will now skip `model` when the user passes
  `model="codex"`. This is the desired behavior and matches the "don't
  pin a model unless asked" rule.
- `describe_reasoning_effort_resolution` returns source `"fallback"` for
  unknown values (line 107). R2's rule: emit when source is anything
  except `"default"`. `"fallback"` emits; user asked for something (even
  if invalid), we honor the closest approximation.

## Related

- ch-eun — Reclassify mismarked integration tests with fixture conversion
  (same family of issue; R4 conftest assertion directly helps that effort)
- ch-8ud — Redesign live-API embedding tests with frozen corpus
  (R4 conftest assertion will make ch-8ud's live-API tests fail loudly,
  forcing the proper redesign)

## Log

- [2026-04-18T06:41:10Z] [Seth] Immediate mitigation: test re-marked
  @pytest.mark.e2e in tests/integration/test_codex_exec_help.py.
  Module-level pytestmark removed so each test has its own marker.
  simple_prompt → e2e; help_available + status_reports remain integration
  (status may also hit API — see Open Questions, resolved below).
- [2026-04-18T06:58:13Z] [Claude] Redesigned via chat: decouple overlay
  builder (unit) from CLI contract (delete subprocess tests). Remove
  hardcoded default model from overlay so Codex uses its own current
  default when we don't explicitly request one. Enforce tier boundary
  via conftest socket block (not pytest-socket). Decisions: (a) unit
  tests only on `_build_overlay_home()`, (b) delete both subprocess
  tests, (c) conftest assertion for R4, (d) unit tests read defaults
  from provider resolvers, (e) expand scope to include overlay code
  change, (f) symmetric treatment of model + reasoning effort.
- [2026-04-18T06:58:07Z] [Seth] Redesigned via chat before SRE: decouple overlay builder (unit tests) from CLI contract (delete subprocess tests); remove hardcoded default model from overlay; conftest socket-block for tier boundary. Unit tests read defaults from provider resolvers. Symmetric model+effort treatment.
