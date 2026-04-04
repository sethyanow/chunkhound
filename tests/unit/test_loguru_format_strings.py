"""Regression: loguru uses {} format strings, not printf %s."""

from pathlib import Path

import pytest
from loguru import logger

from chunkhound.providers.database.duckdb_provider import DuckDBProvider

pytestmark = pytest.mark.unit


class TestExecuteQueryErrorLogging:
    """execute_query must log the actual error and query on failure, not literal %s."""

    def test_failed_query_logs_error_details(self, tmp_path: Path) -> None:
        """When execute_query fails, the log message should contain
        the exception text AND a truncated version of the query.
        Loguru uses {} format strings — %s would appear literally."""
        provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
        provider.connect()

        captured: list[str] = []
        handler_id = logger.add(lambda msg: captured.append(str(msg)), level="ERROR")

        bad_query = "SELECT * FROM nonexistent_table_12345"
        try:
            with pytest.raises(Exception):
                provider.execute_query(bad_query)

            # The log message must contain the actual error text, not literal %s
            assert len(captured) == 1, f"Expected 1 error log, got {len(captured)}"
            log_msg = captured[0]
            assert "%s" not in log_msg, (
                f"Log contains literal '%s' — loguru needs {{}} format strings.\n"
                f"Got: {log_msg}"
            )
            assert "nonexistent_table_12345" in log_msg, (
                f"Log should contain the query text.\nGot: {log_msg}"
            )
        finally:
            logger.remove(handler_id)
