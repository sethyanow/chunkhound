"""LSP constants — shared mappings for the lsp package.

Zero chunkhound imports — stdlib only.
"""

from __future__ import annotations

# LSP SymbolKind enum value → human-readable name (LSP spec 3.17).
# Used by population service to store kind as TEXT in the symbols table.
SYMBOL_KIND: dict[int, str] = {
    1: "File",
    2: "Module",
    3: "Namespace",
    4: "Package",
    5: "Class",
    6: "Method",
    7: "Property",
    8: "Field",
    9: "Constructor",
    10: "Enum",
    11: "Interface",
    12: "Function",
    13: "Variable",
    14: "Constant",
    15: "String",
    16: "Number",
    17: "Boolean",
    18: "Array",
    19: "Object",
    20: "Key",
    21: "Null",
    22: "EnumMember",
    23: "Struct",
    24: "Event",
    25: "Operator",
    26: "TypeParameter",
}


def symbol_kind_name(kind: int) -> str:
    """Map LSP SymbolKind int to string, with fallback for unknown values."""
    return SYMBOL_KIND.get(kind, f"unknown_{kind}")
