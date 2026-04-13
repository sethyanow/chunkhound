"""Shared mock factories and helpers for MCP tool tests.

Separate from conftest.py which has population-test helpers with different
signatures (e.g. _make_mock_pool returns (pool, client) tuple there).
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from chunkhound.mcp_server.tools import execute_tool


def make_mock_pool(client: AsyncMock | None = None) -> MagicMock:
    """Create a mock LSPClientPool with a controlled client.

    Unlike conftest._make_mock_pool (which takes symbols and returns a tuple),
    this one takes an optional pre-configured client and returns just the pool.
    """
    pool = MagicMock()
    if client is None:
        client = AsyncMock()
    pool.get = AsyncMock(return_value=client)
    pool._clients = {}
    return pool


def make_mock_config(target_dir: str = "/workspace") -> MagicMock:
    """Create a mock Config with target_dir."""
    config = MagicMock()
    config.target_dir = Path(target_dir)
    return config


def make_mock_services(
    query_results: list[list[dict[str, Any]]] | None = None,
) -> MagicMock:
    """Create mock services with execute_query returning sequential results.

    Each element in query_results is returned for successive execute_query calls.

    ch-nxu Step 17: When ``query_results`` is the 3-list graph-pipeline format
    ``[symbol_rows, walk_rows, chunk_rows]`` — used by the structural search
    and graph walk expander tests — this helper ALSO wires the protocol
    methods so the migrated expander sees the expected data:

    - ``provider.symbol_overlap(chunks)`` → ``[row["fqn"] for row in symbol_rows]``
    - ``provider.graph_walk(...)`` → ``(walk_rows, [])``
    - ``provider.chunk_resolution(fqns)`` → ``chunk_rows``

    Legacy ``execute_query.side_effect`` wiring is preserved for any test that
    still reads results directly through the raw SQL path.
    """
    services = MagicMock()
    if query_results:
        services.provider.execute_query.side_effect = query_results
        # Wire the graph-pipeline protocol methods when the 3-list format is
        # passed (structural + graph-expander tests). Other shapes pass through.
        if len(query_results) == 3:
            symbol_rows, walk_rows, chunk_rows = query_results
            services.provider.symbol_overlap.return_value = [
                row["fqn"] for row in symbol_rows if "fqn" in row
            ]
            services.provider.graph_walk.return_value = (walk_rows, [])
            services.provider.chunk_resolution.return_value = chunk_rows
    else:
        services.provider.execute_query.return_value = []
    return services


async def call_lsp_tool(
    *,
    pool: MagicMock | None,
    config: MagicMock,
    file: str = "/workspace/foo.py",
    line: int = 10,
    character: int = 4,
    operation: str = "definition",
) -> dict[str, Any] | str:
    """Call the lsp tool with common defaults, reducing per-test boilerplate."""
    return await execute_tool(
        tool_name="lsp",
        services=MagicMock(),
        embedding_manager=None,
        arguments={
            "file": file,
            "line": line,
            "character": character,
            "operation": operation,
        },
        lsp_client_pool=pool,
        config=config,
    )


async def call_graph_tool(
    *,
    services: MagicMock,
    **arguments: Any,
) -> dict[str, Any] | str:
    """Call the graph tool with common defaults."""
    return await execute_tool(
        tool_name="graph",
        services=services,
        embedding_manager=None,
        arguments=arguments,
    )


async def call_search_tool(
    *,
    services: MagicMock,
    embedding_manager: MagicMock | None = None,
    **arguments: Any,
) -> dict[str, Any] | str:
    """Call the search tool with common defaults."""
    return await execute_tool(
        tool_name="search",
        services=services,
        embedding_manager=embedding_manager,
        arguments=arguments,
    )


async def call_get_stats_tool(
    *,
    services: MagicMock,
    lsp_client_pool: MagicMock | None = None,
) -> dict[str, Any] | str:
    """Call the get_stats tool with common defaults."""
    return await execute_tool(
        tool_name="get_stats",
        services=services,
        embedding_manager=None,
        arguments={},
        lsp_client_pool=lsp_client_pool,
    )


async def call_symbol_context_tool(
    *,
    pool: MagicMock | None,
    config: MagicMock,
    services: MagicMock,
    file: str = "/workspace/foo.py",
    line: int = 10,
    character: int = 4,
) -> dict[str, Any] | str:
    """Call the symbol_context tool with common defaults."""
    return await execute_tool(
        tool_name="symbol_context",
        services=services,
        embedding_manager=None,
        arguments={
            "file": file,
            "line": line,
            "character": character,
        },
        lsp_client_pool=pool,
        config=config,
    )
