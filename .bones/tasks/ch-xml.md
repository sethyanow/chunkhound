---
id: ch-xml
title: Graph-aware prompt templates for code_research BFS
status: closed
type: task
priority: 1
owner: Seth
parent: ch-z2o
---




**Blocked by:** ch-8mu (GraphWalkExpander wired into UnifiedSearch — graph data now flows through code_research pipeline)
**Unlocks:** Phase 5 acceptance (sub-epic criteria 7, 8); parent epic criterion "Prompt templates used by code_research BFS for structural sub-questions"

## Context

Phase 5 (ch-z2o) R8 requires graph-aware prompt templates in `chunkhound/services/prompts/graph_patterns/` that code_research's BFS uses for structural sub-questions. With ch-8mu complete, GraphWalkExpander provides graph-discovered chunks to `unified_search()`. This task adds templates that guide the QuestionGenerator to produce structural follow-up questions when the topic is structural.

The BFS exploration flow:
1. `BFSExplorationStrategy._process_node()` calls `QuestionGenerator.generate_follow_up_questions()` (`shared/exploration/bfs_exploration_strategy.py:384`)
2. QuestionGenerator uses `prompts.FOLLOWUP_GENERATION_USER` template (`v1/question_generator.py:210`)
3. LLM generates follow-up questions as strings
4. Questions become child BFS nodes, each triggering its own `unified_search()`

Currently the follow-up prompt is generic ("focus on component interactions, data/control flow, dependencies"). Graph-aware templates add structural categories that guide the LLM to produce questions answerable by graph data rather than semantic search alone.

Key infrastructure:
- Prompts live in `chunkhound/services/prompts/` — one file per prompt, re-exported via `__init__.py`
- `FOLLOWUP_GENERATION_USER` at `followup_generation.py:10` — template string with `{format_variables}`
- `QuestionGenerator.generate_follow_up_questions()` at `v1/question_generator.py:69` — builds prompt, calls LLM
- `BFSExplorationStrategy._process_node()` at `shared/exploration/bfs_exploration_strategy.py:316` — calls question generator

Sub-epic says: "Pattern matching on question text, not hardcoded routing." Augmentation happens pre-LLM on `context.root_query` — the root query determines whether exploration should favor structural questions. The LLM then receives structural guidance and generates questions answerable by graph data.

## Design

Two changes:

1. **Create graph-aware prompt templates** — 5 template files in `chunkhound/services/prompts/graph_patterns/`:
   - `type_chain_tracing.py` — "What type does X return? What consumes that type?"
   - `call_graph_scope.py` — "What calls X? What does X call?"
   - `test_coverage_map.py` — "Which tests exercise X? What paths are untested?"
   - `import_dependency.py` — "What does this module import? What imports it?"
   - `interface_implementors.py` — "What implements interface X? Where is X used polymorphically?"

   Each template exports a `PATTERN: str` (regex for matching generated questions) and a `PROMPT_AUGMENTATION: str` (appended to follow-up generation prompt when the root query matches structural patterns).

2. **Augment follow-up generation with structural guidance** — Modify `generate_follow_up_questions()` to detect structural query patterns and append relevant template augmentations to the prompt. The detection runs on the ROOT query (not individual follow-ups), since the root query determines whether the exploration should favor structural questions.

   No routing or filtering — just prompt enrichment. When the root query matches structural patterns, the LLM gets additional guidance on what kinds of structural follow-ups to generate.

## Requirements

From Phase 5 sub-epic ch-z2o:
- Prompt templates exist in `chunkhound/services/prompts/graph_patterns/`
- code_research BFS uses templates when encountering structural sub-questions
- NO prompt templates that duplicate semantic search — templates for questions where graph data is faster/more accurate

## Implementation

### Step 1: Write failing test — graph_patterns directory has template modules
File: `tests/unit/research/test_graph_pattern_templates.py` (new).
Test: `test_graph_pattern_templates_exist` — import each template module, verify it exports `PATTERN` (str) and `PROMPT_AUGMENTATION` (str). Verify PATTERN compiles as regex (`re.compile(PATTERN)`), both strings are non-empty.

