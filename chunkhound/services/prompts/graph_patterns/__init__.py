"""Graph-aware prompt templates for code_research BFS.

Each template provides a PATTERN (regex matching structural queries) and
PROMPT_AUGMENTATION (text appended to follow-up generation prompt).

ALL_PATTERNS is a list of (compiled_regex, augmentation_text) tuples
for use by QuestionGenerator.
"""

import re

from .call_graph_scope import PATTERN as _CALL_GRAPH_PATTERN
from .call_graph_scope import PROMPT_AUGMENTATION as _CALL_GRAPH_AUG
from .import_dependency import PATTERN as _IMPORT_DEP_PATTERN
from .import_dependency import PROMPT_AUGMENTATION as _IMPORT_DEP_AUG
from .interface_implementors import PATTERN as _INTERFACE_PATTERN
from .interface_implementors import PROMPT_AUGMENTATION as _INTERFACE_AUG
from .test_coverage_map import PATTERN as _TEST_COV_PATTERN
from .test_coverage_map import PROMPT_AUGMENTATION as _TEST_COV_AUG
from .type_chain_tracing import PATTERN as _TYPE_CHAIN_PATTERN
from .type_chain_tracing import PROMPT_AUGMENTATION as _TYPE_CHAIN_AUG

ALL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_TYPE_CHAIN_PATTERN, re.IGNORECASE), _TYPE_CHAIN_AUG),
    (re.compile(_CALL_GRAPH_PATTERN, re.IGNORECASE), _CALL_GRAPH_AUG),
    (re.compile(_TEST_COV_PATTERN, re.IGNORECASE), _TEST_COV_AUG),
    (re.compile(_IMPORT_DEP_PATTERN, re.IGNORECASE), _IMPORT_DEP_AUG),
    (re.compile(_INTERFACE_PATTERN, re.IGNORECASE), _INTERFACE_AUG),
]
