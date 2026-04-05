"""Tests for graph-aware prompt templates used by code_research BFS."""

import re

import pytest

pytestmark = pytest.mark.unit

# The 5 template modules that must exist
TEMPLATE_MODULES = [
    "type_chain_tracing",
    "call_graph_scope",
    "test_coverage_map",
    "import_dependency",
    "interface_implementors",
]


class TestGraphPatternTemplatesExist:
    """Verify each template module exports PATTERN and PROMPT_AUGMENTATION."""

    @pytest.mark.parametrize("module_name", TEMPLATE_MODULES)
    def test_template_exports_pattern_and_augmentation(self, module_name: str) -> None:
        """Each template module exports non-empty PATTERN (valid regex) and PROMPT_AUGMENTATION."""
        import importlib

        mod = importlib.import_module(
            f"chunkhound.services.prompts.graph_patterns.{module_name}"
        )

        # Exports exist and are strings
        assert hasattr(mod, "PATTERN"), f"{module_name} missing PATTERN export"
        assert hasattr(
            mod, "PROMPT_AUGMENTATION"
        ), f"{module_name} missing PROMPT_AUGMENTATION export"
        assert isinstance(mod.PATTERN, str)
        assert isinstance(mod.PROMPT_AUGMENTATION, str)

        # Non-empty
        assert mod.PATTERN.strip(), f"{module_name} PATTERN is empty"
        assert (
            mod.PROMPT_AUGMENTATION.strip()
        ), f"{module_name} PROMPT_AUGMENTATION is empty"

        # PATTERN compiles as regex
        try:
            re.compile(mod.PATTERN, re.IGNORECASE)
        except re.error as e:
            pytest.fail(f"{module_name} PATTERN is not valid regex: {e}")

    def test_all_patterns_importable(self) -> None:
        """ALL_PATTERNS list is importable from graph_patterns package."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        assert isinstance(ALL_PATTERNS, list)
        assert len(ALL_PATTERNS) == len(TEMPLATE_MODULES)

        for compiled_pattern, augmentation in ALL_PATTERNS:
            assert isinstance(compiled_pattern, re.Pattern)
            assert isinstance(augmentation, str)
            assert augmentation.strip()


class TestStructuralQueryDetection:
    """Verify patterns match structural queries and reject non-structural ones."""

    @pytest.mark.parametrize(
        "query",
        [
            "What calls parse_config?",
            "What does process_file call?",
            "Who calls the validate function?",
            "callers of handle_request",
            "Show me the call graph for indexing",
        ],
    )
    def test_call_graph_queries_match(self, query: str) -> None:
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert matches, f"Expected call graph match for: {query}"
        assert any("call" in aug.lower() for aug in matches)

    @pytest.mark.parametrize(
        "query",
        [
            "What type does parse_config return?",
            "What consumes that type?",
            "Trace the type chain from Reader to Output",
            "What is the return type of process()?",
        ],
    )
    def test_type_chain_queries_match(self, query: str) -> None:
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert matches, f"Expected type chain match for: {query}"
        assert any("type" in aug.lower() for aug in matches)

    @pytest.mark.parametrize(
        "query",
        [
            "Which tests exercise the parse function?",
            "What is the test coverage for indexing?",
            "Are there untested paths in the search module?",
        ],
    )
    def test_test_coverage_queries_match(self, query: str) -> None:
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert matches, f"Expected test coverage match for: {query}"
        assert any("test" in aug.lower() for aug in matches)

    @pytest.mark.parametrize(
        "query",
        [
            "What does the search module import?",
            "What imports the config module?",
            "Show me the dependencies of the parser",
            "Module dependencies for chunkhound.services",
        ],
    )
    def test_import_dependency_queries_match(self, query: str) -> None:
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert matches, f"Expected import dependency match for: {query}"
        assert any("import" in aug.lower() or "dependenc" in aug.lower() for aug in matches)

    @pytest.mark.parametrize(
        "query",
        [
            "What implements the SearchProvider interface?",
            "Show me implementations of the Protocol",
            "Which concrete classes implement Repository?",
            "What subclasses of BaseEngine exist?",
        ],
    )
    def test_interface_queries_match(self, query: str) -> None:
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert matches, f"Expected interface match for: {query}"
        assert any("implement" in aug.lower() for aug in matches)

    @pytest.mark.parametrize(
        "query",
        [
            "How does authentication work?",
            "Explain the search pipeline",
            "What is the purpose of this module?",
            "How are errors handled in the API layer?",
            "Describe the indexing architecture",
        ],
    )
    def test_nonstructural_queries_do_not_match(self, query: str) -> None:
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert not matches, f"Non-structural query should not match: {query}"

    def test_multiple_patterns_can_match(self) -> None:
        """A query touching multiple structural concerns matches multiple patterns."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        query = "What calls parse_config and what type does it return?"
        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert len(matches) >= 2, f"Expected multiple matches, got {len(matches)}"


class TestAdversarialPatternMatching:
    """Adversarial stress tests for graph pattern matching."""

    def test_empty_query_no_match(self) -> None:
        """Empty string should match nothing and not crash."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        matches = [aug for pat, aug in ALL_PATTERNS if pat.search("")]
        assert matches == []

    def test_single_char_query_no_match(self) -> None:
        """Single character should match nothing."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        for char in ["?", "a", ".", "*", "\n"]:
            matches = [aug for pat, aug in ALL_PATTERNS if pat.search(char)]
            assert matches == [], f"Single char '{repr(char)}' should not match"

    def test_unicode_query(self) -> None:
        """Unicode in query should not crash pattern matching."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        queries = [
            "What calls función_principal?",
            "型チェーン what type does it return?",
            "Кто вызывает parse_config?",
            "🔍 callers of handle_request",
        ]
        for query in queries:
            # Should not raise
            matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
            assert isinstance(matches, list)

    def test_whitespace_only_query(self) -> None:
        """Whitespace-only query should match nothing."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        for ws in ["   ", "\t\t", "\n\n", "  \t\n  "]:
            matches = [aug for pat, aug in ALL_PATTERNS if pat.search(ws)]
            assert matches == [], f"Whitespace-only should not match: {repr(ws)}"

    def test_control_chars_in_query(self) -> None:
        """Control characters should not crash or produce false matches."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        query = "What\x00calls\x01parse_config\x07?"
        # Should not raise
        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert isinstance(matches, list)

    def test_dense_all_patterns_match(self) -> None:
        """Query touching all 5 structural categories should return 5 augmentations."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        query = (
            "What calls parse_config, what type does it return, "
            "which tests exercise it, what does it import, "
            "and what implements the Parser interface?"
        )
        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert len(matches) == 5, f"Expected all 5 patterns to match, got {len(matches)}"

    def test_semantically_hostile_structural_keywords(self) -> None:
        """Structural keywords in nonsensical arrangement — valid matches, no crash."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        query = "type chain call graph test coverage import dependency interface implementors"
        # Some patterns may match fragments — that's fine, just shouldn't crash
        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert isinstance(matches, list)

    def test_very_long_query(self) -> None:
        """Very long query should not hang or crash pattern matching."""
        from chunkhound.services.prompts.graph_patterns import ALL_PATTERNS

        query = "What calls " + "a" * 10000 + "?"
        matches = [aug for pat, aug in ALL_PATTERNS if pat.search(query)]
        assert isinstance(matches, list)
