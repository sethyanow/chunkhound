"""Prompt augmentation for test coverage mapping questions.

Matches queries about which tests exercise a symbol and untested paths.
"""

# Matches: "which tests", "test coverage", "tests exercise", "tested by",
# "untested", "test for", "tests that cover"
PATTERN = r"(?:which\s+tests|test\s+coverage|tests?\s+(?:exercise|that\s+cover)|tested\s+by|untested|test\s+for)"

PROMPT_AUGMENTATION = """\
The symbol dependency graph can map test coverage:
- Which test files/functions call a given symbol (via caller graph)
- Symbols with no test callers (potential coverage gaps)
- Test entry points reachable from changed symbols
Prioritize follow-up questions about test relationships and coverage gaps."""
