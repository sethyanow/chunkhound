"""Integration tests for LanceDB ``get_stats`` correctness and avoidance of
pandas materialization.

Regression coverage for ch-3zc: ``_executor_get_stats`` used ``.to_pandas()``
to count rows, which materialized hundreds of MB on populated tables (80K
chunks × 1024-dim embeddings) and exceeded the 30s
``CHUNKHOUND_DB_EXECUTE_TIMEOUT`` budget. The outer executor timed the
future out, and ``stats.py`` silently swallowed the ``TimeoutError``, so
users saw ``{files: 0, chunks: 0}`` instead of real counts.

Tests:
- Test A (``test_get_stats_counts_without_pandas_materialization``): the
  headline regression. Forbids ``.to_pandas()`` on both files and chunks
  tables; asserts counts are still returned correctly. Deterministic — if
  the implementation calls ``.to_pandas()``, the files path returns 0 and
  the assertion fails.
- Test B (``test_get_stats_empty_db_returns_zeros``): regression guard for
  the empty-DB path.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_get_stats_counts_without_pandas_materialization(lancedb_provider):
    """``get_stats`` must not materialize tables via ``.to_pandas()`` (ch-3zc).

    Populates a minimal fixture (1 file, 1 chunk), then replaces
    ``to_pandas`` on both tables with a function that raises. If the
    implementation calls ``to_pandas()``, the files-table branch returns 0
    (no count_rows fallback there), and the assertion fails. The chunks
    branch has an inner ``count_rows()`` fallback on exception, so it still
    returns the right count — but that's the old code patching over its
    own bug; files exposes the problem cleanly.

    The correct fix (ch-3zc) replaces ``.to_pandas()`` with ``count_rows()``
    directly, so neither table ever gets materialized and both counts come
    back right.
    """
    from chunkhound.core.models import Chunk, File
    from chunkhound.core.types.common import (
        ChunkType,
        FilePath,
        Language,
        LineNumber,
        Timestamp,
    )

    file_record = File(
        path=FilePath("test.py"),
        mtime=Timestamp(1.0),
        language=Language.PYTHON,
        size_bytes=10,
    )
    file_id = lancedb_provider.insert_file(file_record)

    chunk = Chunk(
        file_id=file_id,
        code="def f(): pass",
        start_line=LineNumber(1),
        end_line=LineNumber(1),
        chunk_type=ChunkType.FUNCTION,
        language=Language.PYTHON,
        symbol="f",
    )
    lancedb_provider.insert_chunks_batch([chunk])

    def forbidden_to_pandas(*_args, **_kwargs):
        raise RuntimeError(
            "_executor_get_stats must not call .to_pandas() — see ch-3zc"
        )

    # Shadow the bound ``to_pandas`` on each table object. The instances
    # are live tables returned from the LanceDB connection — assigning an
    # attribute on the Python wrapper intercepts the call.
    lancedb_provider._files_table.to_pandas = forbidden_to_pandas
    lancedb_provider._chunks_table.to_pandas = forbidden_to_pandas

    stats = lancedb_provider.get_stats()

    assert stats["files"] >= 1, (
        f"expected files >= 1, got {stats['files']} — current code likely "
        f"called .to_pandas() and fell through to 0"
    )
    assert stats["chunks"] >= 1, (
        f"expected chunks >= 1, got {stats['chunks']}"
    )


def test_get_stats_empty_db_returns_zeros(lancedb_provider):
    """Empty DB returns ``{files: 0, chunks: 0}`` without raising.

    Guards against a refactor that assumes tables are populated. The fix
    for ch-3zc changes truthy checks to ``is not None`` and switches to
    ``count_rows()``; either change could conceivably break the empty path
    if done carelessly. This test codifies the invariant.
    """
    stats = lancedb_provider.get_stats()
    assert stats["files"] == 0, f"empty DB: expected files=0, got {stats['files']}"
    assert stats["chunks"] == 0, (
        f"empty DB: expected chunks=0, got {stats['chunks']}"
    )


# --- Adversarial stress tests (ch-3zc) -------------------------------------
#
# Each test below represents an adversarial hypothesis about the fix.
# They cover the structural patterns that are meaningful for a pure-read
# stats function: partial-state (disconnected/degraded), state transitions
# (second-run / idempotence), and graceful degradation (one table fails).
#
# Patterns skipped with reason:
# - Self-referential, disconnected, dense: no graph/link semantics here.
# - Encoding boundaries: count_rows() does not read row content.
# - Redundant: counts are counts regardless of duplication.


def test_get_stats_partial_disconnect_files_table_none(lancedb_provider):
    """One table is None, the other is live — adversarial: per-table independence.

    The ``is not None`` checks (ch-3zc) should handle each table independently.
    If the guards were ever collapsed into a single check or shared state, a
    partially-disconnected provider would silently return wrong counts.
    """
    from chunkhound.core.models import Chunk, File
    from chunkhound.core.types.common import (
        ChunkType,
        FilePath,
        Language,
        LineNumber,
        Timestamp,
    )

    file_record = File(
        path=FilePath("test.py"),
        mtime=Timestamp(1.0),
        language=Language.PYTHON,
        size_bytes=10,
    )
    file_id = lancedb_provider.insert_file(file_record)
    lancedb_provider.insert_chunks_batch(
        [
            Chunk(
                file_id=file_id,
                code="def f(): pass",
                start_line=LineNumber(1),
                end_line=LineNumber(1),
                chunk_type=ChunkType.FUNCTION,
                language=Language.PYTHON,
                symbol="f",
            )
        ]
    )

    # Simulate a partial-disconnect: files_table reference dropped, chunks intact.
    lancedb_provider._files_table = None

    stats = lancedb_provider.get_stats()

    assert stats["files"] == 0, (
        f"files_table=None should yield files=0, got {stats['files']}"
    )
    assert stats["chunks"] >= 1, (
        f"chunks_table should still count independently, got {stats['chunks']}"
    )


def test_get_stats_is_idempotent(lancedb_provider):
    """Second run yields the same result. get_stats must not mutate state.

    A pure read should be trivially idempotent, but a careless refactor
    could memoize, mutate, or close resources. This codifies the invariant.
    """
    from chunkhound.core.models import Chunk, File
    from chunkhound.core.types.common import (
        ChunkType,
        FilePath,
        Language,
        LineNumber,
        Timestamp,
    )

    file_record = File(
        path=FilePath("test.py"),
        mtime=Timestamp(1.0),
        language=Language.PYTHON,
        size_bytes=10,
    )
    file_id = lancedb_provider.insert_file(file_record)
    lancedb_provider.insert_chunks_batch(
        [
            Chunk(
                file_id=file_id,
                code="def f(): pass",
                start_line=LineNumber(1),
                end_line=LineNumber(1),
                chunk_type=ChunkType.FUNCTION,
                language=Language.PYTHON,
                symbol="f",
            )
        ]
    )

    first = lancedb_provider.get_stats()
    second = lancedb_provider.get_stats()
    third = lancedb_provider.get_stats()

    assert first == second == third, (
        f"get_stats not idempotent: {first} vs {second} vs {third}"
    )


def test_get_stats_degrades_gracefully_when_count_rows_raises(lancedb_provider):
    """One table's count_rows raises — other still counts. ch-3zc per-table try/except.

    The failure catalog (Input Hostility entry in the skeleton) notes that
    ``count_rows()`` could raise on fragment-level read errors. The
    per-table try/except with ``logger.warning`` must degrade gracefully:
    the failing table contributes 0, the healthy table contributes its count.
    """
    from chunkhound.core.models import Chunk, File
    from chunkhound.core.types.common import (
        ChunkType,
        FilePath,
        Language,
        LineNumber,
        Timestamp,
    )

    file_record = File(
        path=FilePath("test.py"),
        mtime=Timestamp(1.0),
        language=Language.PYTHON,
        size_bytes=10,
    )
    file_id = lancedb_provider.insert_file(file_record)
    lancedb_provider.insert_chunks_batch(
        [
            Chunk(
                file_id=file_id,
                code="def f(): pass",
                start_line=LineNumber(1),
                end_line=LineNumber(1),
                chunk_type=ChunkType.FUNCTION,
                language=Language.PYTHON,
                symbol="f",
            )
        ]
    )

    def broken_count(*_args, **_kwargs):
        raise RuntimeError("simulated fragment read error")

    lancedb_provider._chunks_table.count_rows = broken_count

    stats = lancedb_provider.get_stats()

    assert stats["files"] >= 1, (
        f"files_table should count independently, got {stats['files']}"
    )
    assert stats["chunks"] == 0, (
        f"chunks count failure should yield 0 (graceful), got {stats['chunks']}"
    )
