"""Type signature / hover collection tests — _collect_type_signatures and populate_file hover flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from chunkhound.lsp.types import HoverResult, LSPCapability, SymbolInfo
from chunkhound.services.lsp_population import LSPPopulationService
from tests.lsp.conftest import (
    _insert_file,
    _make_provider,
    _sample_symbols,
)

pytestmark = pytest.mark.unit


class TestCollectTypeSignatures:
    """
    Feature: Hover per symbol collects type_signature data

    As the population service
    I want to call hover for each symbol and collect type signatures
    So that the symbols table contains type information for downstream tools
    """

    @pytest.mark.asyncio
    async def test_hover_called_per_symbol_returns_mapping(self, tmp_path: Path) -> None:
        """
        Scenario: Collect type signatures from hover for all symbols (including nested)
        Given a file with 3 symbols: Greeter (class), greet (method), main (function)
        When _collect_type_signatures is called
        Then hover is called 3 times (once per symbol including nested child)
             and the returned dict maps (line, char) → hover contents
        """
        symbols = _sample_symbols()  # Greeter(children=[greet]), main

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER})
        # Hover returns different content for each symbol position
        hover_responses = {
            (1, 0): HoverResult(contents="class Greeter"),
            (5, 4): HoverResult(contents="(method) greet() -> None"),
            (10, 0): HoverResult(contents="(function) main() -> None"),
        }
        async def hover_side_effect(_uri: str, line: int, char: int) -> HoverResult | None:
            return hover_responses.get((line, char))
        client.hover = AsyncMock(side_effect=hover_side_effect)

        provider = _make_provider(tmp_path)
        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        result = await service._collect_type_signatures(
            client, "file:///test.py", symbols,
        )

        # Hover called 3 times — Greeter, greet (nested child), main
        assert client.hover.call_count == 3

        # Mapping contains all 3 symbols
        assert result == {
            (1, 0): "class Greeter",
            (5, 4): "(method) greet() -> None",
            (10, 0): "(function) main() -> None",
        }

    @pytest.mark.asyncio
    async def test_hover_failure_on_one_symbol_does_not_block_others(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: One symbol's hover raises, others still collected
        Given 3 symbols where hover raises Exception for the 2nd (greet)
        When _collect_type_signatures is called
        Then the 1st and 3rd symbols still have type_signatures
             and the 2nd symbol is absent from the mapping
        """
        symbols = _sample_symbols()  # Greeter(children=[greet]), main

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER})

        call_count = 0
        async def hover_side_effect(_uri: str, line: int, char: int) -> HoverResult | None:
            nonlocal call_count
            call_count += 1
            if line == 5 and char == 4:  # greet — simulate failure
                raise RuntimeError("LSP server crashed mid-hover")
            if line == 1:
                return HoverResult(contents="class Greeter")
            if line == 10:
                return HoverResult(contents="(function) main() -> None")
            return None
        client.hover = AsyncMock(side_effect=hover_side_effect)

        provider = _make_provider(tmp_path)
        pool = AsyncMock()

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        result = await service._collect_type_signatures(
            client, "file:///test.py", symbols,
        )

        # All 3 hover calls attempted
        assert call_count == 3

        # Only 2 results — greet's failure was isolated
        assert result == {
            (1, 0): "class Greeter",
            (10, 0): "(function) main() -> None",
        }
        assert (5, 4) not in result

    @pytest.mark.asyncio
    async def test_no_hover_capability_returns_empty_dict(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: Server doesn't advertise hover capability
        Given a client whose capabilities do NOT include HOVER
        When _collect_type_signatures is called
        Then an empty dict is returned and hover is never called
        """
        symbols = _sample_symbols()

        client = AsyncMock()
        # Only DOCUMENT_SYMBOL, no HOVER
        client.capabilities = frozenset({LSPCapability.DOCUMENT_SYMBOL})
        client.hover = AsyncMock()

        provider = _make_provider(tmp_path)
        pool = AsyncMock()

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        result = await service._collect_type_signatures(
            client, "file:///test.py", symbols,
        )

        assert result == {}
        client.hover.assert_not_called()


class TestPopulateFileTypeSignature:
    """
    Feature: populate_file stores type_signature in the symbols table

    As the indexing pipeline
    I want type_signature populated from hover data during populate_file
    So that downstream graph tools have type information without live LSP queries
    """

    @pytest.mark.asyncio
    async def test_type_signature_stored_for_symbols_with_hover_data(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: populate_file with hover data writes type_signature to DB
        Given a file with 3 symbols where hover returns data for 2 of them
        When populate_file is called
        Then type_signature is populated for the 2 symbols with hover data
             and NULL for the symbol where hover returned None
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/greeter.py")

        symbols = _sample_symbols()  # Greeter(children=[greet]), main

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()

        # Hover returns data for Greeter and main, None for greet
        async def hover_side_effect(_uri: str, line: int, char: int) -> HoverResult | None:
            if line == 1 and char == 0:
                return HoverResult(contents="class Greeter")
            if line == 10 and char == 0:
                return HoverResult(contents="(function) main() -> None")
            return None  # greet at (5, 4) — no hover data
        client.hover = AsyncMock(side_effect=hover_side_effect)

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        src_file = tmp_path / "src" / "greeter.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(
            file_path=Path("src/greeter.py"), file_id=1, language="python",
        )

        rows = provider.execute_query(
            "SELECT fqn, type_signature FROM symbols ORDER BY range_start"
        )

        assert len(rows) == 3

        # Greeter — has hover data
        assert rows[0]["fqn"] == "Greeter"
        assert rows[0]["type_signature"] == "class Greeter"

        # greet — no hover data
        assert rows[1]["fqn"] == "Greeter::greet"
        assert rows[1]["type_signature"] is None

        # main — has hover data
        assert rows[2]["fqn"] == "main"
        assert rows[2]["type_signature"] == "(function) main() -> None"


class TestAdversarialHover:
    """Adversarial stress tests for hover / type_signature population (ch-nvc)."""

    @pytest.mark.asyncio
    async def test_empty_symbols_no_hover_calls(self, tmp_path: Path) -> None:
        """
        Pattern: Empty
        Hypothesis: _collect_type_signatures with [] symbols → empty dict, no hover calls.
        """
        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER})
        client.hover = AsyncMock()

        provider = _make_provider(tmp_path)
        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        result = await service._collect_type_signatures(client, "file:///e.py", [])

        assert result == {}
        client.hover.assert_not_called()

    @pytest.mark.asyncio
    async def test_singular_symbol_gets_type_signature(self, tmp_path: Path) -> None:
        """
        Pattern: Singular
        Hypothesis: Single symbol with hover → one-entry dict, stored in DB correctly.
        """
        single = SymbolInfo(
            name="VERSION", kind=13, range_start_line=1, range_start_char=0,
            range_end_line=1, range_end_char=20, children=[],
        )

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=[single])
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(contents="(variable) VERSION: str"))

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/ver.py")

        src = tmp_path / "src" / "ver.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text('VERSION = "1.0"\n')

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/ver.py"), 1, "python")

        rows = provider.execute_query("SELECT name, type_signature FROM symbols")
        assert len(rows) == 1
        assert rows[0]["name"] == "VERSION"
        assert rows[0]["type_signature"] == "(variable) VERSION: str"

    @pytest.mark.asyncio
    async def test_unicode_in_hover_contents_stored_faithfully(self, tmp_path: Path) -> None:
        """
        Pattern: Encoding boundaries
        Hypothesis: Hover contents with unicode (CJK, emoji, math symbols) stored as-is.
        """
        sym = SymbolInfo(
            name="grüße", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )
        # Multi-byte content: CJK + emoji + mathematical notation
        unicode_hover = "(function) grüße() -> Résultat[données, 错误] # 🎯 ∀x∈ℝ"

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=[sym])
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(contents=unicode_hover))

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/u.py")

        src = tmp_path / "src" / "u.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("def grüße(): pass\n")

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )
        await service.populate_file(Path("src/u.py"), 1, "python")

        rows = provider.execute_query("SELECT type_signature FROM symbols")
        assert len(rows) == 1
        assert rows[0]["type_signature"] == unicode_hover

    @pytest.mark.asyncio
    async def test_second_run_preserves_type_signatures(self, tmp_path: Path) -> None:
        """
        Pattern: The "second run"
        Hypothesis: populate_file called twice → same rows with same type_signatures.
        Delete-before-insert must not leave stale type_signature data.
        """
        symbols = _sample_symbols()

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(contents="type info"))

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/r.py")

        src = tmp_path / "src" / "r.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/r.py"), 1, "python")
        await service.populate_file(Path("src/r.py"), 1, "python")

        rows = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols")
        assert rows[0]["cnt"] == 3  # Not 6 — idempotent

        sigs = provider.execute_query(
            "SELECT type_signature FROM symbols WHERE type_signature IS NOT NULL"
        )
        assert len(sigs) == 3  # All 3 have type_signature from hover
        assert all(s["type_signature"] == "type info" for s in sigs)

    @pytest.mark.asyncio
    async def test_all_hover_fails_symbols_still_inserted_with_null_type_sig(
        self, tmp_path: Path,
    ) -> None:
        """
        Pattern: Dependency treachery — server degrades, all hover fails
        Hypothesis: All hover calls raise → symbols still inserted, all type_signature = NULL.
        The file's symbols must not be lost because hover failed.
        """
        symbols = _sample_symbols()

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(side_effect=ConnectionError("server died"))

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/dead.py")

        src = tmp_path / "src" / "dead.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/dead.py"), 1, "python")

        rows = provider.execute_query("SELECT name, type_signature FROM symbols ORDER BY range_start")
        assert len(rows) == 3  # All symbols inserted despite hover failure
        assert all(r["type_signature"] is None for r in rows)
