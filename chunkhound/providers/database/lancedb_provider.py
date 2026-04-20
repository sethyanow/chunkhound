"""LanceDB provider implementation for ChunkHound - concrete database provider using LanceDB."""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pyarrow as pa
from loguru import logger

from chunkhound.core.models import Chunk, Embedding, File
from chunkhound.core.models.symbol import EdgeRow, SymbolRow
from chunkhound.core.types.common import ChunkType, Language

# Import existing components that will be used by the provider
from chunkhound.embeddings import EmbeddingManager
from chunkhound.providers.database.like_utils import escape_like_pattern
from chunkhound.providers.database.serial_database_provider import (
    SerialDatabaseProvider,
)
from chunkhound.utils.chunk_hashing import generate_chunk_id

# Type hinting only
if TYPE_CHECKING:
    from chunkhound.core.config.database_config import DatabaseConfig


# PyArrow schemas - avoiding LanceModel to prevent enum issues
def get_files_schema() -> pa.Schema:
    """Get PyArrow schema for files table."""
    return pa.schema(
        [
            ("id", pa.int64()),
            ("path", pa.string()),
            ("size", pa.int64()),
            ("modified_time", pa.float64()),
            ("content_hash", pa.string()),
            ("indexed_time", pa.float64()),
            ("language", pa.string()),
            ("encoding", pa.string()),
            ("line_count", pa.int64()),
        ]
    )


def get_chunks_schema(embedding_dims: int | None = None) -> pa.Schema:
    """Get PyArrow schema for chunks table.

    Args:
        embedding_dims: Number of dimensions for embedding vectors.
                       If None, uses variable-size list (which doesn't support vector search)
    """
    # Define embedding field based on whether we have fixed dimensions
    if embedding_dims is not None:
        embedding_field = pa.list_(pa.float32(), embedding_dims)  # Fixed-size list
    else:
        embedding_field = pa.list_(pa.float32())  # Variable-size list

    return pa.schema(
        [
            ("id", pa.int64()),
            ("file_id", pa.int64()),
            ("content", pa.string()),
            ("start_line", pa.int64()),
            ("end_line", pa.int64()),
            ("chunk_type", pa.string()),
            ("language", pa.string()),
            ("name", pa.string()),
            ("embedding", embedding_field),
            ("provider", pa.string()),
            ("model", pa.string()),
            ("created_time", pa.float64()),
            (
                "metadata",
                pa.string(),
            ),  # JSON-serialized chunk metadata (constants, etc.)
        ]
    )


def get_symbols_schema() -> pa.Schema:
    """Get PyArrow schema for symbols table."""
    return pa.schema(
        [
            ("id", pa.int64()),
            ("fqn", pa.string()),
            ("name", pa.string()),
            ("kind", pa.string()),
            ("language", pa.string()),
            ("file_id", pa.int64()),
            ("file_path", pa.string()),
            ("range_start", pa.int64()),
            ("range_end", pa.int64()),
            ("type_signature", pa.string()),
            ("parent_fqn", pa.string()),
            ("confidence", pa.float64()),
            ("lsp_server", pa.string()),
            ("created_at", pa.float64()),
        ]
    )


def get_symbol_edges_schema() -> pa.Schema:
    """Get PyArrow schema for symbol_edges table."""
    return pa.schema(
        [
            ("id", pa.int64()),
            ("from_symbol_id", pa.int64()),
            ("from_fqn", pa.string()),
            ("from_file", pa.string()),
            ("to_symbol_id", pa.int64()),
            ("to_fqn", pa.string()),
            ("to_file", pa.string()),
            ("edge_kind", pa.string()),
            ("confidence", pa.float64()),
            ("lsp_server", pa.string()),
            ("created_at", pa.float64()),
        ]
    )


def _has_valid_embedding(x: Any) -> bool:
    """Check if embedding is valid (not None, not empty, not all zeros).

    Handles both list and numpy array embeddings. Zero-vector detection
    provides defense-in-depth for legacy placeholder vectors.
    """
    if not hasattr(x, "__len__"):
        return False
    if x is None or not isinstance(x, (list, np.ndarray)) or len(x) == 0:
        return False
    # Check not all zeros (legacy placeholder detection)
    if isinstance(x, np.ndarray):
        return np.any(x != 0)
    return any(v != 0 for v in x)


