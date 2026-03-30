"""LSP Client Manager — standalone module with zero chunkhound imports.

Provides asyncio JSON-RPC over stdio transport, config-driven server registry,
capability gating, and connection pooling for language servers.
"""

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

__all__ = [
    "CallHierarchyItem",
    "Diagnostic",
    "HoverResult",
    "Location",
    "LSPCapability",
    "LSPCapabilityError",
    "LSPError",
    "LSPTransportError",
    "ServerConfig",
    "ServerState",
    "SymbolInfo",
]
