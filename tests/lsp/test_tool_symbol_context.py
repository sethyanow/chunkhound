"""Tests for the `symbol_context` MCP tool — compound symbol profile.

Verifies:
- Concurrent LSP call assembly (hover + definition + callers + callees)
- FQN lookup → graph_neighborhood integration
- Graceful degradation (partial LSP failures, graph failures)
- Response shape contracts
- Adversarial inputs (empty paths, edge positions, unicode, total failures)
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

from chunkhound.lsp.types import (
    CallHierarchyItem,
    HoverResult,
    Location,
)
from tests.lsp.mcp_tool_helpers import (
    call_symbol_context_tool,
    make_mock_config,
    make_mock_pool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


RESPONSE_TOP_KEYS = {"hover", "definition", "callers", "callees", "graph_neighborhood"}


def _make_full_client() -> AsyncMock:
    """Client with all 4 LSP operations returning non-empty results."""
    client = AsyncMock()
    client.hover = AsyncMock(return_value=HoverResult(
        contents="```python\ndef my_func(x: int) -> str\n```",
        range_start_line=10, range_start_char=0,
        range_end_line=10, range_end_char=7,
    ))
    client.go_to_definition = AsyncMock(return_value=[
        Location("file:///workspace/module.py", 10, 4, 15, 0),
    ])
    client.incoming_calls = AsyncMock(return_value=[
        CallHierarchyItem(
            name="caller_func", kind=12,
            uri="file:///workspace/caller.py",
            range_start_line=20, range_start_char=0,
            range_end_line=30, range_end_char=0,
            selection_range_start_line=20, selection_range_start_char=4,
            selection_range_end_line=20, selection_range_end_char=15,
        ),
    ])
    client.outgoing_calls = AsyncMock(return_value=[
        CallHierarchyItem(
            name="callee_func", kind=12,
            uri="file:///workspace/callee.py",
            range_start_line=5, range_start_char=0,
            range_end_line=10, range_end_char=0,
            selection_range_start_line=5, selection_range_start_char=4,
            selection_range_end_line=5, selection_range_end_char=15,
        ),
    ])
    return client


def _make_graph_services():
    """Services with FQN lookup + graph walk canned responses."""
    services = MagicMock()
    services.provider.execute_query.side_effect = [
        # FQN lookup
        [{"fqn": "module::my_func"}],
        # graph_walk nodes
        [
            {"fqn": "module::my_func", "name": "my_func", "kind": "function",
             "file_path": "module.py", "depth": 0},
            {"fqn": "module::callee_func", "name": "callee_func", "kind": "function",
             "file_path": "callee.py", "depth": 1},
        ],
        # graph_walk edges
        [
            {"from_fqn": "module::my_func", "to_fqn": "module::callee_func",
             "edge_kind": "calls", "from_file": "module.py", "to_file": "callee.py"},
        ],
    ]
    return services


def _make_no_fqn_services():
    """Services where FQN lookup returns nothing."""
    services = MagicMock()
    services.provider.execute_query.return_value = []
    return services


# ---------------------------------------------------------------------------
# Happy path: all 5 response sections populated
# ---------------------------------------------------------------------------


class TestSymbolContextAssembly:
    """Compound profile assembles 4 concurrent LSP calls + graph lookup."""

    @pytest.mark.asyncio
    async def test_full_response_contract(self) -> None:
        """All 5 top-level keys present with correct shapes."""
        client = _make_full_client()
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_graph_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="/workspace/module.py", line=10, character=4,
        )

        assert set(result.keys()) == RESPONSE_TOP_KEYS
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_hover_extracts_contents_string(self) -> None:
        """Hover section is the contents string, not the full HoverResult."""
        client = _make_full_client()
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_graph_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="/workspace/module.py", line=10, character=4,
        )

        assert "def my_func" in result["hover"]
        assert isinstance(result["hover"], str)

    @pytest.mark.asyncio
    async def test_definition_transforms_locations(self) -> None:
        """Definition section uses _location_to_dict — URI stripped."""
        client = _make_full_client()
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_graph_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="/workspace/module.py", line=10, character=4,
        )

        assert len(result["definition"]) == 1
        assert result["definition"][0]["file_path"] == "/workspace/module.py"

    @pytest.mark.asyncio
    async def test_callers_and_callees_transformed(self) -> None:
        """Both callers and callees use _call_item_to_dict."""
        client = _make_full_client()
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_graph_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="/workspace/module.py", line=10, character=4,
        )

        assert len(result["callers"]) == 1
        assert result["callers"][0]["name"] == "caller_func"
        assert len(result["callees"]) == 1
        assert result["callees"][0]["name"] == "callee_func"

    @pytest.mark.asyncio
    async def test_graph_neighborhood_from_fqn_lookup(self) -> None:
        """FQN lookup hit → graph_walk populates neighborhood."""
        client = _make_full_client()
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_graph_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="/workspace/module.py", line=10, character=4,
        )

        gn = result["graph_neighborhood"]
        assert gn is not None
        assert len(gn["results"]) == 2
        assert len(gn["edges"]) == 1
        assert gn["count"] == 2


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestSymbolContextDegradation:
    """Partial failures produce partial results, never crashes."""

    @pytest.mark.asyncio
    async def test_no_hover_still_has_other_fields(self) -> None:
        """hover returns None → result.hover is None, other fields populated."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        client.go_to_definition = AsyncMock(
            return_value=[Location("file:///workspace/foo.py", 10, 0, 10, 10)],
        )
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_no_fqn_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
        )

        assert result["hover"] is None
        assert len(result["definition"]) == 1
        assert result["callers"] == []
        assert result["callees"] == []

    @pytest.mark.asyncio
    async def test_no_symbol_in_index_nulls_graph(self) -> None:
        """FQN lookup returns [] → graph_neighborhood is null."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(
            contents="type info",
            range_start_line=5, range_start_char=0,
            range_end_line=5, range_end_char=4,
        ))
        client.go_to_definition = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_no_fqn_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
        )

        assert result["hover"] == "type info"
        assert result["graph_neighborhood"] is None

    @pytest.mark.asyncio
    async def test_one_lsp_call_fails_others_succeed(self) -> None:
        """incoming_calls raises → callers empty, other fields present."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(
            contents="hover data",
            range_start_line=10, range_start_char=0,
            range_end_line=10, range_end_char=5,
        ))
        client.go_to_definition = AsyncMock(
            return_value=[Location("file:///workspace/foo.py", 10, 0, 10, 10)],
        )
        client.incoming_calls = AsyncMock(side_effect=Exception("connection reset"))
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_no_fqn_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
        )

        assert "error" not in result
        assert result["hover"] == "hover data"
        assert len(result["definition"]) == 1
        assert result["callers"] == []  # failed gracefully
        assert result["callees"] == []

    @pytest.mark.asyncio
    async def test_graph_walk_failure_nulls_neighborhood(self) -> None:
        """FQN found but graph_walk raises → graph_neighborhood null, LSP fields present."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(
            contents="hover data",
            range_start_line=10, range_start_char=0,
            range_end_line=10, range_end_char=5,
        ))
        client.go_to_definition = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()

        call_count = 0

        def fqn_then_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"fqn": "module::my_func"}]
            raise RuntimeError("DuckDB schema error")

        services = MagicMock()
        services.provider.execute_query.side_effect = fqn_then_fail

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
        )

        assert "error" not in result
        assert result["hover"] == "hover data"
        assert result["graph_neighborhood"] is None


# ---------------------------------------------------------------------------
# Error guards
# ---------------------------------------------------------------------------


class TestSymbolContextGuards:
    """Pre-condition checks before any LSP calls are made."""

    @pytest.mark.asyncio
    async def test_pool_not_ready(self) -> None:
        config = make_mock_config()
        services = MagicMock()

        result = await call_symbol_context_tool(
            pool=None, config=config, services=services,
        )

        assert result["error"] == "lsp_not_ready"

    @pytest.mark.asyncio
    async def test_unsupported_language(self) -> None:
        pool = make_mock_pool()
        config = make_mock_config()
        services = MagicMock()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="/workspace/data.xyz",
        )

        assert result["error"] == "unsupported_language"

    @pytest.mark.asyncio
    async def test_file_uri_input_normalized(self) -> None:
        """file:// URI is converted to path before language detection."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        client.go_to_definition = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_no_fqn_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="file:///workspace/foo.py",
        )

        assert "error" not in result
        assert "hover" in result


