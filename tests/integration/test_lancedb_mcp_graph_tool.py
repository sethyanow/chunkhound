"""End-to-end test: MCP graph tool returns results when backed by LanceDB.

ch-nxu SC3: ``chunkhound mcp`` with ``"provider": "lancedb"`` returns graph
tool results. This verifies the full execute_tool → provider_agnostic tool
implementation → LanceDBProvider path without raw SQL, without sqlglot, and
without subprocess overhead.
"""

from types import SimpleNamespace

import pytest

from chunkhound.core.models import File
from chunkhound.core.models.symbol import EdgeRow, SymbolRow
from chunkhound.core.types.common import Language
from chunkhound.mcp_server.tools import execute_tool

pytestmark = pytest.mark.integration


def _seed_lancedb_graph(provider) -> None:
    """Insert a small graph: A→B→C plus an orphan symbol in a sub-scope."""
    core_file = provider.insert_file(
        File(path="src/core.py", mtime=1.0, language=Language.PYTHON, size_bytes=100)
    )
    util_file = provider.insert_file(
        File(path="src/util.py", mtime=1.0, language=Language.PYTHON, size_bytes=100)
    )

    symbols: list[SymbolRow] = [
        SymbolRow(
            fqn="src::core::A", name="A", kind="Function", language="python",
            file_id=core_file, file_path="src/core.py",
            range_start=0, range_end=10, confidence=1.0, lsp_server="pyright",
            parent_fqn=None, type_signature="() -> None",
        ),
        SymbolRow(
            fqn="src::core::B", name="B", kind="Function", language="python",
            file_id=core_file, file_path="src/core.py",
            range_start=15, range_end=25, confidence=1.0, lsp_server="pyright",
            parent_fqn=None, type_signature="() -> None",
        ),
        SymbolRow(
            fqn="src::core::C", name="C", kind="Function", language="python",
            file_id=core_file, file_path="src/core.py",
            range_start=30, range_end=40, confidence=1.0, lsp_server="pyright",
            parent_fqn=None, type_signature="() -> None",
        ),
        SymbolRow(
            fqn="src::util::orphan", name="orphan", kind="Function", language="python",
            file_id=util_file, file_path="src/util.py",
            range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
            parent_fqn=None, type_signature=None,
        ),
    ]
    provider.insert_symbols_batch(symbols)
    fqn_map = provider.query_symbol_fqns_by_file(core_file)

    edges: list[EdgeRow] = [
        EdgeRow(
            from_symbol_id=fqn_map["src::core::A"], from_fqn="src::core::A",
            from_file="src/core.py",
            to_symbol_id=fqn_map["src::core::B"], to_fqn="src::core::B",
            to_file="src/core.py",
            edge_kind="calls", confidence=1.0, lsp_server="pyright",
        ),
        EdgeRow(
            from_symbol_id=fqn_map["src::core::B"], from_fqn="src::core::B",
            from_file="src/core.py",
            to_symbol_id=fqn_map["src::core::C"], to_fqn="src::core::C",
            to_file="src/core.py",
            edge_kind="calls", confidence=1.0, lsp_server="pyright",
        ),
    ]
    provider.insert_edges_batch(edges)


@pytest.mark.asyncio
async def test_graph_walk_against_lancedb(lancedb_provider) -> None:
    """MCP graph tool walk operation returns connected nodes from LanceDB."""
    _seed_lancedb_graph(lancedb_provider)
    services = SimpleNamespace(provider=lancedb_provider)

    result = await execute_tool(
        tool_name="graph",
        services=services,
        embedding_manager=None,
        arguments={"operation": "walk", "symbol": "src::core::A", "depth": 2, "limit": 20},
    )

    assert isinstance(result, dict), f"expected dict, got {type(result).__name__}: {result!r}"
    assert "error" not in result, f"unexpected error: {result}"
    results = result.get("results") or []
    fqns = {row.get("fqn") for row in results}
    assert "src::core::A" in fqns
    assert "src::core::B" in fqns
    assert "src::core::C" in fqns
    assert "src::util::orphan" not in fqns  # not connected


@pytest.mark.asyncio
async def test_graph_overview_against_lancedb(lancedb_provider) -> None:
    """MCP graph tool overview returns per-symbol edge counts from LanceDB."""
    _seed_lancedb_graph(lancedb_provider)
    services = SimpleNamespace(provider=lancedb_provider)

    result = await execute_tool(
        tool_name="graph",
        services=services,
        embedding_manager=None,
        arguments={"operation": "overview", "limit": 20},
    )

    assert isinstance(result, dict)
    assert "error" not in result
    symbols = result.get("symbols") or []
    assert len(symbols) >= 1, f"expected at least one entry in overview, got {result}"
    # Verify contract: each entry has fqn + total_edges + breakdown
    top = symbols[0]
    assert "fqn" in top and "total_edges" in top and "breakdown" in top


@pytest.mark.asyncio
async def test_get_stats_against_lancedb(lancedb_provider) -> None:
    """MCP get_stats tool returns symbol/edge counts + language breakdown from LanceDB."""
    _seed_lancedb_graph(lancedb_provider)
    services = SimpleNamespace(provider=lancedb_provider)

    result = await execute_tool(
        tool_name="get_stats",
        services=services,
        embedding_manager=None,
        arguments={},
    )

    assert isinstance(result, dict)
    assert result.get("symbols") == 4
    assert result.get("symbol_edges") == 2
    languages = result.get("languages") or []
    assert any(lang.get("language") == "python" for lang in languages)
