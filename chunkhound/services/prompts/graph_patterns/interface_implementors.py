"""Prompt augmentation for interface implementation questions.

Matches queries about what implements an interface and polymorphic usage.
"""

# Matches: "what implements", "implementations of", "implementors",
# "polymorphic", "concrete classes", "subclasses of", "inherits from"
PATTERN = (
    r"(?:what\s+implements|implementations?\s+of|implementors?"
    r"|polymorphic|concrete\s+class|subclass(?:es)?\s+of|inherits?\s+from)"
)

PROMPT_AUGMENTATION = """\
The symbol dependency graph can trace implementation relationships:
- What classes/structs implement a given interface or protocol
- Where an interface is used polymorphically (called via base type)
- Inheritance and implementation hierarchies
Prioritize follow-up questions about interface contracts, concrete implementations, and polymorphic call sites."""
