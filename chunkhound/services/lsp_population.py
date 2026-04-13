"""LSP Population Service — writes documentSymbol results to the symbols table.

Background service that runs after tree-sitter chunking to populate the
symbols table with LSP-derived structure data. Provider-agnostic: talks to
any DatabaseProvider implementation through the protocol.
"""

from __future__ import annotations

import enum
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from chunkhound.core.models.symbol import EdgeRow, SymbolRow
from chunkhound.interfaces.database_provider import ProviderError
from chunkhound.lsp.constants import symbol_kind_name
from chunkhound.lsp.types import LSPCapability, LSPError, SymbolInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from chunkhound.interfaces.database_provider import DatabaseProvider
    from chunkhound.lsp.client import LSPClient, LSPClientPool
    from chunkhound.lsp.types import CallHierarchyItem, Location

    _EdgeOp = Callable[
        [str, int, int],
        Coroutine[Any, Any, list[Location] | list[CallHierarchyItem]],
    ]

logger = logging.getLogger(__name__)


class PopulateResult(enum.Enum):
    """Status returned by populate_file for accurate caller counting."""

    POPULATED = "populated"
    SKIPPED = "skipped"
    FAILED = "failed"


class LSPPopulationService:
    """Populates the symbols/symbol_edges tables from LSP results via the
    DatabaseProvider protocol."""

    def __init__(
        self,
        pool: LSPClientPool,
        provider: DatabaseProvider,
        workspace_root: Path,
    ) -> None:
        self._pool = pool
        self._provider = provider
        self._workspace_root = workspace_root.resolve()

    async def populate_file(
        self,
        file_path: Path,
        file_id: int,
        language: str,
    ) -> PopulateResult:
        """Run documentSymbol on a single file and write rows to the symbols table.

        Follows LSP protocol: didOpen → documentSymbol → didClose.
        Skips gracefully if no LSP server is configured for the language.

        Returns:
            PopulateResult.POPULATED if symbols were written.
            PopulateResult.SKIPPED if file was skipped (no server, unreadable, no symbols).
        """
        try:
            client = await self._pool.get(language, str(self._workspace_root))
        except LSPError:
            logger.debug("No LSP server for language=%s, skipping %s", language, file_path)
            return PopulateResult.SKIPPED

        abs_path = self._workspace_root / file_path
        uri = abs_path.as_uri()

        try:
            content = abs_path.read_text()
        except (OSError, UnicodeDecodeError):
            logger.debug("Cannot read %s, skipping LSP population", file_path)
            return PopulateResult.SKIPPED

        try:
            await client.notify_did_open(uri, content, language)
            symbols = await client.document_symbols(uri)
            type_signatures = await self._collect_type_signatures(client, uri, symbols) if symbols else {}

            if not symbols:
                return PopulateResult.SKIPPED

            # Delete edges before symbols (edges reference symbol IDs via FK)
            await self._provider.delete_edges_by_file_async(file_id)
            await self._provider.delete_symbols_by_file_async(file_id)

            # Flatten nested symbols into SymbolRow dicts and batch insert
            symbol_rows = self._flatten_symbols(
                symbols=symbols,
                file_id=file_id,
                file_path=str(file_path),
                language=language,
                lsp_server=self._server_name(language),
                parent_fqn=None,
                type_signatures=type_signatures,
            )

            if symbol_rows:
                await self._provider.insert_symbols_batch_async(symbol_rows)

            # Query back symbol IDs for edge collection
            fqn_to_id = await self._provider.query_symbol_fqns_by_file_async(file_id)

            # Collect edges (LSP operations need file open)
            edges = await self._collect_edges(
                client,
                uri,
                symbols,
                str(file_path),
                fqn_to_id,
                language,
            )
        finally:
            await client.notify_did_close(uri)

        # Batch insert edges (pure DB write — safe after didClose)
        if edges:
            await self._provider.insert_edges_batch_async(edges)

        return PopulateResult.POPULATED

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
                    sym.name,
                    sym.range_start_line,
                    sym.range_start_char,
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
    ) -> list[SymbolRow]:
        """Recursively flatten SymbolInfo tree into SymbolRow dicts."""
        ts = type_signatures or {}
        rows: list[SymbolRow] = []
        for sym in symbols:
            fqn = f"{parent_fqn}::{sym.name}" if parent_fqn else sym.name
            rows.append(
                SymbolRow(
                    fqn=fqn,
                    name=sym.name,
                    kind=symbol_kind_name(sym.kind),
                    language=language,
                    file_id=file_id,
                    file_path=file_path,
                    range_start=sym.range_start_line,
                    range_end=sym.range_end_line,
                    parent_fqn=parent_fqn,
                    confidence=1.0,  # compiler-grade
                    lsp_server=lsp_server,
                    type_signature=ts.get((sym.range_start_line, sym.range_start_char)),
                )
            )
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

    def _server_name(self, language: str) -> str:
        """Look up the LSP server command name for a language."""
        from chunkhound.lsp.registry import LANGUAGE_SERVER_REGISTRY

        config = LANGUAGE_SERVER_REGISTRY.get(language)
        return config.command if config and config.command else "unknown"

    async def populate_files(self) -> None:
        """Populate symbols for all indexed files. Used by batch indexing path."""
        from chunkhound.core.types.common import Language

        file_records = await self._provider.get_all_files_async()
        languages_seen: set[str] = set()
        populated = 0
        failed = 0
        skipped = 0

        for file_record in file_records:
            try:
                file_path = Path(file_record["path"])
                lang = Language.from_file_extension(file_path).value
                languages_seen.add(lang)
                result = await self.populate_file(
                    file_path=file_path,
                    file_id=file_record["id"],
                    language=lang,
                )
                if result is PopulateResult.POPULATED:
                    populated += 1
                elif result is PopulateResult.SKIPPED:
                    skipped += 1
            except (LSPError, OSError, UnicodeDecodeError, ProviderError) as exc:
                # Expected per-file failure types. Unexpected exception types
                # (RuntimeError, AssertionError, etc.) propagate and abort the
                # batch so internal bugs stay visible.
                failed += 1
                logger.warning(
                    "Population failed for %s: %s: %s",
                    file_record["path"],
                    type(exc).__name__,
                    exc,
                )

        # workspaceSymbol pass: supplement per-file results with cross-file symbols
        await self._populate_workspace_symbols(languages_seen)

        logger.info(
            "Population complete: %d populated, %d failed, %d skipped",
            populated,
            failed,
            skipped,
        )

    async def _populate_workspace_symbols(self, languages: set[str]) -> None:
        """Call workspaceSymbol per language and insert new symbols not already in DB."""
        for language in languages:
            try:
                client = await self._pool.get(language, str(self._workspace_root))
            except LSPError:
                continue

            if LSPCapability.WORKSPACE_SYMBOL not in client.capabilities:
                continue

            try:
                symbols = await client.workspace_symbols("")
            except LSPError:
                logger.debug("workspaceSymbol failed for language=%s, skipping", language)
                continue

            lsp_server = self._server_name(language)
            seen: set[tuple[str, str]] = set()  # (fqn, file_path) dedup within batch
            new_rows: list[SymbolRow] = []

            for sym in symbols:
                if sym.location_uri is None:
                    continue

                parsed = urlparse(sym.location_uri)
                if parsed.scheme != "file":
                    continue

                abs_path = Path(unquote(parsed.path))
                try:
                    rel_path = abs_path.relative_to(self._workspace_root)
                except ValueError:
                    continue

                file_path_str = str(rel_path)

                # Look up file_id — skip symbols for files not in the DB.
                # Default as_model=False so the protocol returns a dict.
                file_record = await self._provider.get_file_by_path_async(file_path_str)
                if not file_record or not isinstance(file_record, dict):
                    continue
                file_id = file_record["id"]

                fqn = sym.name  # workspaceSymbol returns flat results, no parent context

                # In-batch dedup
                dedup_key = (fqn, file_path_str)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                # Skip if symbol already exists (from documentSymbol pass)
                if await self._provider.query_symbols_by_fqn_exists_async(fqn, file_path_str):
                    continue

                # Stage row for batch insert (workspace symbols use lower confidence)
                new_rows.append(
                    SymbolRow(
                        fqn=fqn,
                        name=sym.name,
                        kind=symbol_kind_name(sym.kind),
                        language=language,
                        file_id=file_id,
                        file_path=file_path_str,
                        range_start=sym.range_start_line,
                        range_end=sym.range_end_line,
                        parent_fqn=None,
                        confidence=0.9,  # less precise than documentSymbol
                        lsp_server=lsp_server,
                        type_signature=None,
                    )
                )

            # Batch insert all new workspace symbols for this language at once
            if new_rows:
                await self._provider.insert_symbols_batch_async(new_rows)

    async def _collect_edges(
        self,
        client: LSPClient,
        uri: str,
        symbols: list[SymbolInfo],
        file_path: str,
        fqn_to_id: dict[str, int],
        language: str,
    ) -> list[EdgeRow]:
        """Collect edges from LSP operations for all symbols in a file.

        Calls definition/references/implementation/calls per symbol, resolves
        targets via _resolve_symbol, and returns deduplicated edge dicts.

        Args:
            client: Active LSP client (file must be open).
            uri: File URI for the source file.
            symbols: Symbol tree from documentSymbol.
            file_path: Relative path of the source file.
            fqn_to_id: Mapping of FQN → symbol_id for the source file's symbols.
            language: Language identifier (caller already knows it; previous
                code looked it up via SQL — that defect is now removed).

        Returns:
            List of EdgeRow dicts ready for insert_edges_batch_async.
        """
        # Dedup dict: (from_fqn, to_fqn, edge_kind) → EdgeRow
        edges: dict[tuple[str, str, str], EdgeRow] = {}
        lsp_server = self._server_name(language)
        await self._edges_recursive(
            client,
            uri,
            symbols,
            file_path,
            fqn_to_id,
            parent_fqn=None,
            lsp_server=lsp_server,
            edges=edges,
        )
        return list(edges.values())

    async def _edges_recursive(
        self,
        client: LSPClient,
        uri: str,
        symbols: list[SymbolInfo],
        file_path: str,
        fqn_to_id: dict[str, int],
        parent_fqn: str | None,
        lsp_server: str,
        edges: dict[tuple[str, str, str], EdgeRow],
    ) -> None:
        """Recursively walk symbols and collect edges from LSP operations."""
        # Build operation list gated by capabilities
        ops: list[tuple[str, _EdgeOp]] = []
        if LSPCapability.DEFINITION in client.capabilities:
            ops.append(("defines", client.go_to_definition))
        if LSPCapability.REFERENCES in client.capabilities:
            ops.append(("references", client.find_references))
        if LSPCapability.IMPLEMENTATION in client.capabilities:
            ops.append(("implements", client.go_to_implementation))
        if LSPCapability.CALL_HIERARCHY in client.capabilities:
            ops.append(("called_by", client.incoming_calls))
            ops.append(("calls", client.outgoing_calls))

        for sym in symbols:
            fqn = f"{parent_fqn}::{sym.name}" if parent_fqn else sym.name
            from_id = fqn_to_id.get(fqn)
            if from_id is None:
                logger.debug("No symbol_id for FQN %s, skipping edge collection", fqn)
                if sym.children:
                    await self._edges_recursive(
                        client,
                        uri,
                        sym.children,
                        file_path,
                        fqn_to_id,
                        parent_fqn=fqn,
                        lsp_server=lsp_server,
                        edges=edges,
                    )
                continue

            # Prefer selectionRange (name position) over range (keyword position)
            op_line = (
                sym.selection_range_start_line if sym.selection_range_start_line is not None else sym.range_start_line
            )
            op_char = (
                sym.selection_range_start_char if sym.selection_range_start_char is not None else sym.range_start_char
            )

            try:
                for edge_kind, operation in ops:
                    try:
                        results = await operation(uri, op_line, op_char)
                    except Exception:
                        logger.debug(
                            "Edge op %s failed for %s at %d:%d, skipping",
                            edge_kind,
                            sym.name,
                            sym.range_start_line,
                            sym.range_start_char,
                        )
                        continue

                    for loc in results:
                        target = await self._resolve_symbol(loc.uri, loc.range_start_line)
                        if target is None:
                            continue
                        to_id, to_fqn, to_file = target

                        # Skip self-edges
                        if fqn == to_fqn and edge_kind == "defines":
                            continue

                        dedup_key = (fqn, to_fqn, edge_kind)
                        edges[dedup_key] = EdgeRow(
                            from_symbol_id=from_id,
                            from_fqn=fqn,
                            from_file=file_path,
                            to_symbol_id=to_id,
                            to_fqn=to_fqn,
                            to_file=to_file,
                            edge_kind=edge_kind,
                            confidence=1.0,
                            lsp_server=lsp_server,
                        )
            except Exception:
                logger.debug(
                    "Edge collection failed for symbol %s, skipping",
                    sym.name,
                )

            if sym.children:
                await self._edges_recursive(
                    client,
                    uri,
                    sym.children,
                    file_path,
                    fqn_to_id,
                    parent_fqn=fqn,
                    lsp_server=lsp_server,
                    edges=edges,
                )

    async def _resolve_symbol(self, uri: str, line: int) -> tuple[int, str, str] | None:
        """Resolve an LSP result location to the innermost symbol in the DB.

        Args:
            uri: File URI from an LSP result (e.g. "file:///path/to/file.py").
            line: Line number within the file (0-based, matching stored range_start/range_end).

        Returns:
            (symbol_id, fqn, file_path) for the most specific symbol at that line,
            or None if the URI is not a file:// scheme, points outside the workspace,
            or no symbol covers that line.
        """
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None

        abs_path = Path(unquote(parsed.path))
        try:
            rel_path = abs_path.relative_to(self._workspace_root)
        except ValueError:
            return None

        file_path_str = str(rel_path)
        row = await self._provider.query_symbols_by_range_async(file_path_str, line)
        if row is None:
            return None
        return (row["id"], row["fqn"], row["file_path"])
