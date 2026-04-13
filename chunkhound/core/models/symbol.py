"""Symbol and edge data types for the graph intelligence layer.

These TypedDicts define the transport types for symbol/edge operations
between callers and DatabaseProvider implementations.
"""

from typing import TypedDict


class SymbolRow(TypedDict):
    """A symbol row for batch insertion into the symbols table.

    All fields are required. ``parent_fqn`` and ``type_signature`` may be
    ``None`` (e.g. top-level symbols or symbols whose hover failed) but the
    keys themselves must always be present so callers can rely on dict access.
    """

    fqn: str
    name: str
    kind: str
    language: str
    file_id: int
    file_path: str
    range_start: int
    range_end: int
    confidence: float
    lsp_server: str
    parent_fqn: str | None
    type_signature: str | None


class EdgeRow(TypedDict):
    """An edge row for batch insertion into the symbol_edges table.

    All fields are required.
    """

    from_symbol_id: int
    from_fqn: str
    from_file: str
    to_symbol_id: int
    to_fqn: str
    to_file: str
    edge_kind: str
    confidence: float
    lsp_server: str