def _deduplicate_by_id(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate LanceDB results by 'id' field, preserving order.

    LanceDB queries may return duplicates across table fragments until
    compaction runs. This helper ensures each chunk_id appears once.

    Args:
        results: List of result dictionaries with 'id' field

    Returns:
        Deduplicated list (first occurrence wins, preserves order)
    """
    if not results:
        return results

    seen: set[int] = set()
    unique: list[dict[str, Any]] = []

    for result in results:
        chunk_id = result.get("id")
        if chunk_id is not None and chunk_id not in seen:
            seen.add(chunk_id)
            unique.append(result)

    return unique


def _serialize_metadata(metadata: dict | None) -> str | None:
    """Serialize chunk metadata dict to JSON string for storage."""
    return json.dumps(metadata) if metadata else None


def _deserialize_metadata(metadata_json: str | None) -> dict:
    """Deserialize chunk metadata from JSON string."""
    return json.loads(metadata_json) if metadata_json else {}


def _escape_like_pattern(value: str) -> str:
    """Escape SQL LIKE metacharacters for prefix matching.

    Uses backslash as the escape character — Lance/DataFusion only supports
    backslash for ESCAPE, not arbitrary characters like '!'.
    """
    return escape_like_pattern(value, escape_quotes=True, escape_char="\\")


def _escape_lance_string(value: str) -> str:
    """Escape single quotes for LanceDB/DataFusion SQL string literals.

    For use in equality predicates: ``where(f"fqn = '{_escape_lance_string(fqn)}'")``.
    Only escapes ``'`` → ``''`` (SQL standard). Do NOT use for LIKE patterns
    (use ``_escape_like_pattern`` instead, which also escapes ``%``, ``_``, ``[``).
    """
    return value.replace("'", "''")


def _iter_batches(values: list[int], batch_size: int) -> list[list[int]]:
    """Split a list into fixed-size batches."""
    return [values[idx : idx + batch_size] for idx in range(0, len(values), batch_size)]


class LanceDBProvider(SerialDatabaseProvider):
    """LanceDB implementation using serial executor pattern."""

    def __init__(
        self,
        db_path: Path | str,
        base_directory: Path,
        embedding_manager: EmbeddingManager | None = None,
        config: "DatabaseConfig | None" = None,
    ):
        """Initialize LanceDB provider.

        Args:
            db_path: Path to LanceDB database directory
                (includes .lancedb suffix from DatabaseConfig.get_db_path())
            base_directory: Base directory for path normalization
            embedding_manager: Optional embedding manager for vector generation
            config: Database configuration for provider-specific settings
        """
        # Database path expected from DatabaseConfig.get_db_path() with .lancedb suffix
        # Ensure it's absolute to avoid LanceDB internal path resolution issues
        absolute_db_path = Path(db_path).absolute()

        # Initialize base class
        super().__init__(absolute_db_path, base_directory, embedding_manager, config)

        self.index_type = config.lancedb_index_type if config else None
        self._fragment_threshold = config.lancedb_optimize_fragment_threshold if config else 100
        self.connection: Any | None = None  # For backward compatibility only - do not use directly

        # Table references
        self._files_table = None
        self._chunks_table = None
        self._symbols_table = None
        self._symbol_edges_table = None

    def _build_path_like_clause(self, prefix: str) -> str:
        escaped = _escape_like_pattern(prefix)
        like = f"{escaped}%"
        return f"path LIKE '{like}' ESCAPE '\\\\'"

    def _fetch_file_paths_by_ids(self, file_ids: list[int]) -> dict[int, str]:
        if not self._files_table or not file_ids:
            return {}

        unique_ids = sorted({int(fid) for fid in file_ids if fid is not None})
        if not unique_ids:
            return {}

        ids_str = ",".join(map(str, unique_ids))
        try:
            rows = self._files_table.search().where(f"id IN ({ids_str})").to_list()
        except Exception as exc:
            logger.debug(f"Failed to batch fetch file paths: {exc}")
            return {}

        file_map: dict[int, str] = {}
        for row in rows:
            try:
                fid = int(row.get("id"))
                path = str(row.get("path") or "")
            except Exception:
                continue
            if path:
                file_map[fid] = path
        return file_map

    def _count_chunks_for_file_ids(self, file_ids: list[int], batch_size: int = 1000) -> int | None:
        if not self._chunks_table or not file_ids:
            return 0

        total = 0
        try:
            for batch in _iter_batches(file_ids, batch_size):
                file_ids_str = ",".join(map(str, batch))
                table = self._chunks_table.to_lance().to_table(filter=f"file_id IN ({file_ids_str})")
                batch_count = int(getattr(table, "num_rows", 0) or len(table))
                total += batch_count
            return total
        except Exception as exc:
            logger.debug(f"Failed to count chunks with batched filters: {exc}")
            return None

    def _create_connection(self) -> Any:
        """Create and return a LanceDB connection.

        This method is called from within the executor thread to create
        a thread-local connection.

        Returns:
            LanceDB connection object
        """
        import lancedb

        abs_db_path = self._db_path

        # Save CWD (thread-safe in executor)
        original_cwd = os.getcwd()
        try:
            os.chdir(abs_db_path.parent)
            conn = lancedb.connect(abs_db_path.name)
            return conn
        finally:
            os.chdir(original_cwd)

    def _get_schema_sql(self) -> list[str] | None:
        """LanceDB doesn't use SQL - return None."""
        return None

    def _executor_connect(self, conn: Any, state: dict[str, Any]) -> None:
        """Executor method for connect - runs in DB thread.

        Note: The connection is already created by _create_connection,
        so this method ensures schema and indexes are created.
        """
        try:
            # Store connection reference for backward compatibility
            self.connection = conn

            # Create schema and indexes in executor thread
            self._executor_create_schema(conn, state)
            self._executor_create_indexes(conn, state)

            logger.info(f"Connected to LanceDB at {self._db_path}")
        except Exception as e:
            logger.error(f"Error in LanceDB connect: {e}")
            raise

    def _executor_disconnect(self, conn: Any, state: dict[str, Any], skip_checkpoint: bool) -> None:
        """Executor method for disconnect - runs in DB thread."""
        try:
            # Clear connection and table references
            self.connection = None
            self._files_table = None
            self._chunks_table = None

            # Connection will be closed by base class
            logger.info("Disconnected from LanceDB")
        except Exception as e:
            logger.error(f"Error in LanceDB disconnect: {e}")
            raise

    def _get_embedding_dimensions_safe(self) -> int | None:
        """Safely retrieve embedding dimensions from embedding manager.

        Returns:
            Embedding dimensions if available, None otherwise.

        Notes:
            Handles all edge cases gracefully:
            - No embedding_manager configured
            - No default provider registered
            - Provider missing .dims attribute
            - Provider.dims raises exception
        """
        if self.embedding_manager is None:
            logger.debug("No embedding_manager configured - using variable-size schema")
            return None

        try:
            provider = self.embedding_manager.get_default_provider()
            if provider is None:
                logger.debug("No default embedding provider - using variable-size schema")
                return None

            dims = provider.dims
            if not isinstance(dims, int) or dims <= 0:
                logger.warning(f"Invalid embedding dimensions: {dims} - using variable-size schema")
                return None

            logger.debug(f"Detected embedding dimensions: {dims} (provider={provider.name}, model={provider.model})")
            return dims

        except AttributeError:
            logger.debug("Embedding provider has no 'dims' attribute - using variable-size schema")
            return None
        except Exception as e:
            logger.warning(f"Error detecting embedding dimensions: {e} - using variable-size schema")
            return None

    def create_schema(self) -> None:
        """Create database schema for files, chunks, and embeddings."""
        return self._execute_in_db_thread_sync(self._executor_create_schema)

    def _executor_create_schema(self, conn: Any, state: dict[str, Any]) -> None:
        """Executor method for create_schema - runs in DB thread."""
        # Create files table if it doesn't exist
        try:
            self._files_table = conn.open_table("files")
        except Exception:
            # Table doesn't exist, create it
            # Create table using PyArrow schema
            self._files_table = conn.create_table("files", schema=get_files_schema())
            logger.info("Created files table")

        # Create chunks table if it doesn't exist
        try:
            self._chunks_table = conn.open_table("chunks")
            logger.debug("Opened existing chunks table")
        except Exception:
            # Table doesn't exist, create it
            # Try to get embedding dimensions to avoid migration later
            embedding_dims = self._get_embedding_dimensions_safe()

            if embedding_dims is not None:
                logger.info(
                    f"Creating chunks table with fixed-size embedding schema "
                    f"({embedding_dims} dimensions) - no migration will be needed"
                )
            else:
                logger.info(
                    "Creating chunks table with variable-size embedding schema - "
                    "table will be migrated when first embeddings are inserted"
                )

            self._chunks_table = conn.create_table("chunks", schema=get_chunks_schema(embedding_dims))
            logger.info("Created chunks table")

        # Create symbols table if it doesn't exist
        try:
            self._symbols_table = conn.open_table("symbols")
        except Exception:
            self._symbols_table = conn.create_table("symbols", schema=get_symbols_schema())
            logger.info("Created symbols table")

        # Create symbol_edges table if it doesn't exist
        try:
            self._symbol_edges_table = conn.open_table("symbol_edges")
        except Exception:
            self._symbol_edges_table = conn.create_table("symbol_edges", schema=get_symbol_edges_schema())
            logger.info("Created symbol_edges table")

    def create_indexes(self) -> None:
        """Create database indexes for performance optimization."""
        return self._execute_in_db_thread_sync(self._executor_create_indexes)

    def _executor_create_indexes(self, conn: Any, state: dict[str, Any]) -> None:
        """Executor method for create_indexes - runs in DB thread."""
        # Validate LanceDB version for ivf_rq index type
        if self.index_type == "ivf_rq":
            import lancedb
            from packaging.version import Version

            lancedb_version = getattr(lancedb, "__version__", "0.0.0")
            if Version(lancedb_version) < Version("0.25.3"):
                raise ValueError(
                    f"ivf_rq index type requires LanceDB 0.25.3+, but found {lancedb_version}. "
                    f"Please upgrade: pip install 'lancedb>=0.25.3'"
                )

        # Create scalar index on chunks.id for merge_insert performance
        # merge_insert performs join on id column, index provides O(log n) vs O(n) lookup
        if self._chunks_table:
            try:
                # Check if index already exists
                indices = self._chunks_table.list_indices()
                has_id_index = any(idx.columns == ["id"] or "id" in idx.columns for idx in indices)

                if not has_id_index:
                    logger.info("Creating scalar index on chunks.id for merge_insert performance")
                    self._chunks_table.create_scalar_index("id")
                    logger.info("Scalar index on chunks.id created successfully")
                else:
                    logger.debug("Scalar index on chunks.id already exists")
            except Exception as e:
                # Non-fatal: merge_insert works without index, just slower
                logger.warning(
                    f"Could not create scalar index on chunks.id: {e}. "
                    f"This is non-fatal but may slow down merge_insert operations. "
                    f"Check LanceDB version supports create_scalar_index()."
                )

    def create_vector_index(self, provider: str, model: str, dims: int, metric: str = "cosine") -> None:
        """Create vector index for specific provider/model/dims combination."""
        return self._execute_in_db_thread_sync(self._executor_create_vector_index, provider, model, dims, metric)

    def _executor_create_vector_index(
        self,
        conn: Any,
        state: dict[str, Any],
        provider: str,
        model: str,
        dims: int,
        metric: str = "cosine",
    ) -> None:
        """Executor method for create_vector_index - runs in DB thread."""
        if not self._chunks_table:
            return

        try:
            # Check if index already exists by attempting a simple search
            try:
                test_vector = [0.0] * dims
                self._chunks_table.search(test_vector, vector_column_name="embedding").limit(1).to_list()
                logger.debug(f"Vector index already exists for {provider}/{model}")
                return
            except Exception:
                # Index doesn't exist, create it
                pass

            # Verify sufficient data exists for IVF PQ training
            total_embeddings = len(self._executor_get_existing_embeddings(conn, state, [], provider, model))
            if total_embeddings < 1000:
                logger.debug(
                    f"Skipping index creation for {provider}/{model}: insufficient data ({total_embeddings} < 1000)"
                )
                return

            # Create vector index (wait_timeout not supported in LanceDB OSS)
            if self.index_type == "ivf_hnsw_sq":
                self._chunks_table.create_index(
                    vector_column_name="embedding",
                    index_type="IVF_HNSW_SQ",
                    metric=metric,
                )
            elif self.index_type == "ivf_rq":
                self._chunks_table.create_index(
                    vector_column_name="embedding",
                    index_type="IVF_RQ",
                    metric=metric,
                )
            else:
                # Default to auto-configured index with explicit vector column
                self._chunks_table.create_index(vector_column_name="embedding", metric=metric)
            logger.debug(f"Created vector index for {provider}/{model} with metric={metric}")
        except Exception as e:
            logger.debug(f"Failed to create vector index for {provider}/{model}: {e}")

    def drop_vector_index(self, provider: str, model: str, dims: int, metric: str = "cosine") -> str:
        """Drop vector index for specific provider/model/dims combination."""
        # LanceDB handles index management automatically
        return "Index management handled automatically by LanceDB"

    def _generate_chunk_id_safe(self, chunk: Chunk) -> int:
        """Generate chunk ID with fallback to hash-based ID.

        Returns chunk.id if present, otherwise generates deterministic
        hash-based ID from file_id, content, and chunk type.

        Args:
            chunk: Chunk object to generate ID for

        Returns:
            Chunk ID (existing or generated)
        """
        return chunk.id or generate_chunk_id(
            chunk.file_id,
            chunk.code or "",
            concept=str(chunk.chunk_type.value if hasattr(chunk.chunk_type, "value") else chunk.chunk_type),
        )

    # File Operations
    def insert_file(self, file: File) -> int:
        """Insert file record and return file ID."""
        return self._execute_in_db_thread_sync(self._executor_insert_file, file)

    def _executor_insert_file(self, conn: Any, state: dict[str, Any], file: File) -> int:
        """Executor method for insert_file - runs in DB thread."""
        if not self._files_table:
            self._executor_create_schema(conn, state)

        # Store path as-is (now relative with forward slashes from IndexingCoordinator)
        normalized_path = file.path

        # Prepare file data
        file_data = {
            "id": file.id or int(time.time() * 1000000),
            "path": normalized_path,
            "size": file.size_bytes,
            "modified_time": file.mtime,
            "content_hash": getattr(file, "content_hash", None) or "",
            "indexed_time": time.time(),
            "language": str(file.language.value if hasattr(file.language, "value") else file.language),
            "encoding": "utf-8",
            "line_count": 0,
        }

        # Use merge_insert for atomic upsert based on path
        # This eliminates the TOCTOU race condition by making the
        # check-and-insert/update operation atomic at the database level
        self._files_table.merge_insert("path").when_matched_update_all().when_not_matched_insert_all().execute(
            [file_data]
        )

        # Get the file ID (either newly inserted or existing)
        # We need to query back because merge_insert doesn't return the ID
        result = self._files_table.search().where(f"path = '{normalized_path}'").to_list()
        if result:
            return result[0]["id"]  # type: ignore[no-any-return]  # lancedb to_list() -> List[dict]
        else:
            # This should not happen, but handle gracefully
            logger.error(f"Failed to retrieve file ID after merge_insert for path: {normalized_path}")
            return file_data["id"]

    def get_file_by_path(self, path: str, as_model: bool = False) -> dict[str, Any] | File | None:
        """Get file record by path."""
        return self._execute_in_db_thread_sync(self._executor_get_file_by_path, path, as_model)

    def _executor_get_file_by_path(
        self, conn: Any, state: dict[str, Any], path: str, as_model: bool = False
    ) -> dict[str, Any] | File | None:
        """Executor method for get_file_by_path - runs in DB thread."""
        if not self._files_table:
            return None

        try:
            # Normalize path to handle both absolute and relative paths
            from chunkhound.core.utils import normalize_path_for_lookup

            base_dir = state.get("base_directory")
            normalized_path = normalize_path_for_lookup(path, base_dir)
            results = self._files_table.search().where(f"path = '{normalized_path}'").to_list()
            if not results:
                return None

            result = results[0]
            if as_model:
                return File(
                    id=result["id"],
                    path=result["path"],
                    size_bytes=result["size"],
                    mtime=result["modified_time"],
                    language=Language(result["language"]),
                )
            return result
        except Exception as e:
            logger.error(f"Error getting file by path: {e}")
            return None

    def get_file_by_id(self, file_id: int, as_model: bool = False) -> dict[str, Any] | File | None:
        """Get file record by ID."""
        return self._execute_in_db_thread_sync(self._executor_get_file_by_id, file_id, as_model)

    def _executor_get_file_by_id(
        self, conn: Any, state: dict[str, Any], file_id: int, as_model: bool = False
    ) -> dict[str, Any] | File | None:
        """Executor method for get_file_by_id - runs in DB thread."""
        if not self._files_table:
            return None

        try:
            results = self._files_table.search().where(f"id = {file_id}").to_list()
            if not results:
                return None

            result = results[0]
            if as_model:
                return File(
                    id=result["id"],
                    path=result["path"],
                    size_bytes=result["size"],
                    mtime=result["modified_time"],
                    language=Language(result["language"]),
                )
            return result
        except Exception as e:
            logger.error(f"Error getting file by ID: {e}")
            return None

    def update_file(
        self,
        file_id: int,
        size_bytes: int | None = None,
        mtime: float | None = None,
        content_hash: str | None = None,
        **kwargs,
    ) -> None:
        """Update file record with new values."""
        return self._execute_in_db_thread_sync(self._executor_update_file, file_id, size_bytes, mtime, content_hash)

    def _executor_update_file(
        self,
        conn: Any,
        state: dict[str, Any],
        file_id: int,
        size_bytes: int | None = None,
        mtime: float | None = None,
        content_hash: str | None = None,
        **kwargs,
    ) -> None:
        """Executor method for update_file - runs in DB thread."""
        if not self._files_table:
            return

        try:
            # Get existing file record
            existing_file = self._executor_get_file_by_id(conn, state, file_id, False)
            if not existing_file:
                return

            # Update the relevant fields
            updated_file = dict(existing_file)
            if size_bytes is not None:
                updated_file["size"] = size_bytes
            if mtime is not None:
                updated_file["modified_time"] = mtime
            if content_hash is not None:
                updated_file["content_hash"] = content_hash
            updated_file["indexed_time"] = time.time()

            # LanceDB doesn't support in-place updates, so we use merge_insert
            # This updates the record by matching on the 'id' field
            self._files_table.merge_insert("id").when_matched_update_all().execute([updated_file])

        except Exception as e:
            logger.error(f"Error updating file {file_id}: {e}")

    def delete_file_completely(self, file_path: str) -> bool:
        """Delete a file and all its chunks/embeddings completely."""
        return self._execute_in_db_thread_sync(self._executor_delete_file_completely, file_path)

    def _executor_delete_file_completely(self, conn: Any, state: dict[str, Any], file_path: str) -> bool:
        """Executor method for delete_file_completely - runs in DB thread."""
        try:
            # Get file record in the executor thread
            file_record = self._executor_get_file_by_path(conn, state, file_path, False)
            if not file_record:
                return False

            file_id = file_record["id"]

            # Delete chunks first
            if self._chunks_table:
                self._chunks_table.delete(f"file_id = {file_id}")

            # Delete file record
            if self._files_table:
                self._files_table.delete(f"id = {file_id}")

            return True
        except Exception as e:
            logger.error(f"Error deleting file: {e}")
            return False

    # Chunk Operations
    def insert_chunk(self, chunk: Chunk) -> int:
        """Insert chunk record and return chunk ID."""
        return self._execute_in_db_thread_sync(self._executor_insert_chunk, chunk)

    def _executor_insert_chunk(self, conn: Any, state: dict[str, Any], chunk: Chunk) -> int:
        """Executor method for insert_chunk - runs in DB thread."""
        if not self._chunks_table:
            self._executor_create_schema(conn, state)

        chunk_data = {
            "id": self._generate_chunk_id_safe(chunk),
            "file_id": chunk.file_id,
            "content": chunk.code or "",
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "chunk_type": str(chunk.chunk_type.value if hasattr(chunk.chunk_type, "value") else chunk.chunk_type),
            "language": str(chunk.language.value if hasattr(chunk.language, "value") else chunk.language),
            "name": chunk.symbol or "",
            "embedding": None,
            "provider": "",
            "model": "",
            "created_time": time.time(),
            "metadata": _serialize_metadata(chunk.metadata),
        }

        # Use PyArrow Table directly to avoid LanceDB DataFrame schema alignment bug
        # Convert single item to proper format for pa.table
        chunk_data_list = [chunk_data]
        chunk_table = pa.Table.from_pylist(chunk_data_list, schema=get_chunks_schema())

        # Use merge_insert for atomic upsert with conflict-free semantics
        # Handles idempotency (same file indexed multiple times) and concurrent writes
        # (initial scan + file watcher, multiple processes, multiple file events)
        # LanceDB's MVCC ensures conflicts are resolved via automatic retries
        (
            self._chunks_table.merge_insert("id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(chunk_table)
        )
        return chunk_data["id"]

    def insert_chunks_batch(self, chunks: list[Chunk]) -> list[int]:
        """Insert multiple chunks in batch using optimized DataFrame operations."""
        return self._execute_in_db_thread_sync(self._executor_insert_chunks_batch, chunks)

    def _executor_insert_chunks_batch(self, conn: Any, state: dict[str, Any], chunks: list[Chunk]) -> list[int]:
        """Executor method for insert_chunks_batch - runs in DB thread."""
        if not chunks:
            return []

        if not self._chunks_table:
            self._executor_create_schema(conn, state)

        # Process in optimal batch sizes (LanceDB best practice: 1000+ items)
        batch_size = 1000
        all_chunk_ids = []

        for i in range(0, len(chunks), batch_size):
            batch_chunks = chunks[i : i + batch_size]
            chunk_data_list = []
            chunk_ids = []

            for chunk in batch_chunks:
                chunk_id = self._generate_chunk_id_safe(chunk)
                chunk_ids.append(chunk_id)

                chunk_data = {
                    "id": chunk_id,
                    "file_id": chunk.file_id,
                    "content": chunk.code or "",
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "chunk_type": str(
                        chunk.chunk_type.value if hasattr(chunk.chunk_type, "value") else chunk.chunk_type
                    ),
                    "language": str(chunk.language.value if hasattr(chunk.language, "value") else chunk.language),
                    "name": chunk.symbol or "",
                    "embedding": None,
                    "provider": "",
                    "model": "",
                    "created_time": time.time(),
                    "metadata": _serialize_metadata(chunk.metadata),
                }
                chunk_data_list.append(chunk_data)

            # Use PyArrow Table directly to avoid LanceDB DataFrame schema alignment bug
            chunks_table = pa.Table.from_pylist(chunk_data_list, schema=get_chunks_schema())

            # Use merge_insert for atomic upsert with conflict-free semantics
            # Handles idempotency (same file indexed multiple times) and concurrent writes
            # (initial scan + file watcher, multiple processes, multiple file events)
            # LanceDB's MVCC ensures conflicts are resolved via automatic retries
            (
                self._chunks_table.merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(chunks_table)
            )
            all_chunk_ids.extend(chunk_ids)

            logger.debug(f"Bulk inserted batch of {len(batch_chunks)} chunks")

        logger.debug(f"Completed bulk insert of {len(chunks)} chunks in batches")
        return all_chunk_ids

    def get_chunk_by_id(self, chunk_id: int, as_model: bool = False) -> dict[str, Any] | Chunk | None:
        """Get chunk record by ID."""
        return self._execute_in_db_thread_sync(self._executor_get_chunk_by_id, chunk_id, as_model)

    def _executor_get_chunk_by_id(
        self, conn: Any, state: dict[str, Any], chunk_id: int, as_model: bool = False
    ) -> dict[str, Any] | Chunk | None:
        """Executor method for get_chunk_by_id - runs in DB thread."""
        if not self._chunks_table:
            return None

        try:
            results = self._chunks_table.search().where(f"id = {chunk_id}").to_list()
            if not results:
                return None

            result = results[0]
            if as_model:
                return Chunk(
                    id=result["id"],
                    file_id=result["file_id"],
                    code=result["content"],
                    start_line=result["start_line"],
                    end_line=result["end_line"],
                    chunk_type=ChunkType(result["chunk_type"]),
                    language=Language(result["language"]),
                    symbol=result["name"],
                    metadata=_deserialize_metadata(result.get("metadata")),
                )
            return result
        except Exception as e:
            logger.error(f"Error getting chunk by ID: {e}")
            return None

    def get_chunks_by_file_id(self, file_id: int, as_model: bool = False) -> list[dict[str, Any] | Chunk]:
        """Get all chunks for a specific file."""
        return self._execute_in_db_thread_sync(self._executor_get_chunks_by_file_id, file_id, as_model)

    def _executor_get_chunks_by_file_id(
        self, conn: Any, state: dict[str, Any], file_id: int, as_model: bool = False
    ) -> list[dict[str, Any] | Chunk]:
        """Executor method for get_chunks_by_file_id - runs in DB thread."""
        if not self._chunks_table:
            return []

        try:
            results = self._chunks_table.search().where(f"file_id = {file_id}").to_list()
            # Deduplicate across fragments
            results = _deduplicate_by_id(results)

            if as_model:
                return [
                    Chunk(
                        id=result["id"],
                        file_id=result["file_id"],
                        code=result["content"],
                        start_line=result["start_line"],
                        end_line=result["end_line"],
                        chunk_type=ChunkType(result["chunk_type"]),
                        language=Language(result["language"]),
                        symbol=result["name"],
                        metadata=_deserialize_metadata(result.get("metadata")),
                    )
                    for result in results
                ]
            return results
        except Exception as e:
            logger.error(f"Error getting chunks by file ID: {e}")
            return []

    def get_chunks_in_range(self, file_id: int, start_line: int, end_line: int) -> list[dict[str, Any]]:
        """Get all chunks overlapping a line range (pattern from context_retriever.py)."""
        return self._execute_in_db_thread_sync(self._executor_get_chunks_in_range, file_id, start_line, end_line)

    def _executor_get_chunks_in_range(
        self,
        conn: Any,
        state: dict[str, Any],
        file_id: int,
        start_line: int,
        end_line: int,
    ) -> list[dict[str, Any]]:
        """Executor method for get_chunks_in_range - runs in DB thread.

        Args:
            file_id: ID of the file to search within
            start_line: Start line of the range
            end_line: End line of the range

        Returns:
            List of chunk dictionaries overlapping the range, ordered by start_line
        """
        if not self._chunks_table:
            return []

        try:
            # Overlap condition: chunk overlaps if any of:
            # - chunk start_line is within range
            # - chunk end_line is within range
            # - chunk spans the entire range
            # Using LanceDB's SQL-like where clause with logical OR
            where_clause = (
                f"file_id = {file_id} AND ("
                f"(start_line >= {start_line} AND start_line <= {end_line}) OR "
                f"(end_line >= {start_line} AND end_line <= {end_line}) OR "
                f"(start_line <= {start_line} AND end_line >= {end_line})"
                f")"
            )

            results = self._chunks_table.search().where(where_clause).to_list()

            # Deduplicate across fragments (critical for multi-result queries)
            results = _deduplicate_by_id(results)

            # Sort by start_line (LanceDB doesn't guarantee order from where clause)
            results.sort(key=lambda x: x.get("start_line", 0))

            # Convert to expected format with consistent field names
            chunks = []
            for result in results:
                chunk_dict = {
                    "id": result["id"],
                    "file_id": result["file_id"],
                    "chunk_type": result.get("chunk_type", ""),
                    "symbol": result.get("name", ""),
                    "code": result.get("content", ""),
                    "start_line": result.get("start_line", 0),
                    "end_line": result.get("end_line", 0),
                    "start_byte": None,  # LanceDB chunks table doesn't store byte offsets
                    "end_byte": None,
                    "language": result.get("language", ""),
                    "metadata": _deserialize_metadata(result.get("metadata")),
                    "created_at": result.get("created_time"),
                    "updated_at": None,  # LanceDB chunks table doesn't track updates
                }
                chunks.append(chunk_dict)

            return chunks

        except Exception as e:
            logger.error(f"Error getting chunks in range for file {file_id}: {e}")
            return []

    def delete_file_chunks(self, file_id: int) -> None:
        """Delete all chunks for a file."""
        return self._execute_in_db_thread_sync(self._executor_delete_file_chunks, file_id)

    def _executor_delete_file_chunks(self, conn: Any, state: dict[str, Any], file_id: int) -> None:
        """Executor method for delete_file_chunks - runs in DB thread."""
        if self._chunks_table:
            try:
                self._chunks_table.delete(f"file_id = {file_id}")
            except Exception as e:
                logger.error(f"Error deleting chunks for file {file_id}: {e}")

    def delete_chunk(self, chunk_id: int) -> None:
        """Delete a single chunk by ID."""
        return self._execute_in_db_thread_sync(self._executor_delete_chunk, chunk_id)

    def _executor_delete_chunk(self, conn: Any, state: dict[str, Any], chunk_id: int) -> None:
        """Executor method for delete_chunk - runs in DB thread."""
        if self._chunks_table:
            try:
                self._chunks_table.delete(f"id = {chunk_id}")
            except Exception as e:
                logger.error(f"Error deleting chunk {chunk_id}: {e}")

    def update_chunk(self, chunk_id: int, **kwargs) -> None:
        """Update chunk record with new values."""
        # LanceDB doesn't support in-place updates, need to implement via delete/insert
        pass

    # Embedding Operations
    def insert_embedding(self, embedding: Embedding) -> int:
        """Insert embedding record and return embedding ID."""
        # In LanceDB, embeddings are stored directly in the chunks table
        # This is a no-op since we use insert_embeddings_batch for efficiency
        return embedding.id or 0

    def insert_embeddings_batch(
        self,
        embeddings_data: list[dict],
        batch_size: int | None = None,
        connection=None,
    ) -> int:
        """Insert multiple embedding vectors efficiently using merge_insert."""
        return self._execute_in_db_thread_sync(self._executor_insert_embeddings_batch, embeddings_data, batch_size)

    def _executor_insert_embeddings_batch(
        self,
        conn: Any,
        state: dict[str, Any],
        embeddings_data: list[dict],
        batch_size: int | None = None,
    ) -> int:
        """Executor method for insert_embeddings_batch - runs in DB thread."""
        if not embeddings_data or not self._chunks_table:
            return 0

        try:
            # Determine embedding dimensions from the first embedding
            first_embedding = embeddings_data[0].get("embedding", embeddings_data[0].get("vector"))
            if not first_embedding:
                logger.error("No embedding data found in first record")
                return 0

            embedding_dims = len(first_embedding)
            provider = embeddings_data[0]["provider"]
            model = embeddings_data[0]["model"]

            # Check if embedding columns exist in schema and if they have the correct type
            current_schema = self._chunks_table.schema
            embedding_field = None
            for field in current_schema:
                if field.name == "embedding":
                    embedding_field = field
                    break

            # Check if we need to recreate the table due to schema mismatch
            needs_recreation = False
            if embedding_field:
                # Check if it's a fixed-size list with correct dimensions
                if not pa.types.is_fixed_size_list(embedding_field.type):
                    logger.info(
                        "Embedding column exists but is variable-size list"
                        " - need to recreate table with fixed-size list"
                    )
                    needs_recreation = True
                elif hasattr(embedding_field.type, "list_size") and embedding_field.type.list_size != embedding_dims:
                    logger.info(
                        f"Embedding column exists but has wrong dimensions"
                        f" ({embedding_field.type.list_size} vs {embedding_dims})"
                        f" - need to recreate table"
                    )
                    needs_recreation = True

            if needs_recreation:
                # Need to recreate table with proper fixed-size schema
                existing_data_df = self._chunks_table.to_pandas()
                logger.info(
                    f"Migrating chunks table to fixed-size embedding schema:\n"
                    f"  Reason: Schema created before embedding dimensions were known\n"
                    f"  Required dimensions: {embedding_dims} (provider={provider}, model={model})\n"
                    f"  Chunks to migrate: {len(existing_data_df):,}\n"
                    f"  This is a ONE-TIME operation - future embedding insertions will be fast\n"
                    f"  To avoid this in future: Ensure embedding provider configured before database creation"
                )

                # Drop the old table
                conn.drop_table("chunks")

                # Create new table with proper schema
                new_schema = get_chunks_schema(embedding_dims)
                self._chunks_table = conn.create_table("chunks", schema=new_schema)
                logger.info("Created new chunks table with fixed-size embedding schema")

                # Re-insert existing data (without embeddings - they'll be added below)
                if len(existing_data_df) > 0:
                    # Prepare data for reinsertion
                    chunks_to_restore = []
                    for _, row in existing_data_df.iterrows():
                        chunk_data = {
                            "id": row["id"],
                            "file_id": row["file_id"],
                            "content": row["content"],
                            "start_line": row["start_line"],
                            "end_line": row["end_line"],
                            "chunk_type": row["chunk_type"],
                            "language": row["language"],
                            "name": row["name"],
                            "embedding": None,  # No placeholder - needs embedding generation
                            "provider": "",
                            "model": "",
                            "created_time": row.get("created_time", time.time()),
                            "metadata": row.get("metadata"),  # Preserve existing metadata
                        }
                        chunks_to_restore.append(chunk_data)

                    # Insert in batches
                    restore_batch_size = 1000
                    for i in range(0, len(chunks_to_restore), restore_batch_size):
                        batch = chunks_to_restore[i : i + restore_batch_size]
                        restore_table = pa.Table.from_pylist(batch, schema=new_schema)
                        self._chunks_table.add(restore_table, mode="append")

                    logger.info(f"Restored {len(chunks_to_restore)} chunks to new table")

            elif not embedding_field:
                # Add embedding columns to the table if they don't exist
                logger.debug("Adding embedding columns to chunks table")
                # Create a proper fixed-size list type for the embedding column
                embedding_type = pa.list_(pa.float32(), embedding_dims)
                self._chunks_table.add_columns(
                    {
                        "embedding": f"arrow_cast(NULL, '{embedding_type}')",
                        "provider": "arrow_cast(NULL, 'string')",
                        "model": "arrow_cast(NULL, 'string')",
                    }
                )

            # Determine optimal batch size if not provided
            if batch_size is None:
                # Use larger batches for better performance, but cap at 10k to avoid memory issues
                batch_size = min(10000, len(embeddings_data))

            total_updated = 0

            # Process in batches for better memory management
            # Use read-modify-write pattern: LanceDB's when_matched_update_all()
            # requires ALL columns in source data to match target schema
            for i in range(0, len(embeddings_data), batch_size):
                batch = embeddings_data[i : i + batch_size]

                # Build lookup of chunk_id -> embedding data
                embedding_lookup = {}
                for e in batch:
                    embedding = e.get("embedding", e.get("vector"))
                    # Ensure embedding is a list
                    if hasattr(embedding, "tolist"):
                        embedding = embedding.tolist()
                    elif not isinstance(embedding, list):
                        embedding = list(embedding)
                    embedding_lookup[e["chunk_id"]] = {
                        "embedding": embedding,
                        "provider": e["provider"],
                        "model": e["model"],
                    }

                # Read existing rows for these chunk IDs
                # NOTE: Using Lance SQL filter instead of .search() because .search()
                # may not reliably find rows with NULL embedding columns (vector search semantics)
                chunk_ids = list(embedding_lookup.keys())
                chunk_ids_str = ",".join(map(str, chunk_ids))

                try:
                    # Primary: Use LanceDB's native Lance filter (efficient for large tables)
                    existing_df = self._chunks_table.to_lance().to_table(filter=f"id IN ({chunk_ids_str})").to_pandas()
                except Exception as lance_err:
                    # Fallback: Paginated pandas filtering (memory-safe for large tables)
                    total_rows = self._chunks_table.count_rows()

                    logger.warning(
                        f"Lance SQL filter unavailable, using paginated fallback for {total_rows:,} rows. "
                        f"Error: {lance_err}"
                    )

                    # Paginate to avoid loading entire table into memory
                    page_size = 10_000
                    existing_rows = []
                    chunk_ids_set = set(chunk_ids)  # For faster lookup

                    for offset in range(0, total_rows, page_size):
                        # Load batch of rows
                        try:
                            batch_df = self._chunks_table.to_pandas(offset=offset, limit=page_size)
                        except TypeError:
                            # LanceDB may not support offset/limit in to_pandas()
                            # Fall back to loading all and slicing (less efficient but works)
                            if offset == 0:
                                logger.debug("LanceDB to_pandas() doesn't support pagination, loading full table")
                                all_chunks_df = self._chunks_table.to_pandas()
                                batch_df = all_chunks_df[all_chunks_df["id"].isin(chunk_ids)]
                                existing_rows.extend(batch_df.to_dict("records"))
                                break
                            else:
                                break

                        # Filter to requested chunk IDs
                        matching = batch_df[batch_df["id"].isin(chunk_ids_set)]
                        if len(matching) > 0:
                            existing_rows.extend(matching.to_dict("records"))

                        # Early termination if we found all requested chunks
                        if len(existing_rows) >= len(chunk_ids):
                            break

                    # Convert to DataFrame for consistent downstream handling
                    existing_df = pd.DataFrame(existing_rows) if existing_rows else pd.DataFrame()

                # Diagnostic logging
                logger.debug(f"Looking for {len(chunk_ids)} chunk IDs, found {len(existing_df)} existing chunks")
                if len(existing_df) == 0 and len(chunk_ids) > 0:
                    total_rows = self._chunks_table.count_rows()
                    logger.warning(
                        f"Embedding update: search returned 0 results but table has {total_rows} rows. "
                        f"This indicates a LanceDB query issue. Using paginated fallback. "
                        f"Chunk IDs requested: {chunk_ids[:5]}{'...' if len(chunk_ids) > 5 else ''}"
                    )

                # Merge embedding data into existing rows (full row data required)
                merge_data = []
                for _, row in existing_df.iterrows():
                    chunk_id = row["id"]
                    if chunk_id in embedding_lookup:
                        emb_data = embedding_lookup[chunk_id]
                        merge_data.append(
                            {
                                "id": row["id"],
                                "file_id": row["file_id"],
                                "content": row["content"],
                                "start_line": row["start_line"],
                                "end_line": row["end_line"],
                                "chunk_type": row["chunk_type"],
                                "language": row["language"],
                                "name": row["name"],
                                "embedding": emb_data["embedding"],
                                "provider": emb_data["provider"],
                                "model": emb_data["model"],
                                "created_time": row["created_time"],
                                "metadata": row.get("metadata"),  # Preserve metadata
                            }
                        )

                # merge_insert with PyArrow table to avoid nullable field mismatches
                # (see LanceDB GitHub issue #2366)
                if merge_data:
                    merge_table = pa.Table.from_pylist(merge_data, schema=get_chunks_schema(embedding_dims))
                    (self._chunks_table.merge_insert("id").when_matched_update_all().execute(merge_table))

                total_updated += len(merge_data)

                if len(embeddings_data) > batch_size:
                    logger.debug(f"Processed {total_updated}/{len(embeddings_data)} embeddings")

            # Create vector index if we have enough embeddings
            total_rows = self._chunks_table.count_rows()
            if total_rows >= 256:  # LanceDB minimum for index creation
                try:
                    # Check if we need to create an index
                    # LanceDB will handle this efficiently if index already exists
                    self._executor_create_vector_index(conn, state, provider, model, embedding_dims)
                except Exception as e:
                    # This is expected if the table was created with variable-size list schema
                    # The index will work once the table is recreated with fixed-size schema
                    logger.debug(f"Vector index creation deferred (expected with initial schema): {e}")

            logger.debug(f"Successfully updated {total_updated} embeddings using merge_insert")
            return total_updated

        except Exception as e:
            logger.error(f"Error in bulk embedding insert: {e}")
            raise

    def get_embedding_by_chunk_id(self, chunk_id: int, provider: str, model: str) -> Embedding | None:
        """Get embedding for specific chunk, provider, and model."""
        chunk = self.get_chunk_by_id(chunk_id)
        if not chunk or not chunk.get("embedding"):
            return None

        created_time = chunk.get("created_time", time.time())
        created_at = datetime.fromtimestamp(created_time) if created_time else None

        return Embedding(
            chunk_id=chunk_id,
            provider=chunk.get("provider", provider),
            model=chunk.get("model", model),
            dims=len(chunk["embedding"]),
            vector=chunk["embedding"],
            created_at=created_at,
        )

    def get_existing_embeddings(self, chunk_ids: list[int], provider: str, model: str) -> set[int]:
        """Get set of chunk IDs that already have embeddings for given provider/model."""
        return self._execute_in_db_thread_sync(self._executor_get_existing_embeddings, chunk_ids, provider, model)

    def _executor_get_existing_embeddings(
        self,
        conn: Any,
        state: dict[str, Any],
        chunk_ids: list[int],
        provider: str,
        model: str,
    ) -> set[int]:
        """Executor method for get_existing_embeddings - runs in DB thread."""
        if not self._chunks_table:
            return set()

        try:
            # In LanceDB, we store embeddings directly in the chunks table
            # A chunk has embeddings if the embedding field is not null AND
            # the provider/model match what we're looking for
            chunks_count = self._chunks_table.count_rows()
            try:
                all_chunks_df = self._chunks_table.head(chunks_count).to_pandas()
            except Exception as data_error:
                logger.error(f"LanceDB data corruption detected in chunks table: {data_error}")
                logger.info("Attempting table recovery by recreating indexes...")
                # Try to recover by optimizing the table
                try:
                    self._chunks_table.optimize()
                    all_chunks_df = self._chunks_table.head(chunks_count).to_pandas()
                except Exception as recovery_error:
                    logger.error(f"Failed to recover chunks table: {recovery_error}")
                    return set()

            # Handle embeddings that are lists - pandas notna() might not work correctly with lists
            # Also check embedding is not all zeros (defense-in-depth for legacy placeholder vectors)
            embeddings_mask = all_chunks_df["embedding"].apply(_has_valid_embedding)

            # If no specific chunk_ids provided, check all chunks
            if not chunk_ids:
                # Find all chunks that have embeddings for this provider/model
                existing_embeddings_df = all_chunks_df[
                    embeddings_mask & (all_chunks_df["provider"] == provider) & (all_chunks_df["model"] == model)
                ]
            else:
                # Filter to only the requested chunk IDs
                filtered_df = all_chunks_df[all_chunks_df["id"].isin(chunk_ids)]
                filtered_embeddings_mask = filtered_df.index.isin(all_chunks_df[embeddings_mask].index)

                # Find chunks that have embeddings for this provider/model
                existing_embeddings_df = filtered_df[
                    filtered_embeddings_mask & (filtered_df["provider"] == provider) & (filtered_df["model"] == model)
                ]

            return set(existing_embeddings_df["id"].tolist())
        except Exception as e:
            logger.error(f"Error getting existing embeddings: {e}")
            return set()

    def delete_embeddings_by_chunk_id(self, chunk_id: int) -> None:
        """Delete all embeddings for a specific chunk."""
        # In LanceDB, this would involve updating the chunk to remove embedding data
        pass

    def get_all_chunks_with_metadata(self) -> list[dict[str, Any]]:
        """Get all chunks with their metadata including file paths (provider-agnostic)."""
        return self._execute_in_db_thread_sync(self._executor_get_all_chunks_with_metadata)

    def get_all_files(self) -> list[dict[str, Any]]:
        """Return all indexed file records (id + path at minimum)."""
        return self._execute_in_db_thread_sync(self._executor_get_all_files)

    def _executor_get_all_files(self, conn: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._files_table:
            return []
        try:
            rows = self._files_table.search().select(["id", "path"]).to_list()
        except Exception as e:
            logger.error(f"Error loading all files from LanceDB: {e}")
            return []
        return [{"id": int(r["id"]), "path": r["path"]} for r in rows]

    def get_scope_stats(self, scope_prefix: str | None) -> tuple[int, int]:
        """Return (total_files, total_chunks) under an optional scope prefix.

        Best-effort implementation for LanceDB. This should avoid loading full
        chunk content when possible, but LanceDB table APIs may vary across
        versions; callers should treat failures as non-fatal.
        """
        return self._execute_in_db_thread_sync(self._executor_get_scope_stats, scope_prefix)

    def _executor_get_scope_stats(self, conn: Any, state: dict[str, Any], scope_prefix: str | None) -> tuple[int, int]:
        if not self._files_table or not self._chunks_table:
            return 0, 0

        try:
            if scope_prefix:
                normalized = scope_prefix.replace("\\", "/")
                clause = self._build_path_like_clause(normalized)
                files = self._files_table.search().where(clause).to_list()
                file_ids: set[int] = set()
                for row in files:
                    try:
                        fid = row.get("id")
                        if fid is not None:
                            file_ids.add(int(fid))
                    except Exception:
                        continue

                total_files = len(file_ids)
                if not file_ids:
                    return 0, 0

                # Slow fallback: scan the chunks table and count rows whose
                # file_id is in the scoped file_id set.
                #
                # This avoids schema changes while still producing correct scoped
                # chunk totals, but may be memory-heavy for very large databases.
                total_chunks = 0

                try:
                    file_ids_list = sorted(file_ids)
                    batched_total = self._count_chunks_for_file_ids(file_ids_list)
                    if batched_total is not None:
                        return total_files, batched_total
                except Exception:
                    pass

                # Fallback: scan the chunks table in pages to avoid loading the
                # entire dataset at once when pagination is supported.
                try:
                    chunks_count = int(self._chunks_table.count_rows())
                except Exception:
                    chunks_count = 0

                if chunks_count <= 0:
                    return total_files, 0

                page_size = 10_000
                file_ids_set = set(file_ids)

                for offset in range(0, chunks_count, page_size):
                    try:
                        batch_df = self._chunks_table.to_pandas(offset=offset, limit=page_size)
                    except TypeError:
                        # LanceDB may not support offset/limit; fall back to a full load.
                        if offset == 0:
                            try:
                                chunks_df = self._chunks_table.to_pandas()
                            except Exception:
                                try:
                                    self._chunks_table.optimize()
                                    chunks_df = self._chunks_table.to_pandas()
                                except Exception:
                                    return total_files, 0
                            if "file_id" not in chunks_df.columns:
                                return total_files, 0
                            try:
                                total_chunks = int(chunks_df["file_id"].isin(list(file_ids_set)).sum())
                            except Exception:
                                total_chunks = 0
                        break

                    if "file_id" not in batch_df.columns:
                        return total_files, 0

                    try:
                        total_chunks += int(batch_df["file_id"].isin(list(file_ids_set)).sum())
                    except Exception:
                        continue

                return total_files, total_chunks

            # Root scope: counts over full tables.
            total_files = int(self._files_table.count_rows())
            total_chunks = int(self._chunks_table.count_rows())
            return total_files, total_chunks
        except Exception:
            return 0, 0

    def get_scope_file_paths(self, scope_prefix: str | None) -> list[str]:
        """Return file paths under an optional scope prefix."""
        return self._execute_in_db_thread_sync(self._executor_get_scope_file_paths, scope_prefix)

    def _executor_get_scope_file_paths(self, conn: Any, state: dict[str, Any], scope_prefix: str | None) -> list[str]:
        if not self._files_table:
            return []

        try:
            if scope_prefix:
                normalized = scope_prefix.replace("\\", "/")
                clause = self._build_path_like_clause(normalized)
                rows = self._files_table.search().where(clause).to_list()
            else:
                total = int(self._files_table.count_rows())
                rows = self._files_table.head(total).to_list()
        except Exception:
            return []

        out: list[str] = []
        for row in rows:
            try:
                path = str(row.get("path") or "").replace("\\", "/")
            except Exception:
                path = ""
            if path:
                out.append(path)
        out.sort()
        return out

    def _executor_get_all_chunks_with_metadata(self, conn: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Executor method for get_all_chunks_with_metadata - runs in DB thread."""
        if not self._chunks_table or not self._files_table:
            return []

        try:
            # Get all chunks using LanceDB native API (workaround for to_pandas() bug)
            chunks_count = self._chunks_table.count_rows()
            try:
                chunks_df = self._chunks_table.head(chunks_count).to_pandas()
            except Exception as data_error:
                logger.error(f"LanceDB data corruption detected in chunks table: {data_error}")
                logger.info("Attempting table recovery by recreating indexes...")
                try:
                    self._chunks_table.optimize()
                    chunks_df = self._chunks_table.head(chunks_count).to_pandas()
                except Exception as recovery_error:
                    logger.error(f"Failed to recover chunks table: {recovery_error}")
                    return []

            # Get all files for path lookup
            files_count = self._files_table.count_rows()
            try:
                files_df = self._files_table.head(files_count).to_pandas()
            except Exception as data_error:
                logger.error(f"LanceDB data corruption detected in files table: {data_error}")
                try:
                    self._files_table.optimize()
                    files_df = self._files_table.head(files_count).to_pandas()
                except Exception as recovery_error:
                    logger.error(f"Failed to recover files table: {recovery_error}")
                    return []

            # Create file_id to path mapping
            file_paths = dict(zip(files_df["id"], files_df["path"]))

            # Build result with file paths
            result = []
            for _, chunk in chunks_df.iterrows():
                result.append(
                    {
                        "id": chunk["id"],
                        "file_id": chunk["file_id"],
                        "file_path": file_paths.get(chunk["file_id"], ""),  # Keep stored format
                        "content": chunk["content"],
                        "start_line": chunk["start_line"],
                        "end_line": chunk["end_line"],
                        "chunk_type": chunk["chunk_type"],
                        "language": chunk["language"],
                        "name": chunk["name"],
                        "metadata": _deserialize_metadata(chunk.get("metadata")),
                    }
                )

            return result

        except Exception as e:
            logger.error(f"Error getting chunks with metadata: {e}")
            return []

    # Search Operations (delegate to base class which uses executor)
    def _executor_search_semantic(
        self,
        conn: Any,
        state: dict[str, Any],
        query_embedding: list[float],
        provider: str,
        model: str,
        page_size: int = 10,
        offset: int = 0,
        threshold: float | None = None,
        path_filter: str | None = None,
        fuzzy_path: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Executor method for search_semantic - runs in DB thread."""
        if self._chunks_table is None:
            raise RuntimeError("Chunks table not initialized")

        # Validate embeddings exist for this provider/model
        try:
            chunks_count = self._chunks_table.count_rows()
            if chunks_count == 0:
                return [], {
                    "offset": offset,
                    "page_size": 0,
                    "has_more": False,
                    "total": 0,
                }

            # Check if any chunks have embeddings for this provider/model
            try:
                sample_chunks = self._chunks_table.head(min(100, chunks_count)).to_pandas()
                # Handle embeddings that are lists - also exclude zero vectors
                embeddings_mask = sample_chunks["embedding"].apply(_has_valid_embedding)
            except Exception as data_error:
                logger.error(f"LanceDB data corruption detected during semantic search: {data_error}")
                return [], {
                    "offset": offset,
                    "page_size": 0,
                    "has_more": False,
                    "total": 0,
                }
            embeddings_exist = (
                embeddings_mask & (sample_chunks["provider"] == provider) & (sample_chunks["model"] == model)
            ).any()

            if not embeddings_exist:
                logger.warning(f"No embeddings found for provider={provider}, model={model}")
                return [], {
                    "offset": offset,
                    "page_size": 0,
                    "has_more": False,
                    "total": 0,
                }

            # Perform vector search with explicit vector column name
            query = self._chunks_table.search(query_embedding, vector_column_name="embedding")
            query = query.where(f"provider = '{provider}' AND model = '{model}' AND embedding IS NOT NULL")
            query = query.limit(page_size + offset)

            if threshold:
                query = query.where(f"_distance <= {threshold}")

            if path_filter:
                # Join with files table to filter by path
                pass  # Would need more complex query joining with files table

            results = query.to_list()

            # Deduplicate across fragments (safety net for fragment-induced duplicates)
            results = _deduplicate_by_id(results)

            # Apply offset manually since LanceDB doesn't have native offset
            paginated_results = results[offset : offset + page_size]

            # Format results to match DuckDB output and exclude raw embeddings
            file_map = self._fetch_file_paths_by_ids([r.get("file_id") for r in paginated_results if "file_id" in r])
            formatted_results = []
            for result in paginated_results:
                # Get file path from cached batch lookup
                file_path = file_map.get(result.get("file_id"), "")

                # Convert _distance to similarity (1 - distance for cosine)
                similarity = 1.0 - result.get("_distance", 0.0) if "_distance" in result else 1.0

                # Format the result to match DuckDB's output
                formatted_result = {
                    "chunk_id": result["id"],
                    "symbol": result.get("name", ""),
                    "content": result.get("content", ""),
                    "chunk_type": result.get("chunk_type", ""),
                    "start_line": result.get("start_line", 0),
                    "end_line": result.get("end_line", 0),
                    "file_path": file_path,  # Keep stored format
                    "language": result.get("language", ""),
                    "similarity": similarity,
                    "metadata": _deserialize_metadata(result.get("metadata")),
                }
                formatted_results.append(formatted_result)

            pagination = {
                "offset": offset,
                "page_size": len(paginated_results),
                "has_more": len(results) > offset + page_size,
                "total": len(results),
            }

            return formatted_results, pagination

        except Exception as e:
            logger.error(f"Error in semantic search with provider={provider}, model={model}: {e}")
            # Re-raise the error instead of silently returning empty results
            raise RuntimeError(f"Semantic search failed: {e}") from e

    def find_similar_chunks(
        self,
        chunk_id: int,
        provider: str,
        model: str,
        limit: int = 10,
        threshold: float | None = None,
        path_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find chunks similar to the given chunk using its embedding.

        Args:
            chunk_id: ID of the chunk to find similar chunks for
            provider: Embedding provider name
            model: Embedding model name
            limit: Maximum number of results to return
            threshold: Optional similarity threshold (0-1, where 1 is most similar)
            path_filter: Optional relative path to limit search scope

        Returns:
            List of similar chunks with scores and metadata
        """
        return self._execute_in_db_thread_sync(
            self._executor_find_similar_chunks,
            chunk_id,
            provider,
            model,
            limit,
            threshold,
            path_filter,
        )

    def _executor_find_similar_chunks(
        self,
        conn: Any,
        state: dict[str, Any],
        chunk_id: int,
        provider: str,
        model: str,
        limit: int,
        threshold: float | None,
        path_filter: str | None,
    ) -> list[dict[str, Any]]:
        """Executor method for find_similar_chunks - runs in DB thread.

        LanceDB-specific implementation:
        - Embeddings stored inline in chunks table (no separate embeddings table)
        - Uses native .search() API with vector_column_name parameter
        - Path filtering deferred (not yet implemented in LanceDB)
        """
        if self._chunks_table is None:
            raise RuntimeError("Chunks table not initialized")

        try:
            # PHASE 1: Retrieve target chunk's embedding
            # In LanceDB, embeddings are stored directly in chunks table
            target_results = (
                self._chunks_table.search()
                .where(f"id = {chunk_id} AND provider = '{provider}' AND model = '{model}'")
                .limit(1)
                .to_list()
            )

            if not target_results:
                logger.warning(f"No embedding found for chunk_id={chunk_id}, provider='{provider}', model='{model}'")
                return []

            target_chunk = target_results[0]
            target_embedding = target_chunk.get("embedding")

            # Validate embedding exists and is valid
            if not _has_valid_embedding(target_embedding):
                logger.warning(f"Chunk {chunk_id} has no valid embedding for provider={provider}, model={model}")
                return []

            # PHASE 2: Vector search for similar chunks
            query = self._chunks_table.search(target_embedding, vector_column_name="embedding")
            query = query.where(
                f"provider = '{provider}' AND model = '{model}' AND embedding IS NOT NULL AND id != {chunk_id}"
            )

            # Note: Cannot filter by _distance in WHERE clause - it only exists in results
            # We'll filter after getting results if threshold is specified
            # Request more results than needed if using threshold
            fetch_limit = limit * 3 if threshold is not None else limit
            query = query.limit(fetch_limit)

            # TODO(#107): Path filtering not yet implemented in LanceDB
            # See https://github.com/chunkhound/chunkhound/issues/107
            if path_filter:
                logger.warning("Path filtering not yet implemented for LanceDB find_similar_chunks")

            results = query.to_list()

            # PHASE 3: Format results with file paths and apply threshold
            filtered_results: list[tuple[dict[str, Any], float]] = []
            for result in results:
                # Convert distance to similarity score (cosine: similarity = 1 - distance)
                distance = result.get("_distance", 0.0)
                similarity = 1.0 - distance

                # Apply threshold filter if specified
                if threshold is not None and similarity < threshold:
                    continue
                filtered_results.append((result, similarity))
                if len(filtered_results) >= limit:
                    break

            file_map = self._fetch_file_paths_by_ids([r.get("file_id") for r, _ in filtered_results if "file_id" in r])

            formatted_results = []
            for result, similarity in filtered_results:
                file_path = file_map.get(result.get("file_id"), "")
                formatted_results.append(
                    {
                        "chunk_id": result["id"],
                        "name": result.get("name", ""),
                        "content": result.get("content", ""),
                        "chunk_type": result.get("chunk_type", ""),
                        "start_line": result.get("start_line", 0),
                        "end_line": result.get("end_line", 0),
                        "file_path": file_path,
                        "language": result.get("language", ""),
                        "score": similarity,  # Match DuckDB convention
                        "metadata": _deserialize_metadata(result.get("metadata")),
                    }
                )

            return formatted_results

        except Exception as e:
            logger.error(f"Failed to find similar chunks: {e}")
            return []

    def _executor_search_regex(
        self,
        conn: Any,
        state: dict[str, Any],
        pattern: str,
        page_size: int,
        offset: int,
        path_filter: str | None,
        fuzzy_path: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Executor method for search_regex - runs in DB thread."""
        if not self._chunks_table or not self._files_table:
            return [], {"offset": offset, "page_size": 0, "has_more": False, "total": 0}

        try:
            # Build WHERE clause using regexp_match (DataFusion SQL function)
            # Escape single quotes in pattern to prevent SQL injection
            escaped_pattern = pattern.replace("'", "''")
            where_clause = f"regexp_match(content, '{escaped_pattern}')"

            # Get all matching chunks
            # Note: .search().where() without vector may return duplicates across fragments
            results = self._chunks_table.search().where(where_clause).to_list()

            # Deduplicate across fragments (critical fix for fragmentation bug)
            results = _deduplicate_by_id(results)

            # Apply path filter if provided
            if path_filter:
                # Get file IDs matching path filter
                normalized_path = path_filter.replace("\\", "/")
                clause = self._build_path_like_clause(normalized_path)
                file_results = self._files_table.search().where(clause).to_list()
                valid_file_ids = {r["id"] for r in file_results}
                results = [r for r in results if r["file_id"] in valid_file_ids]

            total_count = len(results)

            # Apply pagination
            paginated = results[offset : offset + page_size]

            # Format results with file paths
            file_map = self._fetch_file_paths_by_ids([r.get("file_id") for r in paginated if "file_id" in r])
            formatted = []
            for result in paginated:
                file_path = file_map.get(result.get("file_id"), "")

                formatted.append(
                    {
                        "chunk_id": result["id"],
                        "symbol": result.get("name", ""),
                        "content": result.get("content", ""),
                        "chunk_type": result.get("chunk_type", ""),
                        "start_line": result.get("start_line", 0),
                        "end_line": result.get("end_line", 0),
                        "file_path": file_path,
                        "language": result.get("language", ""),
                        "metadata": _deserialize_metadata(result.get("metadata")),
                    }
                )

            pagination = {
                "offset": offset,
                "page_size": len(paginated),
                "has_more": total_count > offset + page_size,
                "total": total_count,
            }

            return formatted, pagination

        except Exception as e:
            logger.error(f"Error in regex search: {e}")
            raise RuntimeError(f"Regex search failed: {e}") from e

    def _executor_search_text(
        self,
        conn: Any,
        state: dict[str, Any],
        query: str,
        page_size: int = 10,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Executor method for search_text - runs in DB thread."""
        if not self._chunks_table:
            return [], {"offset": offset, "page_size": 0, "has_more": False, "total": 0}

        try:
            # Use LanceDB's LIKE pattern matching for text search
            escaped_query = _escape_like_pattern(query)
            where_clause = f"content LIKE '%{escaped_query}%' ESCAPE '\\\\'"
            results = self._chunks_table.search().where(where_clause).limit(page_size + offset).to_list()

            # Apply offset manually
            paginated_results = results[offset : offset + page_size]

            # Format results
            file_map = self._fetch_file_paths_by_ids([r.get("file_id") for r in paginated_results if "file_id" in r])
            formatted_results = []
            for result in paginated_results:
                file_path = file_map.get(result.get("file_id"), "")
                formatted_result = {
                    "chunk_id": result["id"],
                    "symbol": result.get("name", ""),
                    "content": result.get("content", ""),
                    "chunk_type": result.get("chunk_type", ""),
                    "start_line": result.get("start_line", 0),
                    "end_line": result.get("end_line", 0),
                    "file_path": file_path,
                    "language": result.get("language", ""),
                }
                formatted_results.append(formatted_result)

            pagination = {
                "offset": offset,
                "page_size": len(paginated_results),
                "has_more": len(results) > offset + page_size,
                "total": len(results),
            }

            return formatted_results, pagination

        except Exception as e:
            logger.error(f"Error in text search: {e}")
            return [], {"offset": offset, "page_size": 0, "has_more": False, "total": 0}

    # Statistics and Monitoring
    def get_stats(self) -> dict[str, int]:
        """Get database statistics (file count, chunk count, etc.)."""
        return self._execute_in_db_thread_sync(self._executor_get_stats)

    def _executor_get_stats(self, conn: Any, state: dict[str, Any]) -> dict[str, int]:
        """Executor method for get_stats - runs in DB thread.

        ch-3zc: previously materialized tables via ``.to_pandas()`` just to
        call ``len(...)``. On ~80K chunks × 1024-dim embeddings that exceeded
        the 30s ``CHUNKHOUND_DB_EXECUTE_TIMEOUT``, so users saw zero counts.
        Now uses ``count_rows()`` — a metadata lookup — which scales with
        fragment count, not row content. The ``embeddings`` key is dropped
        (was "chunks with valid embedding", a semantic metric we can't get
        cheaply); callers default to 0 via ``.get("embeddings", 0)``.
        """
        stats: dict[str, int] = {"files": 0, "chunks": 0, "size_mb": 0}

        try:
            if self._files_table is not None:
                try:
                    stats["files"] = int(self._files_table.count_rows())
                except Exception as data_error:
                    logger.warning(f"Failed to count files: {data_error}")
                    stats["files"] = 0

            if self._chunks_table is not None:
                try:
                    stats["chunks"] = int(self._chunks_table.count_rows())
                except Exception as data_error:
                    logger.warning(f"Failed to count chunks: {data_error}")
                    stats["chunks"] = 0

            # Calculate size (approximate)
            if self._db_path.exists():
                total_size = sum(f.stat().st_size for f in self._db_path.rglob("*") if f.is_file())
                stats["size_mb"] = int(total_size // (1024 * 1024))

        except Exception as e:
            logger.error(f"Error getting stats: {e}")

        return stats

    def get_file_stats(self, file_id: int) -> dict[str, Any]:
        """Get statistics for a specific file."""
        return self._execute_in_db_thread_sync(self._executor_get_file_stats, file_id)

    def _executor_get_file_stats(self, conn: Any, state: dict[str, Any], file_id: int) -> dict[str, Any]:
        """Executor method for get_file_stats - runs in DB thread."""
        chunks = self._executor_get_chunks_by_file_id(conn, state, file_id, False)
        return {
            "file_id": file_id,
            "chunk_count": len(chunks),
            "embedding_count": sum(
                1
                for chunk in chunks
                if chunk.get("embedding") is not None
                and isinstance(chunk.get("embedding"), (list, np.ndarray))
                and len(chunk.get("embedding", [])) > 0
            ),
        }

    def get_provider_stats(self, provider: str, model: str) -> dict[str, Any]:
        """Get statistics for a specific embedding provider/model."""
        return self._execute_in_db_thread_sync(self._executor_get_provider_stats, provider, model)

    def _executor_get_provider_stats(
        self, conn: Any, state: dict[str, Any], provider: str, model: str
    ) -> dict[str, Any]:
        """Executor method for get_provider_stats - runs in DB thread."""
        if not self._chunks_table:
            return {"provider": provider, "model": model, "embedding_count": 0}

        try:
            results = (
                self._chunks_table.search()
                .where(f"provider = '{provider}' AND model = '{model}' AND embedding IS NOT NULL")
                .to_list()
            )

            return {
                "provider": provider,
                "model": model,
                "embedding_count": len(results),
            }
        except Exception as e:
            logger.error(f"Error getting provider stats: {e}")
            return {"provider": provider, "model": model, "embedding_count": 0}

    # Transaction and Bulk Operations
    def execute_query(self, query: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Execute a limited subset of read queries for coordinator helpers.

        LanceDB has no SQL interface; this adapter recognizes a small set of
        patterns used by higher layers (e.g., change detection in the indexing
        coordinator) and serves equivalent results via the native API.

        Supported forms:
        - SELECT path, size, modified_time, content_hash FROM files
        - SELECT path, size, modified_time FROM files
        """
        try:
            if not self._files_table:
                return []

            q = (query or "").strip().lower().replace("\n", " ")
            if q.startswith("select") and " from files" in q:
                # Determine requested columns
                cols: list[str] = []
                try:
                    select_part = q.split("from", 1)[0]
                    select_part = select_part.replace("select", "").strip()
                    cols = [c.strip() for c in select_part.split(",") if c.strip()]
                except Exception:
                    cols = ["path", "size", "modified_time", "content_hash"]

                # Fetch all rows via native API
                try:
                    total = int(self._files_table.count_rows())
                except Exception:
                    total = 0
                rows: list[dict[str, Any]] = []
                try:
                    if total > 0:
                        df = self._files_table.head(total).to_pandas()
                    else:
                        # Fallback for engines that don't support count_rows
                        df = self._files_table.to_pandas()
                    # Normalize frame into list of dicts with requested columns
                    for _, rec in df.iterrows():
                        out: dict[str, Any] = {}
                        for c in cols:
                            if c in rec:
                                out[c] = rec[c]
                            else:
                                # Provide None for missing optional columns
                                out[c] = None
                        rows.append(out)
                    return rows
                except Exception:
                    return []

            # Unsupported pattern → no-op (coordinator will fall back)
            return []
        except Exception:
            return []

    # File Processing Integration (inherited from base class)
    async def process_file_incremental(self, file_path: Path) -> dict[str, Any]:
        """Process a file with incremental parsing and differential chunking."""
        if not self._services_initialized:
            self._initialize_shared_instances()

        # Call process_file with embeddings enabled for real-time indexing
        # This ensures embeddings are generated immediately for modified files
        return await self._indexing_coordinator.process_file(file_path, skip_embeddings=False)

    # Health and Diagnostics
    def get_fragment_count(self) -> dict[str, int]:
        """Get current fragment counts for chunks and files tables.

        Returns:
            Dictionary with fragment counts: {"chunks": 551, "files": 12}
        """
        return self._execute_in_db_thread_sync(self._executor_get_fragment_count)

    def _executor_get_fragment_count(self, conn: Any, state: dict[str, Any]) -> dict[str, int]:
        """Executor method for get_fragment_count - runs in DB thread."""
        result = {}

        if self._chunks_table:
            try:
                stats = self._chunks_table.stats()
                result["chunks"] = stats.fragment_stats.num_fragments
            except Exception as e:
                logger.debug(f"Could not get chunks fragment count: {e}")
                result["chunks"] = 0

        if self._files_table:
            try:
                stats = self._files_table.stats()
                result["files"] = stats.fragment_stats.num_fragments
            except Exception as e:
                logger.debug(f"Could not get files fragment count: {e}")
                result["files"] = 0

        return result

    def should_optimize(self, operation: str = "") -> bool:
        """Check if optimization is warranted based on fragment count vs threshold.

        Args:
            operation: Optional operation name for logging (e.g., "post-chunking")

        Returns:
            True if fragment count exceeds threshold, False otherwise
        """
        try:
            counts = self.get_fragment_count()
            chunks_fragments = counts.get("chunks", 0)
            if chunks_fragments < self._fragment_threshold:
                op_desc = f" {operation}" if operation else ""
                logger.debug(
                    f"Skipping{op_desc} optimization: {chunks_fragments} fragments "
                    f"< threshold {self._fragment_threshold}"
                )
                return False
            return True
        except Exception as e:
            logger.debug(f"Could not check fragment count, will optimize: {e}")
            return True

    def optimize_tables(self) -> None:
        """Optimize tables by compacting fragments and rebuilding indexes."""
        return self._execute_in_db_thread_sync(self._executor_optimize_tables)

    def _executor_optimize_tables(self, conn: Any, state: dict[str, Any]) -> None:
        """Executor method for optimize_tables - runs in DB thread."""
        from datetime import timedelta

        try:
            if self._chunks_table:
                logger.debug("Optimizing chunks table - compacting fragments...")
                # Use minimal cleanup window (1 minute) to focus on fragment consolidation
                # rather than time-based cleanup. The goal is compaction, not age-based deletion.
                stats = self._chunks_table.optimize(cleanup_older_than=timedelta(minutes=1), delete_unverified=True)
                if stats is not None:
                    logger.debug(f"Chunks table cleanup freed {stats.bytes_removed / 1024 / 1024:.2f} MB")
                logger.debug("Chunks table optimization complete")

            if self._files_table:
                logger.debug("Optimizing files table - compacting fragments...")
                stats = self._files_table.optimize(cleanup_older_than=timedelta(minutes=1), delete_unverified=True)
                if stats is not None:
                    logger.debug(f"Files table cleanup freed {stats.bytes_removed / 1024 / 1024:.2f} MB")
                logger.debug("Files table optimization complete")

        except Exception as e:
            logger.warning(f"Failed to optimize tables: {e}")

    def health_check(self) -> dict[str, Any]:
        """Perform health check and return status information."""
        return self._execute_in_db_thread_sync(self._executor_health_check)

    def _executor_health_check(self, conn: Any, state: dict[str, Any]) -> dict[str, Any]:
        """Executor method for health_check - runs in DB thread."""
        health_status = {
            "status": "healthy" if self.is_connected else "disconnected",
            "provider": "lancedb",
            "database_path": str(self._db_path),
            "tables": {
                "files": self._files_table is not None,
                "chunks": self._chunks_table is not None,
            },
        }

        # Check for data corruption
        if self.is_connected and self._chunks_table:
            try:
                # Try to read a small sample to detect corruption
                self._chunks_table.head(10).to_pandas()
                health_status["data_integrity"] = "ok"
            except Exception as e:
                health_status["status"] = "corrupted"
                health_status["data_integrity"] = f"corruption detected: {e}"
                health_status["recovery_suggestion"] = "Run optimize_tables() or recreate database"

        return health_status

    def get_connection_info(self) -> dict[str, Any]:
        """Get information about the database connection."""
        return {
            "provider": "lancedb",
            "database_path": str(self._db_path),
            "connected": self.is_connected,
            "index_type": self.index_type,
        }

    # ── Symbol/Edge CRUD Protocol Methods ────────��────────────────

    _SYMBOL_BATCH_SIZE = 500
    _EDGE_BATCH_SIZE = 500

    def _ensure_symbol_tables(self, conn: Any, state: dict[str, Any]) -> tuple[Any, Any]:
        """Ensure symbols and symbol_edges tables exist and return them.

        Returns (symbols_table, symbol_edges_table) — guaranteed non-None.
        """
        if self._symbols_table is None or self._symbol_edges_table is None:
            self._executor_create_schema(conn, state)
        assert self._symbols_table is not None
        assert self._symbol_edges_table is not None
        return self._symbols_table, self._symbol_edges_table

    def insert_symbols_batch(self, symbols: list[SymbolRow]) -> None:
        """Batch insert symbol rows with internal chunking."""
        if not symbols:
            return
        self._execute_in_db_thread_sync(self._executor_insert_symbols_batch, symbols)

    def _executor_insert_symbols_batch(self, conn: Any, state: dict[str, Any], symbols: list[SymbolRow]) -> None:
        if not symbols:
            return
        sym_tbl, _ = self._ensure_symbol_tables(conn, state)

        base_id = int(time.time() * 1_000_000)
        for i in range(0, len(symbols), self._SYMBOL_BATCH_SIZE):
            batch = symbols[i : i + self._SYMBOL_BATCH_SIZE]
            rows = []
            for j, s in enumerate(batch):
                rows.append(
                    {
                        "id": base_id + i + j,
                        "fqn": s["fqn"],
                        "name": s["name"],
                        "kind": s["kind"],
                        "language": s.get("language", ""),
                        "file_id": s["file_id"],
                        "file_path": s.get("file_path", ""),
                        "range_start": s["range_start"],
                        "range_end": s["range_end"],
                        "type_signature": s.get("type_signature") or "",
                        "parent_fqn": s.get("parent_fqn") or "",
                        "confidence": s.get("confidence", 1.0),
                        "lsp_server": s.get("lsp_server", ""),
                        "created_at": time.time(),
                    }
                )
            arrow_table = pa.Table.from_pylist(rows, schema=get_symbols_schema())
            sym_tbl.add(arrow_table)
        logger.debug(f"Inserted {len(symbols)} symbols into LanceDB")

    def delete_symbols_by_file(self, file_id: int) -> None:
        """Delete all symbols for a given file_id."""
        self._execute_in_db_thread_sync(self._executor_delete_symbols_by_file, file_id)

    def _executor_delete_symbols_by_file(self, conn: Any, state: dict[str, Any], file_id: int) -> None:
        sym_tbl, _ = self._ensure_symbol_tables(conn, state)
        sym_tbl.delete(f"file_id = {file_id}")

    def delete_edges_by_file(self, file_id: int) -> None:
        """Delete all edges referencing symbols belonging to this file."""
        self._execute_in_db_thread_sync(self._executor_delete_edges_by_file, file_id)

    def _executor_delete_edges_by_file(self, conn: Any, state: dict[str, Any], file_id: int) -> None:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        # Find all symbol IDs belonging to this file
        results = sym_tbl.search().where(f"file_id = {file_id}").select(["id"]).to_list()
        if not results:
            return
        sym_ids = {r["id"] for r in results}
        # Delete edges where from or to symbol is in this file
        for sid in sym_ids:
            edge_tbl.delete(f"from_symbol_id = {sid}")
            edge_tbl.delete(f"to_symbol_id = {sid}")

    def query_symbols_by_file(self, file_id: int) -> list[dict[str, Any]]:
        """Return all symbols for a given file_id."""
        return self._execute_in_db_thread_sync(self._executor_query_symbols_by_file, file_id)

    def _executor_query_symbols_by_file(self, conn: Any, state: dict[str, Any], file_id: int) -> list[dict[str, Any]]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        results = sym_tbl.search().where(f"file_id = {file_id}").to_list()
        return [dict(r) for r in results]

    def query_symbols_by_range(self, file_path: str, line: int) -> dict[str, Any] | None:
        """Return the innermost symbol containing the given line."""
        return self._execute_in_db_thread_sync(self._executor_query_symbols_by_range, file_path, line)

    def _executor_query_symbols_by_range(
        self, conn: Any, state: dict[str, Any], file_path: str, line: int
    ) -> dict[str, Any] | None:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        results = (
            sym_tbl.search()
            .where(f"file_path = '{_escape_lance_string(file_path)}' AND range_start <= {line} AND range_end >= {line}")
            .to_list()
        )
        if not results:
            return None
        # Sort by range span ascending (innermost = smallest span)
        results.sort(key=lambda r: r["range_end"] - r["range_start"])
        return dict(results[0])

    def query_symbols_by_range_overlap(self, file_path: str, min_line: int, max_line: int) -> list[dict[str, Any]]:
        """Return all symbols whose range overlaps [min_line, max_line]."""
        return self._execute_in_db_thread_sync(
            self._executor_query_symbols_by_range_overlap, file_path, min_line, max_line
        )

    def _executor_query_symbols_by_range_overlap(
        self,
        conn: Any,
        state: dict[str, Any],
        file_path: str,
        min_line: int,
        max_line: int,
    ) -> list[dict[str, Any]]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        results = (
            sym_tbl.search()
            .where(f"file_path = '{_escape_lance_string(file_path)}' AND range_start <= {max_line} AND range_end >= {min_line}")
            .to_list()
        )
        return [dict(r) for r in results]

    def query_symbol_fqns_by_file(self, file_id: int) -> dict[str, int]:
        """Return {fqn: symbol_id} mapping for all symbols in a file."""
        return self._execute_in_db_thread_sync(self._executor_query_symbol_fqns_by_file, file_id)

    def _executor_query_symbol_fqns_by_file(self, conn: Any, state: dict[str, Any], file_id: int) -> dict[str, int]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        results = sym_tbl.search().where(f"file_id = {file_id}").select(["id", "fqn"]).to_list()
        return {r["fqn"]: int(r["id"]) for r in results}

    def query_symbols_by_fqn_exists(self, fqn: str, file_path: str) -> bool:
        """Check whether a symbol with the given FQN and file_path exists."""
        return self._execute_in_db_thread_sync(self._executor_query_symbols_by_fqn_exists, fqn, file_path)

    def _executor_query_symbols_by_fqn_exists(self, conn: Any, state: dict[str, Any], fqn: str, file_path: str) -> bool:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        results = (
            sym_tbl.search().where(f"fqn = '{_escape_lance_string(fqn)}' AND file_path = '{_escape_lance_string(file_path)}'").select(["id"]).limit(1).to_list()
        )
        return len(results) > 0

    def insert_edges_batch(self, edges: list[EdgeRow]) -> None:
        """Batch insert edge rows."""
        if not edges:
            return
        self._execute_in_db_thread_sync(self._executor_insert_edges_batch, edges)

    def _executor_insert_edges_batch(self, conn: Any, state: dict[str, Any], edges: list[EdgeRow]) -> None:
        if not edges:
            return
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)

        base_id = int(time.time() * 1_000_000)
        for i in range(0, len(edges), self._EDGE_BATCH_SIZE):
            batch = edges[i : i + self._EDGE_BATCH_SIZE]
            rows = []
            for j, e in enumerate(batch):
                rows.append(
                    {
                        "id": base_id + i + j,
                        "from_symbol_id": e["from_symbol_id"],
                        "from_fqn": e.get("from_fqn", ""),
                        "from_file": e.get("from_file", ""),
                        "to_symbol_id": e["to_symbol_id"],
                        "to_fqn": e.get("to_fqn", ""),
                        "to_file": e.get("to_file", ""),
                        "edge_kind": e["edge_kind"],
                        "confidence": e.get("confidence", 1.0),
                        "lsp_server": e.get("lsp_server", ""),
                        "created_at": time.time(),
                    }
                )
            arrow_table = pa.Table.from_pylist(rows, schema=get_symbol_edges_schema())
            edge_tbl.add(arrow_table)
        logger.debug(f"Inserted {len(edges)} edges into LanceDB")

    def symbol_stats(self) -> dict[str, Any]:
        """Return counts for symbols and edges."""
        return self._execute_in_db_thread_sync(self._executor_symbol_stats)

    def _executor_symbol_stats(self, conn: Any, state: dict[str, Any]) -> dict[str, Any]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        sym_count = sym_tbl.count_rows()
        edge_count = edge_tbl.count_rows()

        languages: list[dict[str, Any]] = []
        if sym_count > 0:
            rows = sym_tbl.search().select(["language"]).limit(sym_count).to_list()
            counts: dict[str, int] = {}
            for row in rows:
                lang = row.get("language")
                if lang is None:
                    continue
                counts[lang] = counts.get(lang, 0) + 1
            languages = [
                {"language": lang, "count": count}
                for lang, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ]
        return {
            "symbol_count": sym_count,
            "edge_count": edge_count,
            "languages": languages,
        }

    # ── Graph Query Protocol Methods (Python BFS) ────────────────

    _BFS_FRONTIER_CAP = 10_000  # Cap BFS frontier to prevent OOM on dense graphs

    def graph_walk(
        self,
        seed_fqns: list[str],
        depth: int,
        directed: bool,
        edge_kind: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Walk connected symbols from seed FQNs using Python BFS."""
        if not seed_fqns:
            return [], []
        return self._execute_in_db_thread_sync(self._executor_graph_walk, seed_fqns, depth, directed, edge_kind, limit)

    def _executor_graph_walk(
        self,
        conn: Any,
        state: dict[str, Any],
        seed_fqns: list[str],
        depth: int,
        directed: bool,
        edge_kind: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not seed_fqns:
            return [], []
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)

        # BFS: frontier is a set of FQNs to expand, visited maps FQN → depth
        # (depth at which the FQN was first discovered). Matches DuckDB's
        # recursive-CTE contract which returns a ``depth`` column.
        fqn_depth: dict[str, int] = {}
        frontier: set[str] = set(seed_fqns)
        all_node_fqns: list[str] = []

        for d in range(depth + 1):
            new_frontier: set[str] = set()
            for fqn in frontier:
                if fqn in fqn_depth:
                    continue
                fqn_depth[fqn] = d
                all_node_fqns.append(fqn)
                if len(all_node_fqns) >= limit:
                    break

            if len(all_node_fqns) >= limit:
                break

            if d < depth:
                # Find neighbors via edges
                for fqn in list(frontier):
                    if fqn not in fqn_depth:
                        continue
                    neighbors = self._bfs_get_neighbors(edge_tbl, fqn, directed, edge_kind)
                    for n in neighbors:
                        if n not in fqn_depth:
                            new_frontier.add(n)
                    if len(new_frontier) > self._BFS_FRONTIER_CAP:
                        break

                frontier = new_frontier

        # Fetch full node data for discovered FQNs
        nodes: list[dict[str, Any]] = []
        for fqn in all_node_fqns[:limit]:
            results = sym_tbl.search().where(f"fqn = '{_escape_lance_string(fqn)}'").limit(1).to_list()
            if results:
                r = results[0]
                nodes.append(
                    {
                        "fqn": r["fqn"],
                        "name": r["name"],
                        "kind": r["kind"],
                        "file_path": r["file_path"],
                        "depth": fqn_depth[fqn],
                    }
                )


        if not nodes:
            return [], []

        # Fetch edges between discovered nodes
        node_fqn_set = {n["fqn"] for n in nodes}
        edges: list[dict[str, Any]] = []
        all_edges = edge_tbl.search().to_list()
        for e in all_edges:
            if e["from_fqn"] in node_fqn_set and e["to_fqn"] in node_fqn_set:
                if edge_kind and e["edge_kind"] != edge_kind:
                    continue
                edges.append(
                    {
                        "from_fqn": e["from_fqn"],
                        "to_fqn": e["to_fqn"],
                        "edge_kind": e["edge_kind"],
                        "from_file": e.get("from_file", ""),
                        "to_file": e.get("to_file", ""),
                    }
                )

        return nodes, edges

    def _bfs_get_neighbors(self, edge_tbl: Any, fqn: str, directed: bool, edge_kind: str | None) -> list[str]:
        """Get neighboring FQNs from edges (called within executor thread)."""
        neighbors: list[str] = []
        # Forward edges: from this FQN
        fwd = edge_tbl.search().where(f"from_fqn = '{_escape_lance_string(fqn)}'").select(["to_fqn", "edge_kind"]).to_list()
        for e in fwd:
            if edge_kind and e["edge_kind"] != edge_kind:
                continue
            neighbors.append(e["to_fqn"])

        if not directed:
            # Backward edges: to this FQN
            bwd = edge_tbl.search().where(f"to_fqn = '{_escape_lance_string(fqn)}'").select(["from_fqn", "edge_kind"]).to_list()
            for e in bwd:
                if edge_kind and e["edge_kind"] != edge_kind:
                    continue
                neighbors.append(e["from_fqn"])
        return neighbors

    def graph_reachability(self, scope: str) -> list[dict[str, Any]]:
        """Find unreachable symbols within a scope prefix."""
        return self._execute_in_db_thread_sync(self._executor_graph_reachability, scope)

    def _executor_graph_reachability(self, conn: Any, state: dict[str, Any], scope: str) -> list[dict[str, Any]]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)

        # Get all symbols in scope
        escaped = _escape_like_pattern(scope)
        all_results = sym_tbl.search().where(f"file_path LIKE '{escaped}%'").to_list()

        all_fqns = {r["fqn"] for r in all_results}
        if not all_fqns:
            return []

        # Find entry points: scope symbols with no inbound edges from scope
        symbols_with_inbound: set[str] = set()
        for fqn in all_fqns:
            inbound = edge_tbl.search().where(f"to_fqn = '{_escape_lance_string(fqn)}'").select(["from_fqn"]).to_list()
            for e in inbound:
                if e["from_fqn"] in all_fqns:
                    symbols_with_inbound.add(fqn)
                    break

        entry_points = all_fqns - symbols_with_inbound

        # BFS from entry points only
        reachable: set[str] = set()
        frontier = set(entry_points)
        while frontier:
            new_frontier: set[str] = set()
            for fqn in frontier:
                if fqn in reachable:
                    continue
                reachable.add(fqn)
                # Follow forward edges
                fwd = edge_tbl.search().where(f"from_fqn = '{_escape_lance_string(fqn)}'").select(["to_fqn"]).to_list()
                for e in fwd:
                    if e["to_fqn"] in all_fqns and e["to_fqn"] not in reachable:
                        new_frontier.add(e["to_fqn"])
            frontier = new_frontier

        # Return symbols not reachable
        unreachable = []
        for r in all_results:
            if r["fqn"] not in reachable:
                unreachable.append(
                    {
                        "fqn": r["fqn"],
                        "name": r["name"],
                        "kind": r["kind"],
                        "file_path": r["file_path"],
                    }
                )
        return unreachable

    def graph_boundary(self, scope: str, limit: int) -> list[dict[str, Any]]:
        """Find cross-boundary edges for a scope prefix."""
        return self._execute_in_db_thread_sync(self._executor_graph_boundary, scope, limit)

    def _executor_graph_boundary(
        self, conn: Any, state: dict[str, Any], scope: str, limit: int
    ) -> list[dict[str, Any]]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)

        all_edges = edge_tbl.search().to_list()

        # Collect crossing edges first; look up name/kind per FQN afterwards
        crossing: list[dict[str, Any]] = []
        needed_fqns: set[str] = set()
        for e in all_edges:
            from_in = e.get("from_file", "").startswith(scope)
            to_in = e.get("to_file", "").startswith(scope)
            if from_in != to_in:
                crossing.append(e)
                needed_fqns.add(e["from_fqn"])
                needed_fqns.add(e["to_fqn"])
                if len(crossing) >= limit:
                    break

        # Batch-fetch symbol name/kind for all endpoints (single scan)
        name_kind: dict[str, tuple[str, str]] = {}
        if needed_fqns:
            sym_rows = sym_tbl.search().select(["fqn", "name", "kind"]).to_list()
            for row in sym_rows:
                fqn = row.get("fqn")
                if fqn in needed_fqns:
                    name_kind[fqn] = (row.get("name", ""), row.get("kind", ""))

        results: list[dict[str, Any]] = []
        for e in crossing:
            from_fqn = e["from_fqn"]
            to_fqn = e["to_fqn"]
            from_name, from_kind = name_kind.get(from_fqn, ("", ""))
            to_name, to_kind = name_kind.get(to_fqn, ("", ""))
            results.append(
                {
                    "from_fqn": from_fqn,
                    "from_name": from_name,
                    "from_kind": from_kind,
                    "from_file": e.get("from_file", ""),
                    "to_fqn": to_fqn,
                    "to_name": to_name,
                    "to_kind": to_kind,
                    "to_file": e.get("to_file", ""),
                    "edge_kind": e["edge_kind"],
                }
            )
        return results

    def graph_overview(self, scope: str | None, limit: int) -> list[dict[str, Any]]:
        """Get top symbols by edge connectivity."""
        return self._execute_in_db_thread_sync(self._executor_graph_overview, scope, limit)

    def _executor_graph_overview(
        self, conn: Any, state: dict[str, Any], scope: str | None, limit: int
    ) -> list[dict[str, Any]]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)

        # Count edges per symbol
        all_edges = edge_tbl.search().to_list()

        from collections import Counter

        edge_counts: Counter[str] = Counter()
        for e in all_edges:
            edge_counts[e["from_fqn"]] += 1
            edge_counts[e["to_fqn"]] += 1

        # Get symbol details for top FQNs
        results: list[dict[str, Any]] = []
        for fqn, count in edge_counts.most_common():
            if len(results) >= limit:
                break
            sym_results = sym_tbl.search().where(f"fqn = '{_escape_lance_string(fqn)}'").limit(1).to_list()
            if sym_results:
                s = sym_results[0]
                if scope and not s.get("file_path", "").startswith(scope):
                    continue
                results.append(
                    {
                        "fqn": s["fqn"],
                        "name": s["name"],
                        "kind": s["kind"],
                        "file_path": s["file_path"],
                        "total_edges": count,
                    }
                )
        return results

    def graph_overview_breakdown(self, fqns: list[str]) -> dict[str, dict[str, int]]:
        """Per-edge-kind counts for the given FQNs."""
        if not fqns:
            return {}
        return self._execute_in_db_thread_sync(self._executor_graph_overview_breakdown, fqns)

    def _executor_graph_overview_breakdown(
        self, conn: Any, state: dict[str, Any], fqns: list[str]
    ) -> dict[str, dict[str, int]]:
        if not fqns:
            return {}
        _sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)

        fqn_set = set(fqns)
        result: dict[str, dict[str, int]] = {}
        all_edges = edge_tbl.search().to_list()

        for e in all_edges:
            edge_kind = e["edge_kind"]
            from_fqn = e["from_fqn"]
            to_fqn = e["to_fqn"]
            # Mirrors DuckDB: count each edge once per matching endpoint
            if from_fqn in fqn_set:
                result.setdefault(from_fqn, {})
                result[from_fqn][edge_kind] = result[from_fqn].get(edge_kind, 0) + 1
            if to_fqn in fqn_set and to_fqn != from_fqn:
                result.setdefault(to_fqn, {})
                result[to_fqn][edge_kind] = result[to_fqn].get(edge_kind, 0) + 1
        return result

    def symbol_overlap(self, chunks: list[dict[str, Any]]) -> list[str]:
        """Resolve seed chunks to symbol FQNs via range overlap."""
        if not chunks:
            return []
        return self._execute_in_db_thread_sync(self._executor_symbol_overlap, chunks)

    def _executor_symbol_overlap(self, conn: Any, state: dict[str, Any], chunks: list[dict[str, Any]]) -> list[str]:
        if not chunks:
            return []
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)

        fqns: set[str] = set()
        for chunk in chunks:
            fp = chunk["file_path"]
            start = chunk["start_line"]
            end = chunk["end_line"]
            results = (
                sym_tbl.search()
                .where(f"file_path = '{_escape_lance_string(fp)}' AND range_start <= {end} AND range_end >= {start}")
                .select(["fqn"])
                .to_list()
            )
            for r in results:
                fqns.add(r["fqn"])
        return list(fqns)

    def chunk_resolution(self, fqns: list[str]) -> list[dict[str, Any]]:
        """Resolve symbol FQNs to chunks via file_id + range overlap."""
        if not fqns:
            return []
        return self._execute_in_db_thread_sync(self._executor_chunk_resolution, fqns)

    def _executor_chunk_resolution(self, conn: Any, state: dict[str, Any], fqns: list[str]) -> list[dict[str, Any]]:
        if not fqns:
            return []
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)

        results: list[dict[str, Any]] = []
        seen_chunk_ids: set[int] = set()
        for fqn in fqns:
            # Find the symbol
            sym_results = sym_tbl.search().where(f"fqn = '{_escape_lance_string(fqn)}'").limit(1).to_list()
            if not sym_results:
                continue
            sym = sym_results[0]
            file_id = sym["file_id"]
            range_start = sym["range_start"]
            range_end = sym["range_end"]

            # Find file path from files table
            file_results = self._files_table.search().where(f"id = {file_id}").limit(1).to_list()
            if not file_results:
                continue
            file_path = file_results[0]["path"]

            # Find chunks that overlap this symbol's range
            chunk_results = (
                self._chunks_table.search()
                .where(f"file_id = {file_id} AND start_line <= {range_end} AND end_line >= {range_start}")
                .to_list()
            )
            for c in chunk_results:
                cid = c["id"]
                if cid not in seen_chunk_ids:
                    seen_chunk_ids.add(cid)
                    results.append(
                        {
                            "chunk_id": cid,
                            "file_path": file_path,
                            "content": c.get("content", ""),
                            "start_line": c["start_line"],
                            "end_line": c["end_line"],
                        }
                    )
        return results

    # ── Symbol Read Query Methods ────────────────────────────────

    def query_symbols_by_scope(self, scope: str) -> list[dict[str, Any]]:
        """Return symbols matching scope prefix, grouped by name."""
        return self._execute_in_db_thread_sync(self._executor_query_symbols_by_scope, scope)

    def _executor_query_symbols_by_scope(self, conn: Any, state: dict[str, Any], scope: str) -> list[dict[str, Any]]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        escaped = _escape_like_pattern(scope)
        results = sym_tbl.search().where(f"file_path LIKE '{escaped}%'").to_list()
        return [dict(r) for r in results]

    def query_test_symbols(self, scope: str | None) -> list[dict[str, Any]]:
        """Return test function symbols (kind='Function', name LIKE 'test_%')."""
        return self._execute_in_db_thread_sync(self._executor_query_test_symbols, scope)

    def _executor_query_test_symbols(self, conn: Any, state: dict[str, Any], scope: str | None) -> list[dict[str, Any]]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        where = "kind = 'Function' AND name LIKE 'test_%'"
        if scope:
            escaped = _escape_like_pattern(scope)
            where += f" AND file_path LIKE '{escaped}%'"
        results = sym_tbl.search().where(where).to_list()
        return [dict(r) for r in results]

    def query_symbol_type_signatures(self, fqns: list[str]) -> dict[str, str | None]:
        """Return FQN → type_signature mapping for a batch of FQNs."""
        if not fqns:
            return {}
        return self._execute_in_db_thread_sync(self._executor_query_symbol_type_signatures, fqns)

    def _executor_query_symbol_type_signatures(
        self, conn: Any, state: dict[str, Any], fqns: list[str]
    ) -> dict[str, str | None]:
        if not fqns:
            return {}
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        result: dict[str, str | None] = {}
        for fqn in fqns:
            rows = sym_tbl.search().where(f"fqn = '{_escape_lance_string(fqn)}'").select(["fqn", "type_signature"]).limit(1).to_list()
            if rows:
                ts = rows[0].get("type_signature", "")
                result[fqn] = ts if ts else None
        return result

    def query_distinct_fqns_by_file_path(self, file_path: str) -> list[str]:
        """Return distinct FQNs for symbols in a given file."""
        return self._execute_in_db_thread_sync(self._executor_query_distinct_fqns_by_file_path, file_path)

    def _executor_query_distinct_fqns_by_file_path(self, conn: Any, state: dict[str, Any], file_path: str) -> list[str]:
        sym_tbl, edge_tbl = self._ensure_symbol_tables(conn, state)
        results = sym_tbl.search().where(f"file_path = '{_escape_lance_string(file_path)}'").select(["fqn"]).to_list()
        return list({r["fqn"] for r in results})

    def search_symbols(
        self,
        query: str,
        path: str | None,
        type_filter: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Substring search on symbol name/fqn with path and type filters."""
        return self._execute_in_db_thread_sync(self._executor_search_symbols, query, path, type_filter, limit, offset)

    def _executor_search_symbols(
        self,
        conn: Any,
        state: dict[str, Any],
        query: str,
        path: str | None,
        type_filter: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        sym_tbl, _edge_tbl = self._ensure_symbol_tables(conn, state)
        conditions: list[str] = []
        if query:
            q = _escape_lance_string(query)
            conditions.append(f"(name LIKE '%{q}%' OR fqn LIKE '%{q}%')")
        if path:
            p = _escape_lance_string(path)
            conditions.append(f"file_path LIKE '{p}%'")
        if type_filter:
            t = _escape_lance_string(type_filter)
            conditions.append(f"type_signature LIKE '%{t}%'")

        builder = sym_tbl.search()
        if conditions:
            builder = builder.where(" AND ".join(conditions))
        all_rows = builder.to_list()

        all_rows.sort(key=lambda r: r.get("name", ""))
        total = len(all_rows)
        page = all_rows[offset : offset + limit]
        return [dict(r) for r in page], total

    def filter_chunks_by_symbol_type_signature(
        self,
        chunks: list[dict[str, Any]],
        type_filter: str,
    ) -> list[dict[str, Any]]:
        """Return chunks overlapping a symbol whose type_signature matches."""
        if not chunks:
            return []
        return self._execute_in_db_thread_sync(
            self._executor_filter_chunks_by_symbol_type_signature, chunks, type_filter
        )

    def _executor_filter_chunks_by_symbol_type_signature(
        self,
        conn: Any,
        state: dict[str, Any],
        chunks: list[dict[str, Any]],
        type_filter: str,
    ) -> list[dict[str, Any]]:
        if not chunks:
            return []
        sym_tbl, _edge_tbl = self._ensure_symbol_tables(conn, state)
        t = _escape_lance_string(type_filter)
        matching_symbols = sym_tbl.search().where(f"type_signature LIKE '%{t}%'").to_list()

        # Index symbols by file_path for efficient overlap check
        by_file: dict[str, list[tuple[int, int]]] = {}
        for s in matching_symbols:
            fp = s.get("file_path")
            if fp is None:
                continue
            by_file.setdefault(fp, []).append((int(s.get("range_start", 0)), int(s.get("range_end", 0))))

        kept: list[dict[str, Any]] = []
        for chunk in chunks:
            ranges = by_file.get(chunk["file_path"], [])
            if any(rs <= chunk["end_line"] and re >= chunk["start_line"] for rs, re in ranges):
                kept.append(chunk)
        return kept
