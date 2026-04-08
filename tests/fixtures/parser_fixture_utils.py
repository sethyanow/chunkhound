"""Parser fixture capture and load utilities.

Serialize tree-sitter parser output (list[Chunk]) to JSON fixture files
and load them back, enabling parser tests to run without tree-sitter at runtime.

Fixture boundary: Chunk dataclass output from parser.parse_file().
Tree-sitter AST nodes are C objects and not serializable — we capture at the
Chunk level, which is the parser's public output.
"""

import json
from pathlib import Path

from chunkhound.core.models import Chunk
from chunkhound.core.types import FileId, Language
from chunkhound.parsers.parser_factory import get_parser_factory

# Map Language to a representative file extension for temp file creation
_LANGUAGE_EXTENSIONS: dict[Language, str] = {
    Language.PYTHON: ".py",
    Language.JAVA: ".java",
    Language.CSHARP: ".cs",
    Language.TYPESCRIPT: ".ts",
    Language.JAVASCRIPT: ".js",
    Language.TSX: ".tsx",
    Language.JSX: ".jsx",
    Language.KOTLIN: ".kt",
    Language.GO: ".go",
    Language.RUST: ".rs",
    Language.C: ".c",
    Language.CPP: ".cpp",
    Language.BASH: ".sh",
    Language.MAKEFILE: ".mk",
    Language.PHP: ".php",
    Language.VUE: ".vue",
    Language.SVELTE: ".svelte",
    Language.ELIXIR: ".ex",
    Language.LUA: ".lua",
    Language.SWIFT: ".swift",
    Language.DART: ".dart",
    Language.MARKDOWN: ".md",
    Language.YAML: ".yaml",
    Language.JSON: ".json",
    Language.TOML: ".toml",
    Language.SQL: ".sql",
    Language.HCL: ".tf",
    Language.ZIG: ".zig",
    Language.HASKELL: ".hs",
    Language.GROOVY: ".groovy",
}


def chunks_to_fixture(chunks: list[Chunk]) -> str:
    """Serialize a list of Chunks to a JSON string."""
    return json.dumps([c.to_dict() for c in chunks], indent=2)


def chunks_from_fixture(data: str) -> list[Chunk]:
    """Deserialize a JSON string back to a list of Chunks."""
    return [Chunk.from_dict(d) for d in json.loads(data)]


def save_fixture(chunks: list[Chunk], path: Path) -> None:
    """Save chunks to a JSON fixture file."""
    path.write_text(chunks_to_fixture(chunks))


def load_fixture(path: Path) -> list[Chunk]:
    """Load chunks from a JSON fixture file."""
    return chunks_from_fixture(path.read_text())


def capture_parser_output(
    source_code: str,
    language: Language,
    file_id: FileId,
    output_path: Path,
    file_extension: str | None = None,
) -> list[Chunk]:
    """Run parser on source code, save output as fixture, return chunks.

    Args:
        source_code: The source code to parse.
        language: Language to use for parser selection.
        file_id: File ID to assign to parsed chunks.
        output_path: Path to write the JSON fixture file.
        file_extension: Optional file extension override (e.g. ".ex" for Elixir).

    Returns:
        The parsed chunks (also saved to output_path).
    """
    factory = get_parser_factory()
    parser = factory.create_parser(language)

    # Write source to a temp file with appropriate extension
    ext = file_extension or _LANGUAGE_EXTENSIONS.get(language, ".txt")
    temp_file = output_path.parent / f"_capture_source{ext}"
    temp_file.write_text(source_code)

    try:
        chunks = parser.parse_file(temp_file, file_id)
        save_fixture(chunks, output_path)
        return chunks
    finally:
        temp_file.unlink(missing_ok=True)
