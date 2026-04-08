"""Tests for parser fixture capture/load utilities.

These utilities serialize tree-sitter parser output (list[Chunk]) to JSON
fixtures and load them back, enabling parser tests to run without tree-sitter.
"""

import json

import pytest

pytestmark = pytest.mark.unit

from chunkhound.core.models import Chunk
from chunkhound.core.types import ChunkType, FileId, Language, LineNumber
from tests.fixtures.parser_fixture_utils import (
    capture_parser_output,
    chunks_from_fixture,
    chunks_to_fixture,
    load_fixture,
    save_fixture,
)


def _sample_chunks() -> list[Chunk]:
    return [
        Chunk(
            symbol="MyClass",
            start_line=LineNumber(1),
            end_line=LineNumber(20),
            code="class MyClass:\n    pass",
            chunk_type=ChunkType.CLASS,
            file_id=FileId(1),
            language=Language.PYTHON,
            metadata={"visibility": "public"},
        ),
        Chunk(
            symbol="my_func",
            start_line=LineNumber(22),
            end_line=LineNumber(30),
            code="def my_func():\n    return 42",
            chunk_type=ChunkType.FUNCTION,
            file_id=FileId(1),
            language=Language.PYTHON,
        ),
    ]


class TestChunkSerialization:
    def test_roundtrip_preserves_symbol(self):
        chunks = _sample_chunks()
        data = chunks_to_fixture(chunks)
        restored = chunks_from_fixture(data)
        assert restored[0].symbol == "MyClass"
        assert restored[1].symbol == "my_func"

    def test_roundtrip_preserves_chunk_type(self):
        chunks = _sample_chunks()
        data = chunks_to_fixture(chunks)
        restored = chunks_from_fixture(data)
        assert restored[0].chunk_type == ChunkType.CLASS
        assert restored[1].chunk_type == ChunkType.FUNCTION

    def test_roundtrip_preserves_language(self):
        chunks = _sample_chunks()
        data = chunks_to_fixture(chunks)
        restored = chunks_from_fixture(data)
        assert restored[0].language == Language.PYTHON

    def test_roundtrip_preserves_line_numbers(self):
        chunks = _sample_chunks()
        data = chunks_to_fixture(chunks)
        restored = chunks_from_fixture(data)
        assert restored[0].start_line == 1
        assert restored[0].end_line == 20

    def test_roundtrip_preserves_code(self):
        chunks = _sample_chunks()
        data = chunks_to_fixture(chunks)
        restored = chunks_from_fixture(data)
        assert "class MyClass" in restored[0].code

    def test_roundtrip_preserves_metadata(self):
        chunks = _sample_chunks()
        data = chunks_to_fixture(chunks)
        restored = chunks_from_fixture(data)
        assert restored[0].metadata == {"visibility": "public"}
        assert restored[1].metadata is None

    def test_fixture_is_valid_json(self):
        chunks = _sample_chunks()
        data = chunks_to_fixture(chunks)
        parsed = json.loads(data)
        assert isinstance(parsed, list)
        assert len(parsed) == 2


class TestFileIO:
    def test_save_and_load(self, tmp_path):
        chunks = _sample_chunks()
        fixture_path = tmp_path / "chunks.json"
        save_fixture(chunks, fixture_path)
        assert fixture_path.exists()
        restored = load_fixture(fixture_path)
        assert len(restored) == 2
        assert restored[0].symbol == "MyClass"


class TestCaptureParserOutput:
    @pytest.mark.integration  # Calls tree-sitter
    def test_capture_writes_fixture(self, tmp_path):
        source = "def hello():\n    pass\n"
        fixture_path = tmp_path / "hello.json"
        chunks = capture_parser_output(
            source_code=source,
            language=Language.PYTHON,
            file_id=FileId(1),
            output_path=fixture_path,
        )
        assert fixture_path.exists()
        assert len(chunks) > 0
        # Verify the saved file can be loaded back
        loaded = load_fixture(fixture_path)
        assert len(loaded) == len(chunks)
