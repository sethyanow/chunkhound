"""Type definitions for LSP client manager.

Zero chunkhound imports — stdlib only.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# --- Server Configuration ---


@dataclass(frozen=True)
class ServerConfig:
    """Configuration for a language server."""

    language_id: str
    command: str | None  # None = no known server for this language
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    init_options: dict[str, Any] | None = None
    request_timeout: float = 30.0  # seconds, per-operation default


# --- Server State ---


class ServerState(enum.Enum):
    """LSP server lifecycle states."""

    NOT_STARTED = "not_started"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"


# --- Capabilities ---


class LSPCapability(enum.Enum):
    """LSP server capabilities that can be gated."""

    DOCUMENT_SYMBOL = "documentSymbolProvider"
    WORKSPACE_SYMBOL = "workspaceSymbolProvider"
    DEFINITION = "definitionProvider"
    REFERENCES = "referencesProvider"
    IMPLEMENTATION = "implementationProvider"
    CALL_HIERARCHY = "callHierarchyProvider"
    HOVER = "hoverProvider"
    DIAGNOSTIC = "diagnosticProvider"


# --- Error Hierarchy ---


class LSPError(Exception):
    """Base exception for LSP client errors."""


class LSPCapabilityError(LSPError):
    """Raised when an operation requires a capability the server doesn't advertise."""

    def __init__(self, method: str, server_name: str | None = None) -> None:
        self.method = method
        self.server_name = server_name
        server_part = f" ({server_name})" if server_name else ""
        super().__init__(
            f"Server{server_part} does not advertise capability for: {method}"
        )


class LSPTransportError(LSPError):
    """Raised when the JSON-RPC transport fails (process exited, pipe broken, etc.)."""


class LSPTimeoutError(LSPError):
    """Raised when an LSP operation exceeds its timeout."""


# --- Response Dataclasses ---


@dataclass
class SymbolInfo:
    """A symbol returned by documentSymbol or workspaceSymbol."""

    name: str
    kind: int  # LSP SymbolKind enum value
    range_start_line: int
    range_start_char: int
    range_end_line: int
    range_end_char: int
    children: list[SymbolInfo] = field(default_factory=list)
    detail: str | None = None
    container_name: str | None = None


@dataclass
class Location:
    """A location in a file (definition, reference, implementation)."""

    uri: str
    range_start_line: int
    range_start_char: int
    range_end_line: int
    range_end_char: int


@dataclass
class HoverResult:
    """Hover information for a symbol."""

    contents: str  # Markdown content
    range_start_line: int | None = None
    range_start_char: int | None = None
    range_end_line: int | None = None
    range_end_char: int | None = None


@dataclass
class CallHierarchyItem:
    """An item in the call hierarchy."""

    name: str
    kind: int  # LSP SymbolKind
    uri: str
    range_start_line: int
    range_start_char: int
    range_end_line: int
    range_end_char: int
    selection_range_start_line: int
    selection_range_start_char: int
    selection_range_end_line: int
    selection_range_end_char: int
    detail: str | None = None
    # Raw LSP data preserved for two-step call hierarchy protocol
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Diagnostic:
    """A diagnostic message from the language server."""

    range_start_line: int
    range_start_char: int
    range_end_line: int
    range_end_char: int
    severity: int  # 1=Error, 2=Warning, 3=Info, 4=Hint
    message: str
    source: str | None = None
    code: str | int | None = None
