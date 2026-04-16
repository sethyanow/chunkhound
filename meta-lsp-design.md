# Meta-LSP MCP — Design Notes

Working design for a thin meta-LSP MCP server that gives any MCP-speaking agent cross-language code intel over a user-configured set of LSP servers. Captures decisions from exploratory design chat; intended as a starting point to continue locally.

## Problem

- Polyglot codebases need cross-language intel. Baked-in LSP tools in Claude (and similar agents) don't cut it.
- Agents fall back to `grep` when precise semantic lookups are unavailable or slow. Grep can't distinguish definitions from mentions, can't follow types across files, can't jump across language boundaries.
- Existing language servers already solve most of this — but per-language, and with rough edges that need fixing in forks.
- Cloud / sandboxed environments (Claude Code on the web, ephemeral containers) make the problem worse: no assumed toolchains, no global installs, no manual setup.

## Shape

Three pieces, each with its own concern:

### 1. Meta-LSP MCP (new thin project, its own repo)

A router. No language semantics. Takes a user-supplied list of LSP servers and fans requests across them.

**Responsibilities:**
- Spawn and hold persistent LSP server processes.
- Route requests by file path / language ID.
- Fan out workspace-scoped requests (`workspace/symbol`, etc.) across all configured servers, merge & dedupe results.
- Expose a single MCP tool surface so any MCP client (Claude Code, Cursor, custom agents) gets polyglot intel through one funnel.

**Non-goals:**
- Reimplementing any LSP server logic.
- Owning or replacing per-language MCPs (e.g. `pyright-mcp`) that already exist.
- Shipping language servers itself — plugins do that.

### 2. Plugins (the distribution unit)

Each plugin packages one LSP-ecosystem contribution. Shape:

- **LSP launch config** — how meta-LSP should start this server (cmd, init options, file globs, workspace markers, capability flags).
- **Skills** — agent-facing workflow docs (SKILL.md style) for language-specific recipes.
- **Scripts** — executables that back the skills (analysis helpers, build hooks, batch operations).
- **The LSP server binary or build hook** — bundled prebuilt where possible, built-on-first-run where not.

Installing a plugin registers its LSP with the meta-LSP router and surfaces its skills + scripts to the agent.

### 3. Marketplace (Claude plugin marketplace)

Distribution surface that combines the LSP-improvement work across forks. Each fork ships as a plugin. Agents and users install the marketplace once; all configured languages light up reproducibly — especially important in cloud envs.

Coexistence: standalone per-language MCPs (e.g. `pyright-mcp`) can also live in the same marketplace as independent plugins. Users who want just Python intel install only that; users who want polyglot install meta-LSP + language plugins.

## Tool surface

Single `lsp()` funnel at the MCP layer, with a small set of canonicalized verbs on top. Consistent with the pattern already proven in `pyright-mcp`.

**Verbs (the 80% case — what kills the grep reflex):**

- `symbol_search(name, kind?, langs?)` — fans `workspace/symbol` across all configured servers; merges, dedupes by `(uri, range)`, normalizes `SymbolKind`. **Keystone verb.**
- `defs(file, pos)` — go-to-definition, routed to the server owning `file`.
- `refs(file, pos)` — find references.
- `impls(file, pos)` — implementations / concrete subclasses.
- `hover(file, pos)` — type info / signature.
- `outline(file)` — document symbols.
- `diagnostics(file?)` — errors / warnings.

**Escape hatch:**

- `lsp(lang, method, params)` — raw passthrough for anything not covered by a verb.

## Why `workspace/symbol` is the keystone

1. **Entry point for everything else.** Refs, defs, rename, hover all need a starting `(uri, range)`. Without cross-language workspace-symbol, agents resort to grep for the lookup step.
2. **Every mature LSP server implements it.** Enables a uniform fan-out pattern — no per-language branching in the aggregator.
3. **Polyglot boundaries are where names actually collide.** Python calling Rust via pyo3, Zig build scripts, TS API contracts — a single workspace-symbol query shows the full graph.
4. **Hardest to make fast**, because it's the only workspace-scoped LSP method. Must be treated with the cold-cache discipline below.

## Cold-cache discipline (the Phase 5 lesson)

From `pyright-mcp`'s 2026-04-12 handoff — directly applicable to meta-LSP.

**Observation:** Pyright's `ImplementationProvider` and `TypeHierarchyProvider` call `getParseResults()` on every user-code file, triggering binding for all of them. With no project config, 1,284 files bind on cold cache and blow past the 30s MCP timeout. `ReferencesProvider` avoids this by string-prefiltering raw file contents (no parse, no bind) and only binding candidates.

**Rule for meta-LSP:** Any workspace-scoped operation in the aggregator must assume servers may not have binding warmup. Prefer servers that implement string-prefilter-before-bind internally. When a server doesn't, meta-LSP can offer an outer layer that pre-filters the candidate file set before dispatch (via raw file reads).

Encoded as a plugin manifest flag:

```json
"capabilities": {
  "workspaceSymbol": { "stringPrefilter": true, "timeoutMs": 10000 }
}
```

`stringPrefilter: true` means the server handles it internally. `false` means meta-LSP should do it upstream.

The string-BFS approach proposed in the handoff (scan file text for target name, add hits to queue, expand transitively) is the reference pattern.

## Plugin manifest (draft)

