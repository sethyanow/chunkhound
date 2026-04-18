---
id: ch-26k
title: Stop codex overlay tests from burning live API tokens + remove hardcoded default model
status: closed
type: bug
priority: 1
owner: Seth
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

- [x] `uv run pytest tests/unit/test_codex_overlay_config.py -v` passes
      with 15 tests (9 R1 variants + 6 adversarial); commit a2217766
- [x] `_build_overlay_home()` omits `model` and `model_reasoning_effort`
      keys when resolution source is `"default"`; commit a2217766
- [x] `test_codex_exec_simple_prompt` and
      `test_codex_exec_status_reports_overlay_model` removed; commit
      03ae4489. Only remaining matches are in historical `_*.out` logs.
- [x] `uv run pytest -m integration tests/integration/test_codex_exec_help.py`
      passes with only `test_codex_exec_help_available` (verified)
- [x] `tests/integration/test_tier_network_block.py` has 4 tests
      (IPv4/IPv6 × outbound-blocked/loopback-allowed), all passing;
      commits 283875d3 + 039e5368
- [x] `uv run pytest -m "unit or integration"` passes with no credentials:
      2939 passed, 0 failed (env -u OPENAI_API_KEY -u VOYAGE_API_KEY
      -u CODEX_HOME -u ANTHROPIC_API_KEY)
- [x] `AGENTS.md` has TEST_TIERS section with the four-tier description;
      commit ca7f9a38
- [x] Existing callers of `_build_overlay_home()` still work —
      test_codex_overlay_modes.py (2 asyncio tests through `_run_exec`)
      passes with assertion updated to the new spec
- [x] Unit test covers `CodexCLIProvider(model="codex")` → overlay omits
      `model` key (`test_codex_alias_omits_model_key`)
- [x] AGENTS.md TEST_TIERS explicitly documents the subprocess bypass
      limitation (commit ca7f9a38)

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

### Failure Catalog (from adversarial planning)

**Dev machine bleed-through: Unit tests for `_build_overlay_home()`**
- Assumption: Unit test runs in a clean environment with no side effects.
- Betrayal: `_build_overlay_home()` calls `_get_base_codex_home()` which
  reads `$CODEX_HOME` or `~/.codex`, then `_copy_minimal_codex_state()`
  copies subset files into the overlay. If the developer has a real
  `~/.codex` with sessions/auth state, those get pulled into the overlay
  even in unit tests — tests become non-hermetic.
- Consequence: Test passes on one machine, fails on another; user auth
  tokens or session files end up in pytest's tmp dirs; reviewers can't
  reproduce failures.
- Mitigation: Unit tests monkeypatch `CodexCLIProvider._get_base_codex_home`
  to return `None`, so `_copy_minimal_codex_state` is never invoked.
  Overlay is built from scratch with ONLY our config.toml. Structural
  via fixture, not try/except.

**Model alias resolution: `_build_overlay_home()` omission rule**
- Assumption: The 5 R1 test variants cover all paths where `model` is
  omitted from the TOML.
- Betrayal: `describe_model_resolution("codex")` returns `(<default>,
  "default")` — the string `"codex"` is a documented alias for "use our
  default" (line 84 of codex_cli_provider.py). Source-aware emission
  skips the key when source is `"default"`, so `model="codex"` SHOULD
  also omit. Not currently covered as a distinct test case.
- Consequence: An implementation that only checks source by name
  (e.g., only treating `None`/`""` as default-sourced) would accidentally
  emit `model = "<default>"` when `model="codex"` was passed. Subtle
  regression that the 5 R1 variants miss.
- Mitigation: Add explicit test `model="codex" → no model key in TOML`
  to R1's coverage. Added as success criterion below.

**Subprocess bypass: conftest network block**
- Assumption: The socket monkeypatch prevents all integration-marked
  tests from hitting live APIs.
