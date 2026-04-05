"""Prompt augmentation for import and module dependency questions.

Matches queries about what a module imports and what depends on it.
"""

# Matches: "what does X import", "what imports X", "dependencies of",
# "depends on", "import graph", "module dependencies"
PATTERN = r"(?:what\s+(?:does\s+.+?\s+)?imports?|what\s+imports|dependencies\s+of|depends\s+on|import\s+graph|module\s+dependenc)"

PROMPT_AUGMENTATION = """\
The symbol dependency graph can trace import dependencies:
- What modules a file imports (direct dependencies)
- What other modules import a given module (reverse dependencies)
- Import chains and circular dependency detection
Prioritize follow-up questions about module boundaries, dependency direction, and coupling."""
