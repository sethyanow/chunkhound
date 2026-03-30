"""Tests for DuckDB symbols + symbol_edges schema (ch-a58).

Verifies that DuckDBProvider creates the symbols and symbol_edges tables
with correct columns, types, indexes, and schema versioning.
"""

from pathlib import Path

import duckdb

from chunkhound.providers.database.duckdb_provider import DuckDBProvider


# --- Helpers ---


def _connect_fresh(tmp_path: Path) -> DuckDBProvider:
    """Create and connect a DuckDBProvider to a fresh temp DB."""
    provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
    provider.connect()
    return provider


def _describe_table(provider: DuckDBProvider, table: str) -> dict[str, str]:
    """Return {column_name: column_type} for a table via the provider's executor."""
    rows = provider.execute_query(f"DESCRIBE {table}")
    return {row["column_name"]: row["column_type"] for row in rows}


def _get_index_names(provider: DuckDBProvider) -> set[str]:
    """Return set of all index names in the database."""
    rows = provider.execute_query("SELECT index_name FROM duckdb_indexes()")
    return {row["index_name"] for row in rows}


# --- Test 1: symbols table schema ---


def test_symbols_table_created_on_connect(tmp_path: Path) -> None:
    """Verify symbols table exists with all R2 columns and correct types."""
    provider = _connect_fresh(tmp_path)

    columns = _describe_table(provider, "symbols")

    # All R2-required columns must exist
    expected_columns = {
        "id": "INTEGER",
        "fqn": "VARCHAR",
        "name": "VARCHAR",
        "kind": "VARCHAR",
        "language": "VARCHAR",
        "file_id": "INTEGER",
        "file_path": "VARCHAR",
        "range_start": "INTEGER",
        "range_end": "INTEGER",
        "type_signature": "VARCHAR",
        "parent_fqn": "VARCHAR",
        "confidence": "FLOAT",
        "lsp_server": "VARCHAR",
        "created_at": "TIMESTAMP",
        "updated_at": "TIMESTAMP",
    }

    for col_name, col_type in expected_columns.items():
        assert col_name in columns, f"Missing column: {col_name}"
        assert columns[col_name] == col_type, (
            f"Column {col_name}: expected {col_type}, got {columns[col_name]}"
        )


# --- Test 2: symbol_edges table schema ---


def test_symbol_edges_table_created_on_connect(tmp_path: Path) -> None:
    """Verify symbol_edges table exists with all R2 columns and correct types."""
    provider = _connect_fresh(tmp_path)

    columns = _describe_table(provider, "symbol_edges")

    expected_columns = {
        "id": "INTEGER",
        "from_symbol_id": "INTEGER",
        "from_fqn": "VARCHAR",
        "from_file": "VARCHAR",
        "to_symbol_id": "INTEGER",
        "to_fqn": "VARCHAR",
        "to_file": "VARCHAR",
        "edge_kind": "VARCHAR",
        "confidence": "FLOAT",
        "lsp_server": "VARCHAR",
        "created_at": "TIMESTAMP",
    }

    for col_name, col_type in expected_columns.items():
        assert col_name in columns, f"Missing column: {col_name}"
        assert columns[col_name] == col_type, (
            f"Column {col_name}: expected {col_type}, got {columns[col_name]}"
        )


# --- Test 3: indexes exist ---


def test_symbol_indexes_created(tmp_path: Path) -> None:
    """Verify indexes on symbols and symbol_edges tables."""
    provider = _connect_fresh(tmp_path)

    index_names = _get_index_names(provider)

    expected_indexes = [
        "idx_symbols_fqn",
        "idx_symbols_file_id",
        "idx_symbols_file_path",
        "idx_symbols_kind",
        "idx_symbol_edges_from_symbol_id",
        "idx_symbol_edges_to_symbol_id",
        "idx_symbol_edges_edge_kind",
        "idx_symbol_edges_from_fqn",
        "idx_symbol_edges_to_fqn",
    ]

    for idx_name in expected_indexes:
        assert idx_name in index_names, f"Missing index: {idx_name}"


# --- Test 4: schema version ---


def test_schema_version_is_2(tmp_path: Path) -> None:
    """Fresh DB should have schema version 2."""
    provider = _connect_fresh(tmp_path)

    rows = provider.execute_query("SELECT MAX(version) AS max_ver FROM schema_version")
    version = rows[0]["max_ver"]
    assert version == 2, f"Expected schema version 2, got {version}"


def test_v1_db_migrated_to_v2(tmp_path: Path) -> None:
    """Existing v1 DB should gain symbols/symbol_edges tables and version 2 on reconnect."""
    db_file = tmp_path / "test.db"

    # Create a v1 database manually with raw duckdb
    conn = duckdb.connect(str(db_file))

    conn.execute("""
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            description TEXT
        )
    """)
    conn.execute(
        "INSERT INTO schema_version (version, description) VALUES (1, 'Initial schema')"
    )

    conn.execute("CREATE SEQUENCE IF NOT EXISTS files_id_seq")
    conn.execute("""
        CREATE TABLE files (
            id INTEGER PRIMARY KEY DEFAULT nextval('files_id_seq'),
            path TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            extension TEXT,
            size INTEGER,
            modified_time TIMESTAMP,
            content_hash TEXT,
            language TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("CREATE SEQUENCE IF NOT EXISTS chunks_id_seq")
    conn.execute("""
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY DEFAULT nextval('chunks_id_seq'),
            file_id INTEGER REFERENCES files(id),
            chunk_type TEXT NOT NULL,
            symbol TEXT,
            code TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            start_byte INTEGER,
            end_byte INTEGER,
            language TEXT,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("CREATE SEQUENCE IF NOT EXISTS embeddings_id_seq")
    conn.execute("""
        CREATE TABLE embeddings_1536 (
            id INTEGER PRIMARY KEY DEFAULT nextval('embeddings_id_seq'),
            chunk_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            embedding FLOAT[1536],
            dims INTEGER NOT NULL DEFAULT 1536,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.close()

    # Now connect via DuckDBProvider — should migrate v1 → v2
    provider = DuckDBProvider(db_path=db_file, base_directory=tmp_path)
    provider.connect()

    # Verify symbols table exists
    symbols_cols = _describe_table(provider, "symbols")
    assert "fqn" in symbols_cols, "symbols table missing after v1→v2 migration"

    # Verify symbol_edges table exists
    edges_cols = _describe_table(provider, "symbol_edges")
    assert "edge_kind" in edges_cols, "symbol_edges table missing after v1→v2 migration"

    # Verify version bumped to 2
    rows = provider.execute_query("SELECT MAX(version) AS max_ver FROM schema_version")
    version = rows[0]["max_ver"]
    assert version == 2, f"Expected version 2 after migration, got {version}"
