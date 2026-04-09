"""Utilities for building safe SQL LIKE patterns."""

LIKE_ESCAPE_CHAR = "!"


def escape_like_pattern(value: str, *, escape_quotes: bool = False, escape_char: str = LIKE_ESCAPE_CHAR) -> str:
    """Escape SQL LIKE metacharacters for literal prefix/substring matching.

    Uses '!' as the escape character (paired with ESCAPE '!' in SQL).
    Avoids backslash which causes issues when SQL is round-tripped through sqlglot.
    Set escape_quotes=True when interpolating the value into a SQL string.
    """
    escaped = (
        value.replace(escape_char, escape_char + escape_char)
        .replace("%", escape_char + "%")
        .replace("_", escape_char + "_")
        .replace("[", escape_char + "[")
    )
    if escape_quotes:
        escaped = escaped.replace("'", "''")
    return escaped