### Step 2: Create graph_patterns directory and 5 template files
Dir: `chunkhound/services/prompts/graph_patterns/`
Files: `__init__.py`, `type_chain_tracing.py`, `call_graph_scope.py`, `test_coverage_map.py`, `import_dependency.py`, `interface_implementors.py`.
Each file exports `PATTERN` (regex string matching structural question text) and `PROMPT_AUGMENTATION` (text appended to follow-up prompt). The `__init__.py` exports `ALL_PATTERNS: list[tuple[re.Pattern, str]]` — compiled patterns paired with their augmentation text.

### Step 3: Write failing test — structural query detection
Same file. Test: `test_detect_structural_query` — given queries like "What calls function X?", "What type does Y return?", verify pattern matching returns the correct augmentation(s). Non-structural queries like "How does authentication work?" return no augmentation.

### Step 4: Write failing test — augmentation appears in follow-up prompt
File: `tests/unit/research/test_question_generator.py` (existing — add to it).
Test: `test_structural_query_augments_followup_prompt` — mock LLM, call `generate_follow_up_questions()` with a structural root query (e.g., "What calls parse_config?"). Assert the prompt sent to LLM contains structural augmentation text.

### Step 5: Write failing test — non-structural query has no augmentation
Same file. Test: `test_nonstructural_query_no_augmentation` — mock LLM, call with non-structural root query. Assert prompt does NOT contain augmentation text.

### Step 6: Implement structural detection in QuestionGenerator
File: `chunkhound/services/research/v1/question_generator.py`
- Import `ALL_PATTERNS` from `chunkhound.services.prompts.graph_patterns`
- In `generate_follow_up_questions()`, after building the base prompt (line ~220), detect structural patterns in `context.root_query`
- If matches found, append the augmentation text(s) to the prompt before LLM call
- No changes to BFS flow or question filtering — augmentation is prompt-only

### Step 7: Run targeted tests
`uv run pytest tests/unit/research/test_graph_pattern_templates.py tests/unit/research/test_question_generator.py -v`

### Step 8: Run full test suite
`uv run pytest -m "unit or integration or e2e" tests/ -v > /tmp/test_suite_ch_xml.out 2>&1`

## Success Criteria
- [x] 5 template files exist in `chunkhound/services/prompts/graph_patterns/`
- [x] Each template exports `PATTERN` and `PROMPT_AUGMENTATION`
- [x] `ALL_PATTERNS` is importable from `graph_patterns.__init__`
- [x] Structural root queries produce augmented follow-up prompts
- [x] Non-structural root queries produce unaugmented follow-up prompts
- [x] No changes to BFS exploration flow or question filtering logic
- [x] Full test suite passes

## Anti-Patterns
- NO hardcoded routing (if query == "type chain" → use template) — pattern matching via regex
- NO replacing the existing follow-up generation prompt — augmentation is additive
- NO changes to BFS exploration strategy — templates affect QuestionGenerator only
- NO LLM calls during pattern detection — regex matching is deterministic
- NO templates that duplicate what semantic search already covers (generic "how does X work?" is semantic territory)

## Key Considerations
- Templates are prompt augmentations, not replacements. The base `FOLLOWUP_GENERATION_USER` template stays unchanged. Augmentations append structural guidance sections.
- Pattern matching runs on `context.root_query`, not on individual follow-up questions. The root query determines the structural nature of the exploration.
- Multiple patterns may match a single query (e.g., "What calls parse_config and what type does it return?" matches both call_graph_scope and type_chain_tracing). All matching augmentations are appended.
- Template text should reference ChunkHound's graph capabilities by name (e.g., "the symbol dependency graph can trace callers/callees") so the LLM knows what structural data is available.
- The existing `question_filtering` step in QuestionGenerator (line 248) filters questions by relevance to root query — structural augmentation shouldn't conflict with this filter.
- Augmentation text competes with code context for the `max_input_tokens` budget. If all 5 patterns match, total augmentation must stay small enough not to crowd out code context. Keep each augmentation concise (a few lines, not paragraphs).

## Log

- [2026-04-05T12:46:12Z] [Seth] Debrief: All 7 success criteria met. 44 new tests (32 pattern matching, 5 augmentation integration, 8 adversarial). Full suite 2936 passed. SRE caught 6 wrong paths in skeleton (services/research/prompts → services/prompts). Reflections: speculated about path mismatch cause without git history — corrected by user. Import_dependency regex needed .+? instead of \S+ for multi-word subjects (caught by test). Phase 5 ready for acceptance.
