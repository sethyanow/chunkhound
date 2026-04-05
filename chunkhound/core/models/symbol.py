"""Symbol and edge data types for the graph intelligence layer.

These TypedDicts define the transport types for symbol/edge operations
between callers and DatabaseProvider implementations.
"""

from typing import TypedDict


class SymbolRow(TypedDict, total=False):
    """A symbol row for batch insertion into the symbols table.

    Required fields match the symbols table schema. Optional fields
    (id, type_signature, parent_fqn) may be absent for new inserts.
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


class EdgeRow(TypedDict, total=False):
    """An edge row for batch insertion into the symbol_edges table.

    Required fields match the symbol_edges table schema.
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