- Betrayal: `monkeypatch.setattr(socket.socket, "connect", ...)` only
  patches Python-level `socket` in the test process. Subprocess children
  spawned via `subprocess.run(["codex", "exec", ...])` or
  `asyncio.create_subprocess_exec` have their own socket namespace —
  completely outside our reach. A future integration test that spawns
  codex/git/embedding CLIs hits the API and the block never fires.
- Consequence: False sense of coverage. The rule "integration tier
  forbids live APIs" appears enforced but actually only catches
  in-process network. Someone adds a subprocess-based API test, it
  slips through, burns tokens — same failure class as the original bug.
- Mitigation: AGENTS.md explicitly documents the limitation:
  "The conftest hook catches Python-level sockets only. Subprocess
  invocations that spawn external CLIs (codex, curl, etc.) make
  network calls outside the test process and MUST be manually
  classified into the correct tier — the hook cannot enforce this."
  Added as a criterion on R5.

**Reference leaks: Subprocess test deletion**
- Assumption: Deleting `test_codex_exec_simple_prompt` and
  `test_codex_exec_status_reports_overlay_model` only affects the file.
- Betrayal: CI configs, pytest.ini selectors, documentation, or other
  tests could reference these by name. Post-delete, CI may fail on a
  missing test selector OR docs may silently rot.
- Consequence: Broken CI pipeline OR stale documentation references.
- Mitigation: Implementation step 4 adds a grep check before deletion:
  `rg "test_codex_exec_simple_prompt|test_codex_exec_status_reports"`.
  If hits found outside the test file itself, update or remove them in
  the same commit. Structural via pre-delete grep.

**Fixture shadowing: Regression test for tier block**
- Assumption: The conftest fixture in `tests/conftest.py` applies to
  `tests/integration/test_tier_network_block.py`.
- Betrayal: If a `tests/integration/conftest.py` exists with a
  conflicting fixture name or scope, it shadows the root fixture. The
  regression test would run without the block, the real connect would
  either succeed (network available) or fail with OSError — either way,
  not the expected `RuntimeError`.
- Consequence: Test fails for the wrong reason, or passes when it
  shouldn't (if real connect also happens to error).
- Mitigation: Implementation step 5 checks `tests/integration/conftest.py`
  exists and does NOT define a same-name fixture. Keep the hook in
  `tests/conftest.py` ONLY. Structural via directory discipline.

**Pre-existing hygiene gap: `_build_overlay_home()` TOML escaping**
- Noted but OUT OF SCOPE for this task: line 232 writes
  `f'model = "{model_name}"'` directly instead of using the
  `_toml_string()` helper at line 142. If `model_name` contains `"` or
  `\n`, the TOML becomes malformed. R2 should preserve this
  existing-gap-not-our-problem by using `_toml_string()` when it emits
  the keys. Do NOT expand scope to audit other TOML emissions.

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
- [2026-04-18T07:02:14Z] [Seth] Adversarial planning added failure catalog to Key Considerations: dev-machine bleed-through (monkeypatch _get_base_codex_home), model=codex alias case (new criterion), subprocess bypass of socket block (document limitation in R5), reference leaks on delete (grep check), fixture shadowing (tests/conftest.py only), TOML escaping hygiene (use _toml_string helper in R2). Two new success criteria added.
- [2026-04-18T07:26:39Z] [Seth] Adversarial stress test: 5 new _build_overlay_home tests (case/whitespace alias, unicode, quote, backslash, isolation) + 2 IPv6 conftest tests. One RED→GREEN cycle: _toml_string only escaped quotes, not backslashes — pre-existing bug exposed by my overlay change, fixed in scope. Affects 3 callsites total (lines 246, 249, 346, 352). Out-of-scope findings: (a) overlay temp-dir cleanup is caller responsibility with no leak-detection — minor future risk, (b) uppercase/whitespace alias works only because all in-class callers use the resolver — if someone compared self._model directly they'd miss the normalization. All 15 overlay unit tests + 4 tier-block tests green.