# ---------------------------------------------------------------------------
# Adversarial stress tests
# ---------------------------------------------------------------------------


class TestSymbolContextAdversarial:
    """Edge inputs and failure modes."""

    @pytest.mark.asyncio
    async def test_empty_file_path(self) -> None:
        """Empty string → unsupported_language (no extension)."""
        pool = make_mock_pool()
        config = make_mock_config()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=MagicMock(),
            file="", line=0, character=0,
        )

        assert result["error"] == "unsupported_language"

    @pytest.mark.asyncio
    async def test_zero_position_valid(self) -> None:
        """line=0, character=0 is valid LSP (first position in file)."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        client.go_to_definition = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_no_fqn_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            line=0, character=0,
        )

        assert "error" not in result

    @pytest.mark.asyncio
    async def test_large_line_number_passed_through(self) -> None:
        """Very large line number forwarded to LSP — server decides validity."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        client.go_to_definition = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_no_fqn_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            line=999999, character=0,
        )

        assert "error" not in result

    @pytest.mark.asyncio
    async def test_self_referential_graph(self) -> None:
        """Symbol with self-edge → graph_walk handles without infinite loop."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        client.go_to_definition = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()

        services = MagicMock()
        services.provider.execute_query.side_effect = [
            [{"fqn": "mod::recursive"}],
            [{"fqn": "mod::recursive", "name": "recursive", "kind": "function",
              "file_path": "mod.py", "depth": 0}],
            [{"from_fqn": "mod::recursive", "to_fqn": "mod::recursive",
              "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"}],
        ]

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="/workspace/mod.py", line=5, character=0,
        )

        assert "error" not in result
        assert result["graph_neighborhood"] is not None
        assert result["graph_neighborhood"]["count"] == 1

    @pytest.mark.asyncio
    async def test_unicode_file_path(self) -> None:
        """Unicode in filename doesn't crash path resolution."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        client.go_to_definition = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_no_fqn_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="/workspace/módulo.py",
        )

        assert "error" not in result

    @pytest.mark.asyncio
    async def test_all_lsp_calls_fail(self) -> None:
        """Every LSP call raises → all fields null/empty, not crash."""
        client = AsyncMock()
        client.hover = AsyncMock(side_effect=Exception("fail"))
        client.go_to_definition = AsyncMock(side_effect=Exception("fail"))
        client.incoming_calls = AsyncMock(side_effect=Exception("fail"))
        client.outgoing_calls = AsyncMock(side_effect=Exception("fail"))
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_no_fqn_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
        )

        assert "error" not in result
        assert result["hover"] is None
        assert result["definition"] == []
        assert result["callers"] == []
        assert result["callees"] == []
        assert result["graph_neighborhood"] is None

    @pytest.mark.asyncio
    async def test_file_path_with_spaces(self) -> None:
        """Spaces in path don't break resolution or URI construction."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        client.go_to_definition = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()
        services = _make_no_fqn_services()

        result = await call_symbol_context_tool(
            pool=pool, config=config, services=services,
            file="/workspace/my project/foo.py",
        )

        assert "error" not in result