```json
{
  "name": "pyright",
  "version": "0.1.0",
  "langIds": ["python"],
  "fileGlobs": ["**/*.py", "**/*.pyi"],
  "workspaceMarkers": ["pyrightconfig.json", "pyproject.toml", "setup.py"],
  "server": {
    "cmd": ["node", "${pluginDir}/dist/pyright-langserver.js", "--stdio"],
    "buildHook": "${pluginDir}/scripts/build.sh",
    "initOptions": { }
  },
  "capabilities": {
    "workspaceSymbol": { "stringPrefilter": true, "timeoutMs": 10000 },
    "implementations": { "stringPrefilter": true },
    "callHierarchy": false
  },
  "skills": ["./skills/pyright"],
  "scripts": ["./scripts"]
}
```

New vs. the existing `.claude-plugin/marketplace.json`:
- `workspaceMarkers` — meta-LSP detects project roots in monorepos.
- `capabilities` — declares support *and* cold-cache behavior.
- `buildHook` — build-on-first-run for cloud reproducibility.

## Cloud reproducibility

For sandboxed / cloud environments, two paths per plugin (pick one):

1. **Prebuilt binaries**, bundled in the plugin. Fast cold start, larger download. Works for pyright (portable node bundle) and markymark (single Rust binary).
2. **Build-on-first-run + cache**, driven by a `SessionStart` hook that iterates installed plugins and runs missing build hooks, caching to `~/.cache/<plugin>/`. Required for servers with toolchain-specific needs (zls needs Zig master). Fails loud when toolchain absent.

Either way, first session in a fresh sandbox should get working polyglot intel with no manual setup.

## Learnings carried from pyright-mcp (patterns, not code)

Meta-LSP reimplements these fresh — pyright-mcp keeps its own life cycle and phase plan untouched.

- **Persistent JSON-RPC bridge** to LSP servers (see `lsp-client.ts`). Agents get warm-cache performance after first request.
- **Resolve-server-path pattern** → generalized in meta-LSP as plugin-manifest lookup.
- **Fat `lsp()` + canonical verbs on top** — already the shape in `pyright-mcp/src/mcp-server.ts` and its SKILL.md.
- **String-prefilter-before-bind** for workspace-scoped ops — encoded as a manifest capability.
- **Dual MCP / CLI access** — in pyright-mcp this means MCP when warm, `lsp-client` CLI as fallback. In meta-LSP: MCP is primary; a thin CLI wrapping the same router is the fallback for non-MCP contexts.

## Pyright-mcp current state (for when returning to that track)

Independent project. Current phase plan (from `.bones/tasks` + 2026-04-12 handoff):

- **Phase 5: Code Lens** — implementation exists, tests pass, but `codeLens/resolve` with `kind: implementations` times out on cold cache (root cause: `getParseResults()` binding on all user files). Import-graph pre-filter was tried and reverted — the graph itself requires binding. Proposed fix: string-BFS from file contents, only bind candidates. Not yet prototyped.
- **Phase 6: Range Providers** — blocked by Phase 5.
- **Phase 7: Refactoring** — blocked by Phase 6.
- **Phase 8: Facade** — blocked by Phase 7.
- **pyr-a56: Decompose `typeEvaluator.ts`** — ready, independent.

Test asset worth recreating: `codeLens.transitiveSubclass.fourslash.ts` (three-file transitive inheritance test). Was reverted alongside the import-graph attempt.

## Dogfooding loop

1. Install the marketplace in this sandbox (and in local Claude Code config).
2. Configure meta-LSP with the pyright and zls plugins.
3. Every time an agent reaches for `grep` instead of `symbol_search` / a verb, note it. That's either a missing verb or a latency problem.
4. Fix latency at the fork level (pyright-mcp, zls, etc.). Fix missing verbs at the meta-LSP level.
5. Repeat until `grep` is genuinely the right tool (true text search), not a fallback for failed semantic lookup.

## Open questions / forks in the road

- **Config discovery**: single global config, per-workspace `.meta-lsp.json`, or both with workspace overriding. (Lean: both, workspace wins.)
- **Process supervision details**: spawn per `(workspace_root, language)`, idle-shutdown timer, crash-restart policy. (Steal from coc.nvim or helix-term rather than reinventing.)
- **Skills split**: `skills.claudeCode` (installed into `.claude/skills/`, visible as slash commands) vs `skills.mcp` (surfaced only through meta-LSP tools). Claude Code users get the rich experience; other MCP clients get the MCP-only subset.
- **Capability negotiation shape**: how verbs degrade when some servers lack a method — partial results + explicit `missing: [lang]` field, or hard fail. (Lean: partial + missing field.)
- **Workspace root detection in monorepos**: one root per project marker (`pyrightconfig.json`, `build.zig`, `Cargo.toml`), not one per repo.
- **First plugin to ship with meta-LSP v1**: markymark has the lightest toolchain burden; pyright has the most existing work to draw from; zls is the hardest (requires Zig master). Order matters for proving the shape end-to-end.

## Repo references

- `sethyanow/pyright` (dev branch) — pyright fork with `pyright-mcp` package, `.claude-plugin/marketplace.json`, `.bones/tasks`, skills under `packages/pyright-mcp/skills/pyright`.
- `sethyanow/zls` (dev branch) — zls fork.
- `sethyanow/markymark` — MCP-native Markdown LSP; already in this design space, candidate for v1 reference plugin.
- `sethyanow/chunkhound` (this repo) — complementary: semantic + regex chunk search. Meta-LSP and ChunkHound are companions, not substitutes.
