"""Prompt augmentation for type chain tracing questions.

Matches queries about return types, type consumption, and type flow between symbols.
"""

# Matches: "what type does X return", "what consumes type Y", "type of X",
# "return type", "type chain", "type flow"
PATTERN = r"(?:what\s+type|return\s+type|type\s+(?:of|chain|flow)|consumes?\s+(?:that\s+)?type)"

PROMPT_AUGMENTATION = """\
The symbol dependency graph can trace type chains:
- What type a function returns (via hover/type_signature)
- What other symbols consume that return type
- Type flow across call boundaries
Prioritize follow-up questions about type relationships and data flow between producers and consumers."""
