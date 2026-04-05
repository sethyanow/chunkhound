"""Regression tests for realtime indexing DB access patterns (ch-zn5).

Bug: _consume_events called provider.delete_file_completely_async() directly,
racing with _process_loop's open transactions. All DB-mutating operations must
route through the file queue so _process_loop is the single serialization point.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chunkhound.services.realtime_indexing_service import RealtimeIndexingService

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_services():
    """Create mock services for RealtimeIndexingService."""
    services = MagicMock()
    services.provider = MagicMock()
    services.provider.delete_file_completely_async = AsyncMock(return_value=True)
    services.provider.search_regex_async = AsyncMock(return_value=([], {}))
    return services


@pytest.fixture
def realtime_service(mock_services):
    """Create a RealtimeIndexingService with mocked dependencies."""
    config = MagicMock()
    config.exclude_patterns = []
    service = RealtimeIndexingService(
        services=mock_services,
        config=config,
    )
    service.watch_path = Path("/fake/path")
    return service


@pytest.mark.asyncio
async def test_remove_file_does_not_call_provider_directly(
    realtime_service, mock_services,
):
    """remove_file must route through the file queue, not call provider directly.

    Before the fix, remove_file called provider.delete_file_completely_async()
    which raced with _process_loop's open transactions, causing FATAL DuckDB
    corruption.
    """
    file_path = Path("/fake/path/test.py")

    await realtime_service.remove_file(file_path)

    # The provider should NOT be called directly
    mock_services.provider.delete_file_completely_async.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_deleted_directory_does_not_call_provider_directly(
    realtime_service, mock_services,
):
    """_cleanup_deleted_directory must route through the file queue."""
    mock_services.provider.search_regex_async = AsyncMock(
        return_value=([{"file_path": "subdir/a.py"}, {"file_path": "subdir/b.py"}], {})
    )

    await realtime_service._cleanup_deleted_directory("subdir")

    # search_regex_async for discovery is acceptable (read-only)
    # but delete_file_completely_async should NOT be called directly
    mock_services.provider.delete_file_completely_async.assert_not_called()
