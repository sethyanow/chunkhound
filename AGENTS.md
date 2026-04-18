# ChunkHound LLM Context

## PROJECT_IDENTITY
ChunkHound: Semantic and regex search tool for codebases with MCP integration
Built: 100% by AI agents - NO human-written code
Purpose: Transform codebases into searchable knowledge bases for AI assistants

## MODIFICATION_RULES
**NEVER:**
- NEVER Use print() in MCP server (stdio.py, http_server.py, tools.py)
- NEVER Make single-row DB inserts in loops
- NEVER Use forward references (quotes) in type annotations unless needed

**ALWAYS:**
- ALWAYS Run test suite before committing: `uv run pytest -m "unit or integration" tests/ -v`
- ALWAYS Batch embeddings (min: 100, max: provider_limit)
- ALWAYS Use uv for all Python operations
- ALWAYS Update version via: `uv run scripts/update_version.py`

## KEY_COMMANDS
```bash
# Development
lint:      uv run ruff check chunkhound
typecheck: uv run mypy chunkhound
test:      uv run pytest                                # unit tests only (default)
test-intg: uv run pytest -m integration                 # integration tests
test-all:  uv run pytest -m "unit or integration"           # pre-commit suite
test-e2e:  uv run pytest -m e2e                             # e2e — gate on push/CI, not local dev
test-acc:  uv run pytest -m acceptance                    # acceptance tests (need VCR cassettes)
format:    uv run ruff format chunkhound

# Running
index:     uv run chunkhound index [directory]
mcp_stdio: uv run chunkhound mcp
mcp_http:  uv run chunkhound mcp http --port 5173
```

## VERSION_MANAGEMENT
Dynamic versioning via hatch-vcs - version derived from git tags.

```bash
# Create release
uv run scripts/update_version.py 4.1.0

# Create pre-release
uv run scripts/update_version.py 4.1.0b1
uv run scripts/update_version.py 4.1.0rc1

# Bump version
uv run scripts/update_version.py --bump minor      # v4.0.1 → v4.1.0
uv run scripts/update_version.py --bump minor b1   # v4.0.1 → v4.1.0b1
```

NEVER manually edit version strings - ALWAYS create git tags instead.

## PUBLISHING_PROCESS
```bash
# 1. Create version tag
uv run scripts/update_version.py X.Y.Z

# 2. Run full test suite (MANDATORY)
uv run pytest -m "unit or integration or e2e" tests/ -v

# 3. Prepare release
./scripts/prepare_release.sh

# 4. Test local install
pip install dist/chunkhound-X.Y.Z-py3-none-any.whl

# 5. Push tag
git push origin vX.Y.Z

# 6. Publish
uv publish
```

## DB_PATH_GOTCHAS
- **Preferred: pass project directory as positional arg** — `chunkhound search "query" /path/to/project` — this reads `.chunkhound.json` and resolves the DB correctly
- **For MCP:** `chunkhound mcp --db /path/to/project/.chunkhound` (the path from `.chunkhound.json`'s `database.path`)
- **`--db` with wrong subpath silently returns 0 results** — no error, just empty. Always verify with a regex search first.
- Default DB path: `.chunkhound/db/chunks.db` (directory structure, not flat file)
- When using `--db` flag, pass the **directory** path (e.g. `--db .chunkhound/db`), not the full file path — passing `--db .../chunks.db` creates a nested `chunks.db/chunks.db` directory
- Old-style flat `.chunkhound` files (pre-v4) block directory creation — move aside before re-indexing
- Project-local `.chunkhound.json` with relative `"path": ".chunkhound"` resolves to CWD, not the project dir — use `--db` with absolute paths when indexing remote projects
- `--config` does NOT override a project-local `.chunkhound.json` for DB path — always use explicit `--db` when the target project has its own config

## TEST_TIERS
Tests are classified by marker. The classification is a contract, not a suggestion — the default `-m "unit or integration"` gate runs on every commit, and live-API calls at those tiers burn user credentials and break CI reproducibility.

- **`unit`** — No subprocess. No I/O beyond `tmp_path`. No network. Runs in milliseconds. This is the default tier selector.
- **`integration`** — Local subprocess and loopback network OK. NO outbound network. Autouse fixture `_block_outbound_network_for_integration` in `tests/conftest.py` monkeypatches `socket.socket.connect` to raise for non-loopback destinations; regression covered by `tests/integration/test_tier_network_block.py`.
- **`acceptance`** — VCR-cassette-backed. Cassettes recorded once against real APIs; replay in CI uses placeholder credentials. Configure via `vcr_config` fixture at `tests/conftest.py`.
- **`e2e`** — Live APIs, opt-in only. Not run by the default `-m "unit or integration"` gate or CI. Authors must verify their credentials before invoking.

**Subprocess limitation of the `integration` network block**: the autouse fixture catches Python-level `socket.socket.connect` only. Child processes spawned via `subprocess.run`, `asyncio.create_subprocess_exec`, etc. have their own socket namespace and are NOT blocked. Any integration test that shells out to an external CLI (`codex`, `curl`, `git`, embedding providers) that itself may hit the network MUST be manually reclassified into `acceptance` (cassette) or `e2e` (opt-in). The conftest hook is defense-in-depth for pure-Python callers, not a substitute for thinking about what your subprocess does.

## PROJECT_MAINTENANCE
- Full test suite is the mandatory pre-commit guardrail
- Run `uv run mypy chunkhound` during reviews to catch Optional/type boundary issues
- All code patterns should be self-documenting
- LSP Diagnostics are issues to be resolved when seen not triaged away
