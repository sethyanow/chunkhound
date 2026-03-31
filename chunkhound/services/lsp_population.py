"""LSP Population Service — writes documentSymbol results to the symbols table.

Background service that runs after tree-sitter chunking to populate the
symbols table with LSP-derived structure data.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from chunkhound.lsp.constants import symbol_kind_name
from chunkhound.lsp.types import LSPCapability, LSPError, SymbolInfo

if TYPE_CHECKING:
    from chunkhound.lsp.client import LSPClient, LSPClientPool
    from chunkhound.providers.database.duckdb_provider import DuckDBProvider

logger = logging.getLogger(__name__)


class LSPPopulationService:
    """Populates the DuckDB symbols table from LSP documentSymbol results."""

    def __init__(
        self,
        pool: LSPClientPool,
        provider: DuckDBProvider,
        workspace_root: Path,
    ) -> None:
        self._pool = pool
        self._provider = provider
        self._workspace_root = workspace_root

    async def populate_file(
        self,
        file_path: Path,
        file_id: int,
        language: str,
    ) -> None:
        """Run documentSymbol on a single file and write rows to the symbols table.

        Follows LSP protocol: didOpen → documentSymbol → didClose.
        Skips gracefully if no LSP server is configured for the language.
        """
        try:
            client = await self._pool.get(language, str(self._workspace_root))
        except LSPError:
            logger.debug("No LSP server for language=%s, skipping %s", language, file_path)
            return

        abs_path = self._workspace_root / file_path
        uri = abs_path.as_uri()

        try:
            content = abs_path.read_text()
        except (OSError, UnicodeDecodeError):
            logger.debug("Cannot read %s, skipping LSP population", file_path)
            return

        try:
            await client.notify_did_open(uri, content, language)
            symbols = await client.document_symbols(uri)
            type_signatures = await self._collect_type_signatures(client, uri, symbols) if symbols else {}
        finally:
            await client.notify_did_close(uri)

        if not symbols:
            return

        # Delete existing symbols for this file (idempotent repopulation)
        await self.delete_file_symbols(file_id)

        # Flatten nested symbols into rows
        rows = self._flatten_symbols(
            symbols=symbols,
            file_id=file_id,
            file_path=str(file_path),
            language=language,
            lsp_server=self._server_name(language),
            parent_fqn=None,
            type_signatures=type_signatures,
        )

        if rows:
            self._batch_insert(rows)

    async def _collect_type_signatures(
        self,
        client: LSPClient,
        uri: str,
        symbols: list[SymbolInfo],
    ) -> dict[tuple[int, int], str]:
        """Call hover per symbol to collect type_signature data.

        Returns mapping of (range_start_line, range_start_char) → hover contents.
        Skips symbols where hover fails or returns None.
        Checks hover capability once at entry — returns empty dict if unsupported.
        """
        if LSPCapability.HOVER not in client.capabilities:
            return {}

        result: dict[tuple[int, int], str] = {}
        await self._hover_recursive(client, uri, symbols, result)
        return result

    async def _hover_recursive(
        self,
        client: LSPClient,
        uri: str,
        symbols: list[SymbolInfo],
        result: dict[tuple[int, int], str],
    ) -> None:
        """Recursively walk symbols and call hover for each."""
        for sym in symbols:
            try:
                hover = await client.hover(uri, sym.range_start_line, sym.range_start_char)
                if hover is not None:
                    result[(sym.range_start_line, sym.range_start_char)] = hover.contents
            except Exception:
                logger.debug(
                    "Hover failed for symbol %s at %d:%d, skipping",
                    sym.name, sym.range_start_line, sym.range_start_char,
                )
            if sym.children:
                await self._hover_recursive(client, uri, sym.children, result)

    def _flatten_symbols(
        self,
        symbols: list[SymbolInfo],
        file_id: int,
        file_path: str,
        language: str,
        lsp_server: str,
        parent_fqn: str | None,
        type_signatures: dict[tuple[int, int], str] | None = None,
    ) -> list[tuple]:
        """Recursively flatten SymbolInfo tree into insert-ready tuples."""
        ts = type_signatures or {}
        rows: list[tuple] = []
        for sym in symbols:
            fqn = f"{parent_fqn}::{sym.name}" if parent_fqn else sym.name
            rows.append((
                fqn,
                sym.name,
                symbol_kind_name(sym.kind),
                language,
                file_id,
                file_path,
                sym.range_start_line,
                sym.range_end_line,
                parent_fqn,
                1.0,  # confidence: compiler_grade
                lsp_server,
                ts.get((sym.range_start_line, sym.range_start_char)),
            ))
            if sym.children:
                rows.extend(
                    self._flatten_symbols(
                        symbols=sym.children,
                        file_id=file_id,
                        file_path=file_path,
                        language=language,
                        lsp_server=lsp_server,
                        parent_fqn=fqn,
                        type_signatures=ts,
                    )
                )
        return rows

    def _batch_insert(self, rows: list[tuple]) -> None:
        """Single batch INSERT for all symbols from one file."""
        if not rows:
            return
        placeholders = ", ".join(["(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(rows))
        flat_params = [val for row in rows for val in row]
        self._provider.execute_query(
            "INSERT INTO symbols "
            "(fqn, name, kind, language, file_id, file_path, "
            "range_start, range_end, parent_fqn, confidence, lsp_server, "
            f"type_signature) VALUES {placeholders}",
            flat_params,
        )

    def _server_name(self, language: str) -> str:
        """Look up the LSP server command name for a language."""
        from chunkhound.lsp.registry import LANGUAGE_SERVER_REGISTRY

        config = LANGUAGE_SERVER_REGISTRY.get(language)
        return config.command if config and config.command else "unknown"

    async def populate_files(self) -> None:
        """Populate symbols for all indexed files. Used by batch indexing path."""
        from chunkhound.core.types.common import Language

        rows = self._provider.execute_query(
            "SELECT id, path FROM files"
        )
        for row in rows:
            file_path = Path(row["path"])
            lang = Language.from_file_extension(file_path).value
            await self.populate_file(
                file_path=file_path,
                file_id=row["id"],
                language=lang,
            )

    async def delete_file_symbols(self, file_id: int) -> None:
        """Remove all symbols for a given file_id."""
        self._provider.execute_query(
            "DELETE FROM symbols WHERE file_id = ?", [file_id]
        )
