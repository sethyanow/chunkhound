"""Tests for research.py — deep_research_impl and CODE_RESEARCH_DESCRIPTION.

Verifies module-level imports and that CODE_RESEARCH_DESCRIPTION lives
in research.py (not search.py) for correct ownership.
"""

import pytest

pytestmark = pytest.mark.unit


def test_research_module_exports_deep_research_impl():
    """research.py is importable and exports deep_research_impl."""
    from chunkhound.mcp_server.tools.research import deep_research_impl

    assert callable(deep_research_impl)


def test_research_module_exports_description():
    """CODE_RESEARCH_DESCRIPTION lives in research.py, not search.py."""
    from chunkhound.mcp_server.tools.research import CODE_RESEARCH_DESCRIPTION

    assert isinstance(CODE_RESEARCH_DESCRIPTION, str)
    assert "code_research" in CODE_RESEARCH_DESCRIPTION.lower()


def test_research_tool_registered():
    """deep_research_impl registers as 'code_research' in TOOL_REGISTRY."""
    from chunkhound.mcp_server.tools.registry import TOOL_REGISTRY

    # Force import to trigger registration
    import chunkhound.mcp_server.tools.research  # noqa: F401

    assert "code_research" in TOOL_REGISTRY
