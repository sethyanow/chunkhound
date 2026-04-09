"""Prompt augmentation for call graph scoping questions.

Matches queries about callers, callees, and call chain relationships.
"""

# Matches: "what calls X", "what does X call", "callers of", "callees of",
# "call graph", "call chain", "who calls", "called by"
PATTERN = (
    r"(?:what\s+calls|what\s+does\s+\S+\s+call|callers?\s+of"
    r"|callees?\s+of|call\s+(?:graph|chain)|who\s+calls|called\s+by)"
)

PROMPT_AUGMENTATION = """\
The symbol dependency graph can trace call relationships:
- Direct callers and callees of any symbol
- Transitive call chains (multi-hop)
- Call graph scope within a module or across modules
Prioritize follow-up questions about call relationships, invocation paths, and function dependencies."""
