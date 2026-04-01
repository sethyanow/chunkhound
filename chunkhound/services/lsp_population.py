"""LSP Population Service — writes documentSymbol results to the symbols table.

Background service that runs after tree-sitter chunking to populate the
symbols table with LSP-derived structure data.
"""

from __future__ import annotations

import enum
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

import duckdb

from chunkhound.lsp.constants import symbol_kind_name
from chunkhound.lsp.types import LSPCapability, LSPError, SymbolInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from chunkhound.lsp.client import LSPClient, LSPClientPool
    from chunkhound.lsp.types import CallHierarchyItem, Location
    from chunkhound.providers.database.duckdb_provider import DuckDBProvider

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
    """Populates the DuckDB symbols table from LSP documentSymbol results."""

    def __init__(
        self,
        pool: LSPClientPool,
        provider: DuckDBProvider,
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
            await self.delete_file_edges(file_id)
            await self.delete_file_symbols(file_id)

            # Flatten nested symbols into rows and insert
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

            # Query back symbol IDs for edge collection
            fqn_rows = self._provider.execute_query(
                "SELECT id, fqn FROM symbols WHERE file_id = ?", [file_id]
            )
            fqn_to_id = {row["fqn"]: row["id"] for row in fqn_rows}

            # Collect edges (LSP operations need file open)
            edges = await self._collect_edges(
                client, uri, symbols, str(file_path), fqn_to_id,
            )
        finally:
            await client.notify_did_close(uri)

        # Batch insert edges (pure DB write — safe after didClose)
        if edges:
            self._batch_insert_edges(edges)

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
        import asyncio

        from chunkhound.core.types.common import Language

        rows = self._provider.execute_query(
            "SELECT id, path FROM files"
        )
        languages_seen: set[str] = set()
        populated = 0
        failed = 0
        skipped = 0

        for row in rows:
            try:
                file_path = Path(row["path"])
                lang = Language.from_file_extension(file_path).value
                languages_seen.add(lang)
                result = await self.populate_file(
                    file_path=file_path,
                    file_id=row["id"],
                    language=lang,
                )
                if result is PopulateResult.POPULATED:
                    populated += 1
                elif result is PopulateResult.SKIPPED:
                    skipped += 1
            except (LSPError, OSError, UnicodeDecodeError, duckdb.Error) as exc:
                failed += 1
                logger.warning(
                    "Population failed for %s: %s: %s",
                    row["path"],
                    type(exc).__name__,
                    exc,
                )

        # workspaceSymbol pass: supplement per-file results with cross-file symbols
        await self._populate_workspace_symbols(languages_seen)

        logger.info(
            "Population complete: %d populated, %d failed, %d skipped",
            populated, failed, skipped,
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

                # Look up file_id — skip symbols for files not in the DB
                file_rows = self._provider.execute_query(
                    "SELECT id FROM files WHERE path = ?", [file_path_str]
                )
                if not file_rows:
                    continue

                file_id = file_rows[0]["id"]
                fqn = sym.name  # workspaceSymbol returns flat results, no parent context

                # In-batch dedup
                dedup_key = (fqn, file_path_str)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                # Skip if symbol already exists (from documentSymbol pass)
                existing = self._provider.execute_query(
                    "SELECT id FROM symbols WHERE fqn = ? AND file_path = ? LIMIT 1",
                    [fqn, file_path_str],
                )
                if existing:
                    continue

                # Insert with lower confidence (workspace symbols are less precise)
                self._batch_insert([(
                    fqn,
                    sym.name,
                    symbol_kind_name(sym.kind),
                    language,
                    file_id,
                    file_path_str,
                    sym.range_start_line,
                    sym.range_end_line,
                    None,  # parent_fqn — flat results, no parent context
                    0.9,   # confidence: workspace symbol (less precise than documentSymbol)
                    lsp_server,
                    None,  # type_signature — not collected for workspace symbols
                )])

    async def _collect_edges(
        self,
        client: LSPClient,
        uri: str,
        symbols: list[SymbolInfo],
        file_path: str,
        fqn_to_id: dict[str, int],
    ) -> list[tuple]:
        """Collect edges from LSP operations for all symbols in a file.

        Calls definition/references/implementation/calls per symbol, resolves
        targets via _resolve_symbol, and returns deduplicated edge tuples.

        Args:
            client: Active LSP client (file must be open).
            uri: File URI for the source file.
            symbols: Symbol tree from documentSymbol.
            file_path: Relative path of the source file.
            fqn_to_id: Mapping of FQN → symbol_id for the source file's symbols.

        Returns:
            List of 9-element tuples ready for batch insert into symbol_edges.
        """
        # Dedup dict: (from_fqn, to_fqn, edge_kind) → edge tuple
        edges: dict[tuple[str, str, str], tuple] = {}
        # Infer language from fqn_to_id keys — look up any symbol's language from DB
        # This is called from populate_file which knows the language, but the interface
        # doesn't pass it. Use _server_name with a DB lookup as fallback.
        lsp_server = "unknown"
        if fqn_to_id:
            any_id = next(iter(fqn_to_id.values()))
            lang_rows = self._provider.execute_query(
                "SELECT language FROM symbols WHERE id = ? LIMIT 1", [any_id]
            )
            if lang_rows and lang_rows[0]["language"]:
                lsp_server = self._server_name(lang_rows[0]["language"])
        await self._edges_recursive(
            client, uri, symbols, file_path, fqn_to_id,
            parent_fqn=None, lsp_server=lsp_server, edges=edges,
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
        edges: dict[tuple[str, str, str], tuple],
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
                        client, uri, sym.children, file_path, fqn_to_id,
                        parent_fqn=fqn, lsp_server=lsp_server, edges=edges,
                    )
                continue

            try:
                for edge_kind, operation in ops:
                    try:
                        results = await operation(uri, sym.range_start_line, sym.range_start_char)
                    except Exception:
                        logger.debug(
                            "Edge op %s failed for %s at %d:%d, skipping",
                            edge_kind, sym.name, sym.range_start_line, sym.range_start_char,
                        )
                        continue

                    for loc in results:
                        target = self._resolve_symbol(loc.uri, loc.range_start_line)
                        if target is None:
                            continue
                        to_id, to_fqn, to_file = target

                        # Skip self-edges
                        if fqn == to_fqn and edge_kind == "defines":
                            continue

                        dedup_key = (fqn, to_fqn, edge_kind)
                        edges[dedup_key] = (
                            from_id, fqn, file_path,
                            to_id, to_fqn, to_file,
                            edge_kind, 1.0, lsp_server,
                        )
            except Exception:
                logger.debug(
                    "Edge collection failed for symbol %s, skipping",
                    sym.name,
                )

            if sym.children:
                await self._edges_recursive(
                    client, uri, sym.children, file_path, fqn_to_id,
                    parent_fqn=fqn, lsp_server=lsp_server, edges=edges,
                )

    def _resolve_symbol(self, uri: str, line: int) -> tuple[int, str, str] | None:
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
        rows = self._provider.execute_query(
            "SELECT id, fqn, file_path FROM symbols "
            "WHERE file_path = ? AND range_start <= ? AND range_end >= ? "
            "ORDER BY (range_end - range_start) ASC LIMIT 1",
            [file_path_str, line, line],
        )
        if not rows:
            return None
        row = rows[0]
        return (row["id"], row["fqn"], row["file_path"])

    def _batch_insert_edges(self, edges: list[tuple]) -> None:
        """Single batch INSERT for all edges from one file."""
        if not edges:
            return
        placeholders = ", ".join(
            ["(?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(edges)
        )
        flat_params = [val for edge in edges for val in edge]
        self._provider.execute_query(
            "INSERT INTO symbol_edges "
            "(from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, "
            f"to_file, edge_kind, confidence, lsp_server) VALUES {placeholders}",
            flat_params,
        )

    async def delete_file_edges(self, file_id: int) -> None:
        """Remove all edges that reference symbols belonging to this file."""
        await self._provider.execute_query_async(
            "DELETE FROM symbol_edges WHERE "
            "from_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?) OR "
            "to_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?)",
            [file_id, file_id],
        )

    async def delete_file_symbols(self, file_id: int) -> None:
        """Remove all symbols for a given file_id."""
        await self._provider.execute_query_async(
            "DELETE FROM symbols WHERE file_id = ?", [file_id]
        )
