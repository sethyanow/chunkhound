---
id: ch-bf0
title: 'Phase 6: Skill Script Library'
status: open
type: epic
priority: 1
depends_on: [ch-z2o]
parent: ch-8e7
---




## Context
Parent epic ch-8e7, Phase 6. Depends on Phase 5 (ch-z2o).
Phase 5 delivers enhanced search + code_research with graph awareness. This phase builds the skill script library — executable analysis workflows that compose ChunkHound's MCP tools, following the mcp-code-execution-enhanced progressive disclosure pattern.

## Requirements
Scoped to parent epic R9:
- R9: Skill script library (5 skills: coverage-diff, safe-to-delete, migration-plan, translate-tests, architecture-query)

## Success Criteria
- [ ] Each skill has: `skills/<name>/SKILL.md` + `skills/<name>/workflow.py` (symlink to `scripts/<name>.py`)
- [ ] Each SKILL.md has proper frontmatter (name, description with trigger phrases, parameters)
- [ ] Each workflow.py is executable with `--help` showing parameter docs
- [ ] Scripts call ChunkHound MCP tools via HTTP (`call_mcp_tool()` or httpx against ChunkHound HTTP server)
- [ ] `coverage-diff` gathers symbols + type signatures from two locations, outputs structured comparison for agent synthesis
- [ ] `safe-to-delete` gathers references + reachability + test coverage, outputs structured safety assessment data
- [ ] `migration-plan` gathers dependency graph + semantic similarity, outputs ordered dependency list with similarity scores
- [ ] `translate-tests` gathers API surfaces of both types + test scenarios, outputs structured comparison for agent skeleton generation
- [ ] `architecture-query` gathers graph overview + structural search results, outputs structured summary for agent synthesis
- [ ] Script internals never need to enter agent context — progressive disclosure: discover → read SKILL.md → execute with CLI args → get output
- [ ] All scripts handle ChunkHound server unavailable gracefully (clear error, not traceback)
- [ ] `uv run pytest tests/test_skill_scripts.py -v` → all pass
- [ ] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Anti-Patterns
Inherited from parent epic, plus:
- NO LLM calls inside skill scripts — scripts gather data, agent synthesizes
- NO hardcoded ChunkHound server URLs — configurable via env var or CLI arg
- NO Claude Code plugin skills (markdown-only) — these are executable Python scripts with SKILL.md wrappers
- NO duplicating MCP tool logic in scripts — scripts compose tools, not reimplement them

## Key Considerations
- Scripts need a way to call ChunkHound's MCP tools. ChunkHound has HTTP server mode (`chunkhound mcp http --port 5173`). Scripts use httpx or similar to POST tool calls. The `call_mcp_tool()` pattern from mcp-code-execution-enhanced may need adaptation.
- SKILL.md trigger phrases must be specific enough for Claude Code auto-discovery. E.g., "coverage gap", "what tests are missing", "safe to delete", "can I remove", "migration order", "port from X to Y", "translate tests", "architecture overview".
- Where these live is an open question (ChunkHound repo vs companion plugin). For now, ship in the ChunkHound repo under `skills/`. Can be extracted to a plugin later.
- Each script should output JSON by default (machine-readable for agent), with optional `--human` flag for formatted text output.

## Acceptance Requirements
**Agent Documentation:**
- [ ] CLAUDE.md updated: skill library location, how to add new skills
- [ ] AGENTS.md updated: skill descriptions and trigger phrases
- [ ] README or dedicated doc on writing custom skills

**User Walkthrough Must Cover:**
- Discover a skill via Claude Code auto-discovery (SKILL.md triggers)
- Execute coverage-diff against a test fixture → verify structured output
- Execute safe-to-delete on a known-safe symbol → verify it reports safe
- Execute safe-to-delete on a symbol with dependents → verify it reports risks
- Add a custom skill script → verify it's discoverable and executable
