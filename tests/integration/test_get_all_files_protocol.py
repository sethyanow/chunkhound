"""Integration tests for the get_all_files protocol method.

Verifies both DuckDB and LanceDB providers return all indexed file records
with id and path via the DatabaseProvider protocol. This method is required
by lsp_population.populate_files() after the provider-agnostic migration.
"""

from pathlib import Path

import pytest

from chunkhound.core.types.common import FilePath, Timestamp
from chunkhound.providers.database.duckdb_provider import DuckDBProvider

pytestmark = pytest.mark.integration


class TestDuckDBGetAllFiles:
    """DuckDB provider must implement get_all_files."""

    def test_returns_all_inserted_files(self, tmp_path: Path) -> None:
        provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
        provider.connect()
        try:
            provider.execute_query(
                "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
                ["src/a.py", "a.py", ".py", "python", 100],
            )
            provider.execute_query(
                "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
                ["src/b.py", "b.py", ".py", "python", 200],
            )

            files = provider.get_all_files()

            assert len(files) == 2
            paths = {f["path"] for f in files}
            assert paths == {"src/a.py", "src/b.py"}
            # Each record must include id for file-id-keyed operations
            assert all("id" in f and isinstance(f["id"], int) for f in files)
        finally:
            provider.disconnect()

    def test_returns_empty_list_when_no_files(self, tmp_path: Path) -> None:
        provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
        provider.connect()
        try:
            assert provider.get_all_files() == []
        finally:
            provider.disconnect()

    @pytest.mark.asyncio
    async def test_async_variant_returns_all_files(self, tmp_path: Path) -> None:
        provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
        provider.connect()
        try:
            provider.execute_query(
                "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
                ["src/c.py", "c.py", ".py", "python", 300],
            )

            files = await provider.get_all_files_async()

            assert len(files) == 1
            assert files[0]["path"] == "src/c.py"
            assert isinstance(files[0]["id"], int)
        finally:
            provider.disconnect()


class TestLanceDBGetAllFiles:
    """LanceDB provider must implement get_all_files."""

    def test_returns_all_inserted_files(self, lancedb_provider) -> None:
        from chunkhound.core.models import File
        from chunkhound.core.types.common import Language

        lancedb_provider.insert_file(
            File(
                path=FilePath("src/a.py"),
                mtime=Timestamp(1.0),
                size_bytes=100,
                language=Language.PYTHON,
            )
        )
        lancedb_provider.insert_file(
            File(
                path=FilePath("src/b.py"),
                mtime=Timestamp(2.0),
                size_bytes=200,
                language=Language.PYTHON,
            )
        )

        files = lancedb_provider.get_all_files()

        assert len(files) == 2
        paths = {f["path"] for f in files}
        assert paths == {"src/a.py", "src/b.py"}
        assert all("id" in f and isinstance(f["id"], int) for f in files)

    def test_returns_empty_list_when_no_files(self, lancedb_provider) -> None:
        assert lancedb_provider.get_all_files() == []

    @pytest.mark.asyncio
    async def test_async_variant_returns_all_files(self, lancedb_provider) -> None:
        from chunkhound.core.models import File
        from chunkhound.core.types.common import Language

        lancedb_provider.insert_file(
            File(
                path=FilePath("src/c.py"),
                mtime=Timestamp(3.0),
                size_bytes=300,
                language=Language.PYTHON,
            )
        )

        files = await lancedb_provider.get_all_files_async()

        assert len(files) == 1
        assert files[0]["path"] == "src/c.py"
