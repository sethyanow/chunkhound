"""Regression: Python 3.13 Path.rglob() returns map (no .close()).

_polling_monitor line 604 calls rglob_gen.close() in a finally block.
On Python 3.13, rglob returns a map object which lacks .close(),
crashing the polling loop on every iteration.
"""

import asyncio
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from chunkhound.core.config.config import Config
from chunkhound.services.realtime_indexing_service import RealtimeIndexingService

pytestmark = pytest.mark.unit


@pytest.fixture
def polling_service(tmp_path: Path) -> RealtimeIndexingService:
    """Minimal RealtimeIndexingService configured for polling."""
    (tmp_path / "sample.py").write_text("x = 1\n")

    fake_args = SimpleNamespace(path=tmp_path)
    config = Config(
        args=fake_args,
        database={"path": str(tmp_path / "db"), "provider": "duckdb"},
        indexing={"include": ["*.py"], "exclude": []},
    )

    services = SimpleNamespace(
        provider=MagicMock(),
        coordinator=SimpleNamespace(
            process_file=AsyncMock(),
            process_directory=AsyncMock(),
        ),
    )
    return RealtimeIndexingService(services, config, force_polling=True)


@pytest.mark.skipif(
    sys.version_info < (3, 13),
    reason="Bug only manifests on Python 3.13+ where rglob returns map",
)
class TestPollingRglobCompat:
    @pytest.mark.asyncio
    async def test_polling_does_not_reprocess_known_files(
        self, polling_service: RealtimeIndexingService, tmp_path: Path,
    ) -> None:
        """known_files must persist across iterations — no duplicate add_file calls.

        The bug: rglob_gen.close() raises AttributeError on 3.13 (map has no
        .close()). The except on line 623 catches it, but known_files = current_files
        on line 613 is skipped. Every iteration treats all files as new.
        """
        add_calls: list[Path] = []

        async def spy_add(file_path: Path, priority: str = "change") -> None:
            add_calls.append(file_path)

        polling_service.add_file = spy_add  # type: ignore[assignment]

        task = asyncio.create_task(polling_service._polling_monitor(tmp_path))
        # Two iterations: fast-poll is 0.5s, error-retry is 5s.
        # If bug exists, first iteration crashes → 5s sleep → second iteration
        # also crashes. With 1.5s we catch at least two fast-poll iterations
        # if the code is fixed, or one crash + start of second if broken.
        # Use 6.5s to ensure both paths get two iterations.
        await asyncio.sleep(6.5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        # Each file should appear at most once — found on first poll, then
        # recognized as known (unchanged mtime) on subsequent polls.
        unique = set(add_calls)
        assert len(add_calls) == len(unique), (
            f"Files re-added: {len(add_calls)} calls for {len(unique)} files — "
            f"known_files not persisting (rglob_gen.close() crash?)"
        )

    @pytest.mark.asyncio
    async def test_rglob_close_attribute_error(self, tmp_path: Path) -> None:
        """Prove the precondition: Path.rglob on 3.13 lacks .close()."""
        gen = tmp_path.rglob("*")
        assert type(gen).__name__ == "map", f"Expected map, got {type(gen)}"
        assert not hasattr(gen, "close"), "rglob gained .close() — bug may be fixed upstream"
