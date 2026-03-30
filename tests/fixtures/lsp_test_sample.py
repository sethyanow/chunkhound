"""Test fixture for LSP client integration tests.

Contains structures exercising all 8 LSP operations:
- documentSymbol: class, functions, variable
- definition/references: function call resolves to def
- hover: type-annotated variable shows type info
- incoming/outgoing calls: caller/callee chain
- implementation: abstract-like method
- diagnostics: deliberate type error
"""

from typing import Protocol


class Greeter(Protocol):
    """Protocol for greeters — tests go_to_implementation."""

    def greet(self, name: str) -> str: ...


class FriendlyGreeter:
    """Concrete implementation of Greeter protocol."""

    def greet(self, name: str) -> str:
        return format_greeting(name)


def format_greeting(name: str) -> str:
    """Format a greeting string — tests definition/references."""
    return f"Hello, {name}!"


def make_greeting(name: str) -> str:
    """Calls format_greeting — tests incoming/outgoing calls."""
    greeter = FriendlyGreeter()
    raw = greeter.greet(name)
    return raw.upper()


# Type-annotated variable — tests hover
result: str = make_greeting("World")

# Deliberate type error — tests diagnostics (pyright will flag this)
bad_value: int = "not an int"  # noqa: E501
