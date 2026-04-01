"""LSP client with lifecycle management, state machine, capability gating, and 8 operations.

Zero chunkhound imports — stdlib only + sibling lsp modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from chunkhound.lsp.protocol import JsonRpcTransport
from chunkhound.lsp.types import (
    CallHierarchyItem,
    Diagnostic,
    HoverResult,
    Location,
    LSPCapability,
    LSPCapabilityError,
    LSPError,
    LSPTransportError,
    ServerConfig,
    ServerState,
    SymbolInfo,
)

logger = logging.getLogger(__name__)


class LSPClient:
    """Single LSP server connection with lifecycle, state, capability gating, and operations."""

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._state = ServerState.NOT_STARTED
        self._degraded_reason: dict[str, str] | None = None
        self._transport: JsonRpcTransport | None = None
        self._capabilities: set[LSPCapability] = set()
        self._server_info: dict[str, Any] | None = None
        self._process_monitor: asyncio.Task[None] | None = None
        # Notification-collected diagnostics (push model)
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diagnostics_events: dict[str, asyncio.Event] = {}
        # Progress token tracking ($/progress lifecycle)
        self._progress: dict[str, dict[str, Any]] = {}

    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def degraded_reason(self) -> dict[str, str] | None:
        return self._degraded_reason

    @property
    def capabilities(self) -> frozenset[LSPCapability]:
        return frozenset(self._capabilities)

    @property
    def server_info(self) -> dict[str, Any] | None:
        return self._server_info

    async def start(self, workspace_root: str | Path) -> dict[str, Any]:
        """Spawn server, run initialize handshake, return initialize result."""
        if self._state != ServerState.NOT_STARTED:
            raise LSPError(
                f"Cannot start client in state {self._state.value}"
            )
        if self._config.command is None:
            raise LSPError(
                f"No server command configured for {self._config.language_id}"
            )

        self._state = ServerState.INITIALIZING
        workspace_path = Path(workspace_root).resolve()
        workspace_uri = workspace_path.as_uri()

        try:
            self._transport = await JsonRpcTransport.create(
                self._config.command,
                args=self._config.args,
                env=self._config.env,
                cwd=str(workspace_path),
                notification_handler=self._handle_notification,
            )
        except FileNotFoundError:
            self._state = ServerState.DEGRADED
            self._degraded_reason = {
                "code": "spawn_failed",
                "detail": f"Command not found: {self._config.command}",
            }
            raise LSPTransportError(
                f"Server binary not found: {self._config.command}"
            ) from None
        except PermissionError:
            self._state = ServerState.DEGRADED
            self._degraded_reason = {
                "code": "spawn_failed",
                "detail": f"Permission denied: {self._config.command}",
            }
            raise LSPTransportError(
                f"Permission denied for server binary: {self._config.command}"
            ) from None

        # Send initialize request
        init_params: dict[str, Any] = {
            "processId": os.getpid(),
            "rootUri": workspace_uri,
            "rootPath": str(workspace_path),
            "capabilities": {
                "textDocument": {
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {},
                    "references": {},
                    "implementation": {},
                    "callHierarchy": {},
                    "diagnostic": {},
                    "publishDiagnostics": {},
                },
                "window": {"workDoneProgress": True},
            },
            "workspaceFolders": [
                {"uri": workspace_uri, "name": workspace_path.name}
            ],
        }
        if self._config.init_options is not None:
            init_params["initializationOptions"] = self._config.init_options

        try:
            timeout = self._config.request_timeout
            result = await self._transport.send_request(
                "initialize", init_params, timeout=timeout
            )
        except Exception:
            self._state = ServerState.DEGRADED
            self._degraded_reason = {
                "code": "initialize_failed",
                "detail": "Initialize handshake failed",
            }
            await self._transport.close()
            self._transport = None
            raise

        # Parse capabilities
        caps = result.get("capabilities", {})
        self._capabilities = self._parse_capabilities(caps)

        # Store server info
        self._server_info = result.get("serverInfo")

        # Send initialized notification
        await self._transport.send_notification("initialized", {})

        self._state = ServerState.READY

        # Start process monitor
        self._process_monitor = asyncio.create_task(self._monitor_process())

        return result

    async def stop(self) -> None:
        """Gracefully shut down the server."""
        if self._process_monitor and not self._process_monitor.done():
            self._process_monitor.cancel()
            try:
                await self._process_monitor
            except asyncio.CancelledError:
                pass
            self._process_monitor = None

        if self._transport:
            # Try graceful shutdown
            try:
                await self._transport.send_request(
                    "shutdown", timeout=5.0
                )
                await self._transport.send_notification("exit")
            except Exception:
                pass  # Best effort
            await self._transport.close()
            self._transport = None

        self._state = ServerState.STOPPED

    @staticmethod
    def _parse_capabilities(caps: dict[str, Any]) -> set[LSPCapability]:
        """Map server capabilities dict to LSPCapability set."""
        result: set[LSPCapability] = set()
        for cap in LSPCapability:
            if caps.get(cap.value):
                result.add(cap)
        return result

    async def _monitor_process(self) -> None:
        """Watch for unexpected server process exit."""
        try:
            if self._transport is None:
                return
            proc = self._transport._process
            await proc.wait()
            # Process exited unexpectedly while we're in READY state
            if self._state == ServerState.READY:
                self._state = ServerState.DEGRADED
                self._degraded_reason = {
                    "code": "process_exited",
                    "detail": f"Server process exited with code {proc.returncode}",
                }
                logger.warning(
                    "LSP server %s exited unexpectedly (code %s)",
                    self._config.language_id,
                    proc.returncode,
                )
        except asyncio.CancelledError:
            pass

    def _require_capability(self, capability: LSPCapability, method: str) -> None:
        """Raise LSPCapabilityError if the server doesn't advertise the capability."""
        if capability not in self._capabilities:
            server_name = (
                self._server_info.get("name") if self._server_info else None
            )
            raise LSPCapabilityError(method, server_name)

    def _require_transport(self) -> JsonRpcTransport:
        """Return the transport or raise if not connected."""
        if self._transport is None or self._state != ServerState.READY:
            raise LSPTransportError(
                f"Client not ready (state: {self._state.value})"
            )
        return self._transport

    async def _send_operation(
        self,
        method: str,
        capability: LSPCapability,
        params: dict[str, Any],
    ) -> Any:
        """Send an LSP request with capability gate and transport check."""
        self._require_capability(capability, method)
        transport = self._require_transport()
        try:
            return await transport.send_request(
                method, params, timeout=self._config.request_timeout
            )
        except LSPTransportError:
            self._state = ServerState.DEGRADED
            self._degraded_reason = {
                "code": "transport_error",
                "detail": f"Transport failed during {method}",
            }
            raise

    @staticmethod
    def _make_text_document(uri: str) -> dict[str, str]:
        return {"textDocument": {"uri": uri}}

    @staticmethod
    def _make_text_document_position(
        uri: str, line: int, char: int
    ) -> dict[str, Any]:
        return {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
        }

    async def document_symbols(self, uri: str) -> list[SymbolInfo]:
        result = await self._send_operation(
            "textDocument/documentSymbol",
            LSPCapability.DOCUMENT_SYMBOL,
            self._make_text_document(uri),
        )
        return self._parse_symbols(result or [])

    async def workspace_symbols(self, query: str = "") -> list[SymbolInfo]:
        result = await self._send_operation(
            "workspace/symbol",
            LSPCapability.WORKSPACE_SYMBOL,
            {"query": query},
        )
        return self._parse_symbols(result or [])

    async def go_to_definition(
        self, uri: str, line: int, char: int
    ) -> list[Location]:
        result = await self._send_operation(
            "textDocument/definition",
            LSPCapability.DEFINITION,
            self._make_text_document_position(uri, line, char),
        )
        return self._parse_locations(result)

    async def find_references(
        self, uri: str, line: int, char: int
    ) -> list[Location]:
        params = self._make_text_document_position(uri, line, char)
        params["context"] = {"includeDeclaration": True}
        result = await self._send_operation(
            "textDocument/references",
            LSPCapability.REFERENCES,
            params,
        )
        return self._parse_locations(result)

    async def hover(self, uri: str, line: int, char: int) -> HoverResult | None:
        result = await self._send_operation(
            "textDocument/hover",
            LSPCapability.HOVER,
            self._make_text_document_position(uri, line, char),
        )
        if not result:
            return None
        return self._parse_hover(result)

    async def incoming_calls(
        self, uri: str, line: int, char: int
    ) -> list[CallHierarchyItem]:
        # Two-step: prepareCallHierarchy → callHierarchy/incomingCalls
        items = await self._prepare_call_hierarchy(uri, line, char)
        if not items:
            return []
        result = await self._send_operation(
            "callHierarchy/incomingCalls",
            LSPCapability.CALL_HIERARCHY,
            {"item": items[0]},
        )
        return [
            self._parse_call_hierarchy_item(call["from"])
            for call in (result or [])
        ]

    async def outgoing_calls(
        self, uri: str, line: int, char: int
    ) -> list[CallHierarchyItem]:
        # Two-step: prepareCallHierarchy → callHierarchy/outgoingCalls
        items = await self._prepare_call_hierarchy(uri, line, char)
        if not items:
            return []
        result = await self._send_operation(
            "callHierarchy/outgoingCalls",
            LSPCapability.CALL_HIERARCHY,
            {"item": items[0]},
        )
        return [
            self._parse_call_hierarchy_item(call["to"])
            for call in (result or [])
        ]

    async def _prepare_call_hierarchy(
        self, uri: str, line: int, char: int
    ) -> list[dict[str, Any]]:
        """First step of call hierarchy protocol."""
        result = await self._send_operation(
            "textDocument/prepareCallHierarchy",
            LSPCapability.CALL_HIERARCHY,
            self._make_text_document_position(uri, line, char),
        )
        return result or []

    async def go_to_implementation(
        self, uri: str, line: int, char: int
    ) -> list[Location]:
        # Not all servers advertise implementationProvider (e.g. pyright for Python).
        # If not supported, return empty list instead of raising.
        if LSPCapability.IMPLEMENTATION not in self._capabilities:
            return []
        result = await self._send_operation(
            "textDocument/implementation",
            LSPCapability.IMPLEMENTATION,
            self._make_text_document_position(uri, line, char),
        )
        return self._parse_locations(result)

    async def get_diagnostics(self, uri: str) -> list[Diagnostic]:
        """Get diagnostics for a file.

        Uses pull model (textDocument/diagnostic) if supported,
        falls back to notification-collected diagnostics (publishDiagnostics).
        """
        if LSPCapability.DIAGNOSTIC in self._capabilities:
            result = await self._send_operation(
                "textDocument/diagnostic",
                LSPCapability.DIAGNOSTIC,
                self._make_text_document(uri),
            )
            items = (result or {}).get("items", [])
            return [self._parse_diagnostic(d) for d in items]

        # Fall back to notification-collected diagnostics
        cached = self._diagnostics.get(uri, [])
        return [self._parse_diagnostic(d) for d in cached]

    async def notify_did_open(self, uri: str, text: str, language_id: str = "python") -> None:
        """Send textDocument/didOpen so the server starts analyzing the file."""
        transport = self._require_transport()
        await transport.send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                },
            },
        )

    async def notify_did_close(self, uri: str) -> None:
        """Send textDocument/didClose to free server memory for a file."""
        transport = self._require_transport()
        await transport.send_notification(
            "textDocument/didClose",
            {"textDocument": {"uri": uri}},
        )

    async def wait_for_diagnostics(
        self, uri: str, timeout: float = 10.0, wait_for_nonempty: bool = True
    ) -> list[Diagnostic]:
        """Wait for publishDiagnostics notification for a URI, then return them.

        If wait_for_nonempty, keeps waiting until non-empty diagnostics arrive
        or timeout expires. Pyright often sends an empty list first, then
        the real diagnostics after analysis completes.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            if uri not in self._diagnostics_events:
                self._diagnostics_events[uri] = asyncio.Event()

            event = self._diagnostics_events[uri]
            event.clear()

            remaining = deadline - loop.time()
            if remaining <= 0:
                break

            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break

            # Check if we got real diagnostics
            cached = self._diagnostics.get(uri, [])
            if cached or not wait_for_nonempty:
                break

        return await self.get_diagnostics(uri)

    def _handle_notification(self, method: str, params: dict[str, Any] | None) -> None:
        """Handle LSP notifications from the server."""
        if method == "textDocument/publishDiagnostics" and params:
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])
            self._diagnostics[uri] = diagnostics
            # Signal any waiters
            event = self._diagnostics_events.get(uri)
            if event:
                event.set()
        elif method == "window/logMessage" and params:
            msg_type = params.get("type", 4)
            message = params.get("message", "")
            level_map = {1: logging.ERROR, 2: logging.WARNING, 3: logging.INFO}
            level = level_map.get(msg_type, logging.DEBUG)
            logger.log(level, "LSP [%s]: %s", self._config.language_id, message)
        elif method == "$/progress" and params:
            token = params.get("token")
            if token is None:
                return
            value = params.get("value", {})
            kind = value.get("kind")
            if kind == "begin":
                self._progress[token] = {
                    "title": value.get("title", ""),
                    "percentage": 0,
                }
            elif kind == "report":
                entry = self._progress.get(token)
                if entry is not None:
                    entry["percentage"] = value.get("percentage", entry["percentage"])
            elif kind == "end":
                self._progress.pop(token, None)
        else:
            logger.debug("LSP notification (unhandled): %s", method)

    # --- Response Parsers ---

    @classmethod
    def _parse_symbols(cls, data: list[dict[str, Any]]) -> list[SymbolInfo]:
        symbols = []
        for item in data:
            location = item.get("location", {})
            rng = item.get("range", location.get("range", {}))
            start = rng.get("start", {})
            end = rng.get("end", {})
            children_data = item.get("children", [])
            location_uri = location.get("uri")
            symbols.append(
                SymbolInfo(
                    name=item.get("name", ""),
                    kind=item.get("kind", 0),
                    range_start_line=start.get("line", 0),
                    range_start_char=start.get("character", 0),
                    range_end_line=end.get("line", 0),
                    range_end_char=end.get("character", 0),
                    detail=item.get("detail"),
                    container_name=item.get("containerName"),
                    children=cls._parse_symbols(children_data),
                    location_uri=location_uri,
                )
            )
        return symbols

    @staticmethod
    def _parse_locations(data: Any) -> list[Location]:
        if data is None:
            return []
        # LSP can return a single Location, a list, or LocationLink[]
        if isinstance(data, dict):
            data = [data]
        locations = []
        for item in data:
            # Handle LocationLink (has targetUri/targetRange)
            uri = item.get("uri", item.get("targetUri", ""))
            rng = item.get("range", item.get("targetRange", {}))
            start = rng.get("start", {})
            end = rng.get("end", {})
            locations.append(
                Location(
                    uri=uri,
                    range_start_line=start.get("line", 0),
                    range_start_char=start.get("character", 0),
                    range_end_line=end.get("line", 0),
                    range_end_char=end.get("character", 0),
                )
            )
        return locations

    @staticmethod
    def _parse_hover(data: dict[str, Any]) -> HoverResult:
        contents = data.get("contents", "")
        if isinstance(contents, dict):
            contents = contents.get("value", "")
        elif isinstance(contents, list):
            parts = []
            for c in contents:
                parts.append(c.get("value", str(c)) if isinstance(c, dict) else str(c))
            contents = "\n".join(parts)

        rng = data.get("range")
        if rng:
            start = rng.get("start", {})
            end = rng.get("end", {})
            return HoverResult(
                contents=contents,
                range_start_line=start.get("line"),
                range_start_char=start.get("character"),
                range_end_line=end.get("line"),
                range_end_char=end.get("character"),
            )
        return HoverResult(contents=contents)

    @staticmethod
    def _parse_call_hierarchy_item(data: dict[str, Any]) -> CallHierarchyItem:
        rng = data.get("range", {})
        sel = data.get("selectionRange", {})
        return CallHierarchyItem(
            name=data.get("name", ""),
            kind=data.get("kind", 0),
            uri=data.get("uri", ""),
            range_start_line=rng.get("start", {}).get("line", 0),
            range_start_char=rng.get("start", {}).get("character", 0),
            range_end_line=rng.get("end", {}).get("line", 0),
            range_end_char=rng.get("end", {}).get("character", 0),
            selection_range_start_line=sel.get("start", {}).get("line", 0),
            selection_range_start_char=sel.get("start", {}).get("character", 0),
            selection_range_end_line=sel.get("end", {}).get("line", 0),
            selection_range_end_char=sel.get("end", {}).get("character", 0),
            detail=data.get("detail"),
            _raw=data,
        )

    @staticmethod
    def _parse_diagnostic(data: dict[str, Any]) -> Diagnostic:
        rng = data.get("range", {})
        start = rng.get("start", {})
        end = rng.get("end", {})
        return Diagnostic(
            range_start_line=start.get("line", 0),
            range_start_char=start.get("character", 0),
            range_end_line=end.get("line", 0),
            range_end_char=end.get("character", 0),
            severity=data.get("severity", 1),
            message=data.get("message", ""),
            source=data.get("source"),
            code=data.get("code"),
        )


class LSPClientPool:
    """Connection pool for LSP clients, keyed by (language_id, workspace_root).

    Features:
    - Lazy spawn (on first get, not at construction)
    - State check on get() — respawn DEGRADED/STOPPED clients
    - asyncio.Lock per key to prevent double-spawn on concurrent access
    """

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], LSPClient] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def get(self, language_id: str, workspace_root: str) -> LSPClient:
        """Get or create an LSP client for the given language and workspace."""
        from chunkhound.lsp.registry import LANGUAGE_SERVER_REGISTRY

        key = (language_id, workspace_root)

        # Get or create per-key lock to prevent double-spawn
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()

        async with self._locks[key]:
            # Check if existing client is healthy
            client = self._clients.get(key)
            if client is not None and client.state == ServerState.READY:
                return client

            # Remove stale client
            if client is not None:
                self._clients.pop(key, None)

            # Look up config
            config = LANGUAGE_SERVER_REGISTRY.get(language_id)
            if config is None:
                raise LSPError(
                    f"No server config for language: {language_id}"
                )
            if config.command is None:
                raise LSPError(
                    f"No server binary configured for: {language_id}"
                )

            # Spawn and initialize
            new_client = LSPClient(config)
            await new_client.start(workspace_root)
            self._clients[key] = new_client
            return new_client

    async def stop(self, language_id: str, workspace_root: str) -> None:
        """Stop a specific client."""
        key = (language_id, workspace_root)
        client = self._clients.pop(key, None)
        if client is not None:
            await client.stop()

    async def stop_all(self) -> None:
        """Stop all managed clients."""
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            try:
                await client.stop()
            except Exception:
                logger.warning(
                    "Error stopping LSP client %s", client._config.language_id
                )
