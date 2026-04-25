"""Fake provider implementations for testing without external dependencies.

These providers return deterministic, predictable responses for testing
the complete code research pipeline in CI/CD without external dependencies.

Includes:
- FakeLLMProvider: Scripted LLM responses
- FakeEmbeddingProvider: Deterministic hash-based embeddings
- ConstantEmbeddingProvider: Identical vectors for all inputs
- ValidatingEmbeddingProvider: Validates chunk size constraints
- FakeDatabaseProvider: Dict-backed DatabaseProvider protocol implementation
"""

import asyncio
import math
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import xxhash

from chunkhound.core.models import Chunk, Embedding, File
from chunkhound.core.models.symbol import EdgeRow, SymbolRow
from chunkhound.interfaces.embedding_provider import (
    EmbeddingConfig,
    EmbeddingTask,
    RerankResult,
)
from chunkhound.interfaces.llm_provider import LLMProvider, LLMResponse


class FakeLLMProvider(LLMProvider):
    """Fake LLM provider that returns scripted responses based on prompt patterns.

    Designed to test the full code research pipeline without real LLM API calls.
    Returns deterministic responses based on prompt content patterns.
    """

    def __init__(
        self,
        model: str = "fake-gpt",
        responses: dict[str, str] | None = None,
    ):
        """Initialize fake LLM provider.

        Args:
            model: Model name for identification
            responses: Optional dict mapping prompt substrings to responses
        """
        self._model = model
        self._requests_made = 0
        self._tokens_used = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0

        # Default responses for common patterns
        self._responses = responses or {
            "expand": "function definition, class implementation, code structure",
            "follow": (
                "1. How is search implemented?\n"
                "2. What are the key algorithms?\n"
                "3. How does data flow through the system?"
            ),
            "synthesis": (
                "## Overview\n"
                "The codebase implements semantic search"
                " with BFS traversal.\n\n"
                "## Key Components\n"
                "- Search service handles queries\n"
                "- Deep research coordinates BFS exploration\n"
                "- Database provider stores chunks\n\n"
                "## Data Flow\n"
                "Queries → Semantic search → Chunk retrieval"
                " → Smart boundaries → Synthesis"
            ),
            "code": "semantic search, deep research, database operations",
        }

    @property
    def name(self) -> str:
        """Provider name."""
        return "fake"

    @property
    def model(self) -> str:
        """Model name."""
        return self._model

    async def complete(
        self,
        prompt: str,
        system: str | None = None,
        max_completion_tokens: int = 4096,
        timeout: int | None = None,
    ) -> LLMResponse:
        """Generate a completion based on prompt patterns."""
        await asyncio.sleep(0.001)  # Simulate minimal latency

        self._requests_made += 1

        # Match prompt to response pattern
        prompt_lower = prompt.lower()
        response_content = "Default test response"

        for pattern, response in self._responses.items():
            if pattern in prompt_lower:
                response_content = response
                break

        # Estimate tokens
        prompt_tokens = self.estimate_tokens(prompt)
        if system:
            prompt_tokens += self.estimate_tokens(system)
        completion_tokens = self.estimate_tokens(response_content)
        total_tokens = prompt_tokens + completion_tokens

        self._prompt_tokens += prompt_tokens
        self._completion_tokens += completion_tokens
        self._tokens_used += total_tokens

        return LLMResponse(
            content=response_content,
            tokens_used=total_tokens,
            model=self._model,
            finish_reason="stop",
        )

    async def batch_complete(
        self,
        prompts: list[str],
        system: str | None = None,
        max_completion_tokens: int = 4096,
    ) -> list[LLMResponse]:
        """Generate completions for multiple prompts."""
        tasks = [
            self.complete(prompt, system, max_completion_tokens) for prompt in prompts
        ]
        return await asyncio.gather(*tasks)

    async def complete_structured(
        self,
        prompt: str,
        json_schema: dict[str, Any],
        system: str | None = None,
        max_completion_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Generate structured JSON response based on prompt patterns."""
        import json

        await asyncio.sleep(0.001)  # Simulate minimal latency

        self._requests_made += 1

        # Match prompt to response pattern
        prompt_lower = prompt.lower()
        response_content = '{"result": "default"}'

        for pattern, response in self._responses.items():
            if pattern in prompt_lower:
                response_content = response
                break

        # Estimate tokens
        prompt_tokens = self.estimate_tokens(prompt)
        if system:
            prompt_tokens += self.estimate_tokens(system)
        completion_tokens = self.estimate_tokens(response_content)
        total_tokens = prompt_tokens + completion_tokens

        self._prompt_tokens += prompt_tokens
        self._completion_tokens += completion_tokens
        self._tokens_used += total_tokens

        # Try to parse as JSON
        try:
            return json.loads(response_content)
        except json.JSONDecodeError:
            # Fallback to wrapped string
            return {"content": response_content}

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count (4 chars per token)."""
        return len(text) // 4

    async def health_check(self) -> dict[str, Any]:
        """Perform health check."""
        return {
            "status": "healthy",
            "provider": "fake",
            "model": self._model,
            "test_response": "OK",
        }

    def get_usage_stats(self) -> dict[str, Any]:
        """Get usage statistics."""
        return {
            "requests_made": self._requests_made,
            "total_tokens": self._tokens_used,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
        }


class FakeEmbeddingProvider:
    """Fake embedding provider that returns deterministic vectors.

    Generates consistent embeddings based on text content hash,
    allowing reproducible tests without API calls.

    NOTE: Because each text gets a unique hash-based vector, query embeddings
    won't match stored embeddings. For tests requiring semantic search matches,
    use ConstantEmbeddingProvider instead.
    """

    def __init__(
        self,
        model: str = "fake-embeddings",
        dims: int = 1536,
        batch_size: int = 100,
    ):
        """Initialize fake embedding provider.

        Args:
            model: Model name for identification
            dims: Embedding dimensions
            batch_size: Maximum batch size
        """
        self._model = model
        self._dims = dims
        self._batch_size = batch_size
        self._distance = "cosine"
        self._max_tokens = 8192

        # Usage tracking
        self._requests_made = 0
        self._tokens_used = 0
        self._embeddings_generated = 0

    @property
    def name(self) -> str:
        """Provider name."""
        return "fake"

    @property
    def model(self) -> str:
        """Model name."""
        return self._model

    @property
    def dims(self) -> int:
        """Embedding dimensions."""
        return self._dims

    @property
    def distance(self) -> str:
        """Distance metric."""
        return self._distance

    @property
    def batch_size(self) -> int:
        """Maximum batch size."""
        return self._batch_size

    @property
    def max_tokens(self) -> int:
        """Maximum tokens per request."""
        return self._max_tokens

    @property
    def config(self) -> EmbeddingConfig:
        """Provider configuration."""
        return EmbeddingConfig(
            provider="fake",
            model=self._model,
            dims=self._dims,
            distance=self._distance,
            batch_size=self._batch_size,
            max_tokens=self._max_tokens,
        )

    def _generate_deterministic_vector(self, text: str) -> list[float]:
        """Generate deterministic embedding via character n-gram feature hashing.

        Hashes character n-grams (3, 4, 5-grams) to dimension indices,
        accumulates with length-based weights, applies log-saturation,
        and L2-normalizes. Produces vectors where texts sharing substrings
        (identifiers, keywords) have high cosine similarity.
        """
        dims = self._dims
        vector = [0.0] * dims

        words = text.lower().split()
        for word in words:
            padded = f"^{word}$"
            for n in (3, 4, 5):
                if len(padded) < n:
                    continue
                weight = {3: 1.0, 4: 5.0, 5: 10.0}[n]
                for i in range(len(padded) - n + 1):
                    ngram = padded[i : i + n]
                    idx = xxhash.xxh3_64_intdigest(ngram.encode()) % dims
                    vector[idx] += weight

        # Log-saturation (BM25-style diminishing returns)
        vector = [math.log1p(v) for v in vector]

        # L2-normalize
        magnitude = math.sqrt(sum(v * v for v in vector))
        if magnitude > 0:
            vector = [v / magnitude for v in vector]

        return vector

    async def embed(
        self, texts: list[str], task: EmbeddingTask = None
    ) -> list[list[float]]:
        """Generate embeddings for a list of texts.

        ``task`` is accepted for protocol compatibility (ch-agj) but the
        fake provider's deterministic vector generation does not vary
        based on it.
        """
        if not texts:
            return []

        await asyncio.sleep(0.001)  # Simulate minimal latency

        self._requests_made += 1
        self._embeddings_generated += len(texts)
        self._tokens_used += sum(self.estimate_tokens(text) for text in texts)

        return [self._generate_deterministic_vector(text) for text in texts]

    async def embed_single(
        self, text: str, task: EmbeddingTask = None
    ) -> list[float]:
        """Generate embedding for a single text."""
        embeddings = await self.embed([text], task=task)
        return embeddings[0]

    async def embed_batch(
        self,
        texts: list[str],
        batch_size: int | None = None,
        task: EmbeddingTask = None,
    ) -> list[list[float]]:
        """Generate embeddings in batches."""
        if not texts:
            return []

        batch_size = batch_size or self._batch_size
        batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]

        all_embeddings = []
        for batch in batches:
            embeddings = await self.embed(batch, task=task)
            all_embeddings.extend(embeddings)

        return all_embeddings

    async def embed_streaming(
        self, texts: list[str], task: EmbeddingTask = None
    ) -> AsyncIterator[list[float]]:
        """Generate embeddings with streaming results."""
        for text in texts:
            embedding = await self.embed_single(text, task=task)
            yield embedding

    async def initialize(self) -> None:
        """Initialize the embedding provider."""
        pass

    async def shutdown(self) -> None:
        """Shutdown the embedding provider."""
        pass

    def is_available(self) -> bool:
        """Check if provider is available."""
        return True

    async def health_check(self) -> dict[str, Any]:
        """Perform health check."""
        return {
            "status": "healthy",
            "provider": "fake",
            "model": self._model,
            "dimensions": self._dims,
            "requests_made": self._requests_made,
            "tokens_used": self._tokens_used,
            "embeddings_generated": self._embeddings_generated,
        }

    def validate_texts(self, texts: list[str]) -> list[str]:
        """Validate and preprocess texts."""
        return [text if text else " " for text in texts]

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count (3 chars per token for embeddings)."""
        return max(1, len(text) // 3)

    def chunk_text_by_tokens(self, text: str, max_tokens: int) -> list[str]:
        """Split text into chunks by token count."""
        chars_per_chunk = max_tokens * 3
        chunks = []
        for i in range(0, len(text), chars_per_chunk):
            chunks.append(text[i : i + chars_per_chunk])
        return chunks

    def get_model_info(self) -> dict[str, Any]:
        """Get information about the embedding model."""
        return {
            "provider": "fake",
            "model": self._model,
            "dimensions": self._dims,
            "max_tokens": self._max_tokens,
            "supports_reranking": True,
        }

    def get_usage_stats(self) -> dict[str, Any]:
        """Get usage statistics."""
        return {
            "requests_made": self._requests_made,
            "tokens_used": self._tokens_used,
            "embeddings_generated": self._embeddings_generated,
        }

    def reset_usage_stats(self) -> None:
        """Reset usage statistics."""
        self._requests_made = 0
        self._tokens_used = 0
        self._embeddings_generated = 0

    def update_config(self, **kwargs: Any) -> None:
        """Update provider configuration."""
        if "model" in kwargs:
            self._model = kwargs["model"]
        if "batch_size" in kwargs:
            self._batch_size = kwargs["batch_size"]

    def get_supported_distances(self) -> list[str]:
        """Get list of supported distance metrics."""
        return ["cosine", "l2", "ip"]

    def get_optimal_batch_size(self) -> int:
        """Get optimal batch size."""
        return min(self._batch_size, 100)

    def get_max_tokens_per_batch(self) -> int:
        """Get maximum tokens per batch."""
        return 320000

    def get_max_documents_per_batch(self) -> int:
        """Get maximum documents per batch."""
        return 1000

    def get_max_rerank_batch_size(self) -> int:
        """Get maximum documents per batch for reranking operations."""
        return 1000

    def get_recommended_concurrency(self) -> int:
        """Get recommended concurrency."""
        return 10

    def get_chars_to_tokens_ratio(self) -> float:
        """Get character-to-token ratio."""
        return 3.0

    # Reranking Operations
    def supports_reranking(self) -> bool:
        """Fake provider supports reranking."""
        return True

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Extract lowercase terms longer than 2 characters."""
        return {w for w in text.lower().split() if len(w) > 2}

    async def rerank(
        self, query: str, documents: list[str], top_k: int | None = None
    ) -> list[RerankResult]:
        """Rerank documents using hybrid term-overlap + hash-cosine scoring.

        Score components (range [0.0, 1.0]):
          - Term overlap  (0.5 weight): fraction of query terms found in doc terms
          - Substring match (0.3 weight): fraction of query terms found as
            substrings in the document (catches compound identifiers)
          - Hash cosine    (0.2 weight): deterministic tie-breaker mapped to [0, 0.2]
        """
        if not documents:
            return []

        await asyncio.sleep(0.001)  # Simulate minimal latency

        self._requests_made += 1

        query_terms = self._tokenize(query)
        query_vector = self._generate_deterministic_vector(query)
        results = []

        for idx, doc in enumerate(documents):
            doc_lower = doc.lower()
            doc_terms = self._tokenize(doc)

            # Term overlap: exact token match
            if query_terms:
                term_overlap = len(query_terms & doc_terms) / len(query_terms)
            else:
                term_overlap = 0.0

            # Substring match: query term appears anywhere in doc text
            if query_terms:
                substr_hits = sum(1 for t in query_terms if t in doc_lower)
                substr_score = substr_hits / len(query_terms)
            else:
                substr_score = 0.0

            # Hash cosine: deterministic tie-breaker mapped from [-1,1] to [0,1]
            doc_vector = self._generate_deterministic_vector(doc)
            cosine = sum(a * b for a, b in zip(query_vector, doc_vector))
            hash_score = (cosine + 1.0) / 2.0  # [0, 1]

            score = 0.5 * term_overlap + 0.3 * substr_score + 0.2 * hash_score
            results.append(RerankResult(index=idx, score=score))

        # Sort by score descending
        results.sort(key=lambda x: x.score, reverse=True)

        # Apply top_k if specified
        if top_k is not None:
            results = results[:top_k]

        return results


class ConstantEmbeddingProvider(FakeEmbeddingProvider):
    """Embedding provider that returns identical vectors for all inputs.

    Use this for tests that require semantic search to find matches, since
    any query will match any stored embedding with perfect similarity.
    """

    def _generate_deterministic_vector(self, text: str) -> list[float]:
        """Return constant unit vector (all components equal)."""
        value = 1.0 / (self._dims**0.5)
        return [value] * self._dims


class ValidatingEmbeddingProvider(FakeEmbeddingProvider):
    """Embedding provider that validates chunk size constraints.

    Intercepts all texts sent to embed() and validates they conform
    to the min/max chunk size constraints:
    - max_chunk_size: 1200 non-whitespace chars
    - min_chunk_size: 25 non-whitespace chars (soft threshold)
    - safe_token_limit: 6000 tokens (estimated as len(text) // 3)

    Use this for e2e tests that verify all parsers respect chunk size limits.
    """

    def __init__(
        self,
        max_chunk_size: int = 1200,
        min_chunk_size: int = 25,
        safe_token_limit: int = 6000,
        **kwargs: Any,
    ):
        """Initialize validating embedding provider.

        Args:
            max_chunk_size: Maximum non-whitespace chars per chunk (default 1200)
            min_chunk_size: Soft threshold for suspiciously small chunks (default 25)
            safe_token_limit: Maximum estimated tokens per chunk (default 6000)
            **kwargs: Arguments passed to FakeEmbeddingProvider
        """
        super().__init__(**kwargs)
        self.max_chunk_size = max_chunk_size
        self.min_chunk_size = min_chunk_size
        self.safe_token_limit = safe_token_limit
        self.violations: list[dict[str, Any]] = []
        self.all_texts: list[str] = []
        self.chunk_stats: dict[str, Any] = {
            "total": 0,
            "min_size": float("inf"),
            "max_size": 0,
            "min_tokens": float("inf"),
            "max_tokens": 0,
        }

    def _extract_language_from_header(self, text: str) -> str | None:
        """Extract language from embedding header if present.

        Header format: "# path/to/file.py (python)\n"
        Returns language string (lowercase) or None if not found.
        """
        if not text.startswith("# "):
            return None
        # Look for language in parentheses within first 200 chars
        match = re.search(r"\((\w+)\)\n", text[:200])
        return match.group(1).lower() if match else None

    async def embed(
        self, texts: list[str], task: EmbeddingTask = None
    ) -> list[list[float]]:
        """Generate embeddings while validating chunk size constraints.

        ``task`` accepted for protocol compatibility (ch-agj); validation
        logic does not depend on it.
        """
        for text in texts:
            self.all_texts.append(text)
            # Measure non-whitespace chars (same as ChunkMetrics.from_content)
            non_ws_chars = len(re.sub(r"\s", "", text))
            # Estimate tokens (conservative: len // 3)
            estimated_tokens = len(text) // 3
            # Extract language from header for violation tracking
            language = self._extract_language_from_header(text)

            # Update stats
            self.chunk_stats["total"] += 1
            self.chunk_stats["min_size"] = min(
                self.chunk_stats["min_size"], non_ws_chars
            )
            self.chunk_stats["max_size"] = max(
                self.chunk_stats["max_size"], non_ws_chars
            )
            self.chunk_stats["min_tokens"] = min(
                self.chunk_stats["min_tokens"], estimated_tokens
            )
            self.chunk_stats["max_tokens"] = max(
                self.chunk_stats["max_tokens"], estimated_tokens
            )

            # Validate max_chunk_size (non-whitespace chars)
            if non_ws_chars > self.max_chunk_size:
                self.violations.append(
                    {
                        "type": "max_chars_exceeded",
                        "non_ws_chars": non_ws_chars,
                        "limit": self.max_chunk_size,
                        "text_preview": text[:300],
                        "language": language,
                    }
                )

            # Validate safe_token_limit
            if estimated_tokens > self.safe_token_limit:
                self.violations.append(
                    {
                        "type": "max_tokens_exceeded",
                        "estimated_tokens": estimated_tokens,
                        "limit": self.safe_token_limit,
                        "text_preview": text[:300],
                        "language": language,
                    }
                )

            # Soft threshold: flag suspiciously small chunks
            if non_ws_chars < self.min_chunk_size:
                self.violations.append(
                    {
                        "type": "suspiciously_small",
                        "non_ws_chars": non_ws_chars,
                        "threshold": self.min_chunk_size,
                        "text_preview": text[:300],
                        "language": language,
                    }
                )

        return await super().embed(texts)

    def get_violations_by_type(self, violation_type: str) -> list[dict[str, Any]]:
        """Get all violations of a specific type."""
        return [v for v in self.violations if v["type"] == violation_type]

    def get_violations_by_languages(
        self, violation_type: str, languages: set[str]
    ) -> list[dict[str, Any]]:
        """Get violations of a type from specific languages (lowercase names)."""
        return [
            v
            for v in self.violations
            if v["type"] == violation_type and v.get("language") in languages
        ]

    def get_violations_excluding_languages(
        self, violation_type: str, excluded_languages: set[str]
    ) -> list[dict[str, Any]]:
        """Get violations of a type excluding specific languages (lowercase names)."""
        return [
            v
            for v in self.violations
            if v["type"] == violation_type
            and v.get("language") not in excluded_languages
        ]

    def reset_tracking(self) -> None:
        """Reset all tracking data."""
        self.violations = []
        self.all_texts = []
        self.chunk_stats = {
            "total": 0,
            "min_size": float("inf"),
            "max_size": 0,
            "min_tokens": float("inf"),
            "max_tokens": 0,
        }


class FakeDatabaseProvider:
    """Dict-backed DatabaseProvider protocol implementation for unit tests.

    Replaces real DuckDB/LanceDB providers in tests that need to verify
    application logic without touching a real database. Stores all data
    in Python dicts with auto-incrementing integer IDs.

    Same pattern as FakeEmbeddingProvider / FakeLLMProvider above.
    """

    def __init__(self, base_directory: str = "/fake/project") -> None:
        self._base_directory = Path(base_directory)
        self._connected = False

        # Auto-increment counters
        self._next_file_id = 1
        self._next_chunk_id = 1
        self._next_embedding_id = 1
        self._next_symbol_id = 1

        # Storage: id → dict
        self._files: dict[int, dict[str, Any]] = {}
        self._chunks: dict[int, dict[str, Any]] = {}
        self._embeddings: dict[int, dict[str, Any]] = {}
        self._symbols: dict[int, dict[str, Any]] = {}
        self._edges: list[dict[str, Any]] = []

        # Path index for fast file lookup
        self._file_path_index: dict[str, int] = {}

        # Transaction state
        self._in_transaction = False

    # --- Properties ---

    @property
    def db_path(self) -> Path | str:
        return ":memory:"

    def get_base_directory(self) -> Path:
        return self._base_directory

    @property
    def is_connected(self) -> bool:
        return self._connected

    # --- Connection Management ---

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    # --- Schema Management ---

    def create_schema(self) -> None:
        pass

    def create_indexes(self) -> None:
        pass

    def create_vector_index(
        self, provider: str, model: str, dims: int, metric: str = "cosine"
    ) -> None:
        pass

    def drop_vector_index(
        self, provider: str, model: str, dims: int, metric: str = "cosine"
    ) -> str:
        return f"dropped:{provider}/{model}/{dims}"

    # --- File Operations ---

    def insert_file(self, file: File) -> int:
        file_id = self._next_file_id
        self._next_file_id += 1
        record = file.to_dict()
        record["id"] = file_id
        self._files[file_id] = record
        self._file_path_index[file.path] = file_id
        return file_id

    def get_file_by_path(
        self, path: str, as_model: bool = False
    ) -> dict[str, Any] | File | None:
        file_id = self._file_path_index.get(path)
        if file_id is None:
            return None
        return self._return_file(file_id, as_model)

    def get_file_by_id(
        self, file_id: int, as_model: bool = False
    ) -> dict[str, Any] | File | None:
        if file_id not in self._files:
            return None
        return self._return_file(file_id, as_model)

    def _return_file(self, file_id: int, as_model: bool) -> dict[str, Any] | File:
        record = dict(self._files[file_id])
        if as_model:
            return File.from_dict(record)
        return record

    def update_file(self, file_id: int, **kwargs: Any) -> None:
        if file_id in self._files:
            self._files[file_id].update(kwargs)

    def delete_file_completely(self, file_path: str) -> bool:
        file_id = self._file_path_index.get(file_path)
        if file_id is None:
            return False
        # Remove chunks for this file
        chunk_ids_to_remove = [
            cid for cid, c in self._chunks.items() if c.get("file_id") == file_id
        ]
        for cid in chunk_ids_to_remove:
            # Also remove embeddings for these chunks
            self._remove_embeddings_for_chunk(cid)
            del self._chunks[cid]
        # Remove symbols and edges for this file
        sym_ids_to_remove = [
            sid for sid, s in self._symbols.items() if s.get("file_id") == file_id
        ]
        for sid in sym_ids_to_remove:
            del self._symbols[sid]
        self._edges = [
            e
            for e in self._edges
            if e.get("from_symbol_id") not in sym_ids_to_remove
            and e.get("to_symbol_id") not in sym_ids_to_remove
        ]
        # Remove file
        del self._files[file_id]
        del self._file_path_index[file_path]
        return True

    async def delete_file_completely_async(self, file_path: str) -> bool:
        return self.delete_file_completely(file_path)

    async def insert_file_async(self, file: File) -> int:
        return self.insert_file(file)

    async def get_file_by_path_async(
        self, path: str, as_model: bool = False
    ) -> dict[str, Any] | File | None:
        return self.get_file_by_path(path, as_model)

    async def update_file_async(self, file_id: int, **kwargs: Any) -> None:
        self.update_file(file_id, **kwargs)

    # --- Chunk Operations ---

    def insert_chunk(self, chunk: Chunk) -> int:
        chunk_id = self._next_chunk_id
        self._next_chunk_id += 1
        record = chunk.to_dict()
        record["id"] = chunk_id
        # Ensure file_path is set from the file record if not on chunk
        if "file_path" not in record or record["file_path"] is None:
            file_rec = self._files.get(chunk.file_id)
            if file_rec:
                record["file_path"] = file_rec["path"]
        self._chunks[chunk_id] = record
        return chunk_id

    def insert_chunks_batch(self, chunks: list[Chunk]) -> list[int]:
        return [self.insert_chunk(c) for c in chunks]

    def get_chunk_by_id(
        self, chunk_id: int, as_model: bool = False
    ) -> dict[str, Any] | Chunk | None:
        if chunk_id not in self._chunks:
            return None
        record = dict(self._chunks[chunk_id])
        if as_model:
            return Chunk.from_dict(record)
        return record

    def get_chunks_by_file_id(
        self, file_id: int, as_model: bool = False
    ) -> list[dict[str, Any] | Chunk]:
        matched = [
            dict(c) for c in self._chunks.values() if c.get("file_id") == file_id
        ]
        if as_model:
            return [Chunk.from_dict(r) for r in matched]
        # Protocol requires list[dict | Chunk]; build with correct union type
        out: list[dict[str, Any] | Chunk] = list(matched)
        return out

    async def get_chunks_by_file_id_async(
        self, file_id: int, as_model: bool = False
    ) -> list[dict[str, Any] | Chunk]:
        return self.get_chunks_by_file_id(file_id, as_model)

    async def insert_chunks_batch_async(self, chunks: list[Chunk]) -> list[int]:
        return self.insert_chunks_batch(chunks)

    async def delete_chunks_batch_async(self, chunk_ids: list[int]) -> None:
        self.delete_chunks_batch(chunk_ids)

    def delete_file_chunks(self, file_id: int) -> None:
        to_remove = [
            cid for cid, c in self._chunks.items() if c.get("file_id") == file_id
        ]
        for cid in to_remove:
            self._remove_embeddings_for_chunk(cid)
            del self._chunks[cid]

    def delete_chunks_batch(self, chunk_ids: list[int]) -> None:
        for cid in chunk_ids:
            if cid in self._chunks:
                self._remove_embeddings_for_chunk(cid)
                del self._chunks[cid]

    def delete_chunk(self, chunk_id: int) -> None:
        if chunk_id in self._chunks:
            self._remove_embeddings_for_chunk(chunk_id)
            del self._chunks[chunk_id]

    def update_chunk(self, chunk_id: int, **kwargs: Any) -> None:
        if chunk_id in self._chunks:
            self._chunks[chunk_id].update(kwargs)

    # --- Embedding Operations ---

    def insert_embedding(self, embedding: Embedding) -> int:
        emb_id = self._next_embedding_id
        self._next_embedding_id += 1
        record = embedding.to_dict()
        record["id"] = emb_id
        self._embeddings[emb_id] = record
        return emb_id

    def insert_embeddings_batch(
        self,
        embeddings_data: list[dict],
        batch_size: int | None = None,
        connection: Any = None,
    ) -> int:
        count = 0
        for emb_data in embeddings_data:
            emb_id = self._next_embedding_id
            self._next_embedding_id += 1
            emb_data_copy = dict(emb_data)
            emb_data_copy["id"] = emb_id
            self._embeddings[emb_id] = emb_data_copy
            count += 1
        return count

    def get_embedding_by_chunk_id(
        self, chunk_id: int, provider: str, model: str
    ) -> Embedding | None:
        for emb in self._embeddings.values():
            if (
                emb.get("chunk_id") == chunk_id
                and emb.get("provider") == provider
                and emb.get("model") == model
            ):
                return Embedding.from_dict(emb)
        return None

    def get_existing_embeddings(
        self, chunk_ids: list[int], provider: str, model: str
    ) -> set[int]:
        return {
            emb["chunk_id"]
            for emb in self._embeddings.values()
            if emb.get("chunk_id") in chunk_ids
            and emb.get("provider") == provider
            and emb.get("model") == model
        }

    def delete_embeddings_by_chunk_id(self, chunk_id: int) -> None:
        self._remove_embeddings_for_chunk(chunk_id)

    def _remove_embeddings_for_chunk(self, chunk_id: int) -> None:
        to_remove = [
            eid
            for eid, e in self._embeddings.items()
            if e.get("chunk_id") == chunk_id
        ]
        for eid in to_remove:
            del self._embeddings[eid]

    def get_all_chunks_with_metadata(self) -> list[dict[str, Any]]:
        results = []
        for chunk in self._chunks.values():
            record = dict(chunk)
            file_id = chunk.get("file_id")
            if file_id and file_id in self._files:
                record["file_path"] = self._files[file_id].get("path")
            results.append(record)
        return results

    # --- Search Operations ---

    def search_semantic(
        self,
        query_embedding: list[float],
        provider: str,
        model: str,
        page_size: int = 10,
        offset: int = 0,
        threshold: float | None = None,
        path_filter: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # Simple: return all chunks that have embeddings, scored by dot product
        scored: list[tuple[float, dict[str, Any]]] = []
        for emb in self._embeddings.values():
            if emb.get("provider") != provider or emb.get("model") != model:
                continue
            chunk_id = emb.get("chunk_id")
            if chunk_id not in self._chunks:
                continue
            chunk = dict(self._chunks[chunk_id])
            # Apply path filter
            file_path = chunk.get("file_path", "")
            if path_filter and not file_path.startswith(path_filter):
                continue
            # Compute cosine similarity
            vec = emb.get("vector", [])
            if vec and query_embedding:
                dot = sum(a * b for a, b in zip(query_embedding, vec))
                mag_q = sum(x * x for x in query_embedding) ** 0.5
                mag_v = sum(x * x for x in vec) ** 0.5
                score = dot / (mag_q * mag_v) if mag_q > 0 and mag_v > 0 else 0.0
            else:
                score = 0.0
            if threshold is not None and score < threshold:
                continue
            chunk["score"] = score
            scored.append((score, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [item[1] for item in scored[offset : offset + page_size]]
        meta = {
            "total": len(scored),
            "page_size": page_size,
            "offset": offset,
        }
        return results, meta

    def find_similar_chunks(
        self,
        chunk_id: int,
        provider: str,
        model: str,
        limit: int = 10,
        threshold: float | None = None,
        path_filter: str | None = None,
        fuzzy_path: bool = False,
    ) -> list[dict[str, Any]]:
        # Find the embedding for this chunk
        emb = self.get_embedding_by_chunk_id(chunk_id, provider, model)
        if emb is None:
            return []
        results, _ = self.search_semantic(
            emb.vector, provider, model, page_size=limit + 1,
            threshold=threshold, path_filter=path_filter,
        )
        # Exclude the seed chunk itself
        return [r for r in results if r.get("id") != chunk_id][:limit]

    def search_by_embedding(
        self,
        query_embedding: list[float],
        provider: str,
        model: str,
        limit: int = 10,
        threshold: float | None = None,
        path_filter: str | None = None,
        fuzzy_path: bool = False,
    ) -> list[dict[str, Any]]:
        results, _ = self.search_semantic(
            query_embedding, provider, model, page_size=limit,
            threshold=threshold, path_filter=path_filter,
        )
        return results

    def search_regex(
        self,
        pattern: str,
        page_size: int = 10,
        offset: int = 0,
        path_filter: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        compiled = re.compile(pattern)
        matched: list[dict[str, Any]] = []
        for chunk in self._chunks.values():
            file_path = chunk.get("file_path", "")
            if path_filter and not file_path.startswith(path_filter):
                continue
            code = chunk.get("code", "")
            if compiled.search(code):
                matched.append(dict(chunk))
        results = matched[offset : offset + page_size]
        meta = {"total": len(matched), "page_size": page_size, "offset": offset}
        return results, meta

    async def search_regex_async(
        self,
        pattern: str,
        page_size: int = 10,
        offset: int = 0,
        path_filter: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self.search_regex(pattern, page_size, offset, path_filter)

    def search_text(
        self, query: str, page_size: int = 10, offset: int = 0
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        query_lower = query.lower()
        matched: list[dict[str, Any]] = []
        for chunk in self._chunks.values():
            code = chunk.get("code", "").lower()
            if query_lower in code:
                matched.append(dict(chunk))
        results = matched[offset : offset + page_size]
        meta = {"total": len(matched), "page_size": page_size, "offset": offset}
        return results, meta

    def get_chunks_in_range(
        self, file_id: int, start_line: int, end_line: int
    ) -> list[dict[str, Any]]:
        results = []
        for chunk in self._chunks.values():
            if chunk.get("file_id") != file_id:
                continue
            c_start = chunk.get("start_line", 0)
            c_end = chunk.get("end_line", 0)
            # Overlap check
            if c_start <= end_line and c_end >= start_line:
                results.append(dict(chunk))
        return results

    # --- Statistics ---

    def get_stats(self) -> dict[str, int]:
        return {
            "total_files": len(self._files),
            "total_chunks": len(self._chunks),
            "total_embeddings": len(self._embeddings),
        }

    async def get_stats_async(self) -> dict[str, int]:
        return self.get_stats()

    def get_file_stats(self, file_id: int) -> dict[str, Any]:
        chunks = self.get_chunks_by_file_id(file_id)
        return {"file_id": file_id, "chunk_count": len(chunks)}

    def get_provider_stats(self, provider: str, model: str) -> dict[str, Any]:
        count = sum(
            1
            for e in self._embeddings.values()
            if e.get("provider") == provider and e.get("model") == model
        )
        return {"provider": provider, "model": model, "embedding_count": count}

    # --- Transaction Operations ---

    def execute_query(
        self, query: str, params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        # FakeDatabaseProvider does not interpret SQL. Tests that use
        # execute_query with raw SQL must be rewritten to use the typed
        # API methods (insert_file, insert_chunk, etc.) when converting
        # to unit tests. This is intentional — the fake replaces the
        # database engine, not the SQL language.
        raise NotImplementedError(
            "FakeDatabaseProvider does not support raw SQL via execute_query. "
            "Rewrite the test to use typed API methods (insert_file, insert_chunk, etc.)."
        )

    def begin_transaction(self) -> None:
        self._in_transaction = True

    def commit_transaction(self, force_checkpoint: bool = False) -> None:
        self._in_transaction = False

    def rollback_transaction(self) -> None:
        self._in_transaction = False

    async def begin_transaction_async(self) -> None:
        self.begin_transaction()

    async def commit_transaction_async(self, force_checkpoint: bool = False) -> None:
        self.commit_transaction(force_checkpoint)

    async def rollback_transaction_async(self) -> None:
        self.rollback_transaction()

    # --- File Processing (no-ops for fake) ---

    async def process_file(
        self, file_path: Path, skip_embeddings: bool = False
    ) -> dict[str, Any]:
        return {"status": "skipped", "file": str(file_path)}

    async def process_directory(
        self,
        directory: Path,
        patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        return {"status": "skipped", "directory": str(directory)}

    # --- Health & Optimization ---

    def optimize_tables(self) -> None:
        pass

    def should_optimize(self, operation: str = "") -> bool:
        return False

    def health_check(self) -> dict[str, Any]:
        return {"status": "healthy", "provider": "fake"}

    def get_connection_info(self) -> dict[str, Any]:
        return {"provider": "fake", "connected": self._connected}

    # --- Symbol/Edge CRUD ---

    def insert_symbols_batch(self, symbols: list[SymbolRow]) -> None:
        for sym in symbols:
            sym_id = self._next_symbol_id
            self._next_symbol_id += 1
            record = dict(sym)
            record["id"] = sym_id
            self._symbols[sym_id] = record

    def delete_symbols_by_file(self, file_id: int) -> None:
        to_remove = [
            sid for sid, s in self._symbols.items() if s.get("file_id") == file_id
        ]
        for sid in to_remove:
            del self._symbols[sid]

    def delete_edges_by_file(self, file_id: int) -> None:
        sym_ids = {
            sid for sid, s in self._symbols.items() if s.get("file_id") == file_id
        }
        self._edges = [
            e
            for e in self._edges
            if e.get("from_symbol_id") not in sym_ids
            and e.get("to_symbol_id") not in sym_ids
        ]

    def query_symbols_by_file(self, file_id: int) -> list[dict[str, Any]]:
        return [
            dict(s) for s in self._symbols.values() if s.get("file_id") == file_id
        ]

    def query_symbols_by_range(
        self, file_path: str, line: int
    ) -> dict[str, Any] | None:
        # Find file_id from path
        file_id = self._file_path_index.get(file_path)
        if file_id is None:
            return None
        candidates = []
        for s in self._symbols.values():
            if s.get("file_id") != file_id:
                continue
            r_start = s.get("range_start", 0)
            r_end = s.get("range_end", 0)
            if r_start <= line <= r_end:
                candidates.append(dict(s))
        if not candidates:
            return None
        # Return innermost (smallest range)
        candidates.sort(key=lambda c: c.get("range_end", 0) - c.get("range_start", 0))
        return candidates[0]

    def query_symbols_by_range_overlap(
        self, file_path: str, min_line: int, max_line: int
    ) -> list[dict[str, Any]]:
        file_id = self._file_path_index.get(file_path)
        if file_id is None:
            return []
        results = []
        for s in self._symbols.values():
            if s.get("file_id") != file_id:
                continue
            r_start = s.get("range_start", 0)
            r_end = s.get("range_end", 0)
            if r_start <= max_line and r_end >= min_line:
                results.append(dict(s))
        return results

    def query_symbol_fqns_by_file(self, file_id: int) -> dict[str, int]:
        return {
            s["fqn"]: s["id"]
            for s in self._symbols.values()
            if s.get("file_id") == file_id and "fqn" in s and "id" in s
        }

    def query_symbols_by_fqn_exists(self, fqn: str, file_path: str) -> bool:
        file_id = self._file_path_index.get(file_path)
        if file_id is None:
            return False
        return any(
            s.get("fqn") == fqn and s.get("file_id") == file_id
            for s in self._symbols.values()
        )

    def insert_edges_batch(self, edges: list[EdgeRow]) -> None:
        for edge in edges:
            self._edges.append(dict(edge))

    # Async symbol/edge variants

    async def insert_symbols_batch_async(self, symbols: list[SymbolRow]) -> None:
        self.insert_symbols_batch(symbols)

    async def delete_symbols_by_file_async(self, file_id: int) -> None:
        self.delete_symbols_by_file(file_id)

    async def delete_edges_by_file_async(self, file_id: int) -> None:
        self.delete_edges_by_file(file_id)

    async def query_symbols_by_file_async(self, file_id: int) -> list[dict[str, Any]]:
        return self.query_symbols_by_file(file_id)

    async def query_symbol_fqns_by_file_async(self, file_id: int) -> dict[str, int]:
        return self.query_symbol_fqns_by_file(file_id)

    async def insert_edges_batch_async(self, edges: list[EdgeRow]) -> None:
        self.insert_edges_batch(edges)

    # --- Graph Query Operations ---

    def graph_walk(
        self,
        seed_fqns: list[str],
        depth: int,
        directed: bool,
        edge_kind: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # BFS walk from seed FQNs
        visited_fqns: set[str] = set()
        result_nodes: list[dict[str, Any]] = []
        result_edges: list[dict[str, Any]] = []
        frontier = set(seed_fqns)

        for _d in range(depth + 1):
            next_frontier: set[str] = set()
            for fqn in frontier:
                if fqn in visited_fqns:
                    continue
                visited_fqns.add(fqn)
                # Find symbol node
                for s in self._symbols.values():
                    if s.get("fqn") == fqn:
                        result_nodes.append(dict(s))
                        break
                if len(result_nodes) >= limit:
                    break
                # Find edges
                for e in self._edges:
                    if edge_kind and e.get("edge_kind") != edge_kind:
                        continue
                    if e.get("from_fqn") == fqn:
                        result_edges.append(dict(e))
                        next_frontier.add(e["to_fqn"])
                    elif not directed and e.get("to_fqn") == fqn:
                        result_edges.append(dict(e))
                        next_frontier.add(e["from_fqn"])
            if len(result_nodes) >= limit:
                break
            frontier = next_frontier - visited_fqns

        return result_nodes[:limit], result_edges

    def graph_reachability(self, scope: str) -> list[dict[str, Any]]:
        # Collect symbols in scope
        scope_syms = {
            s["fqn"]: dict(s)
            for s in self._symbols.values()
            if s.get("file_path", "").startswith(scope)
        }
        if not scope_syms:
            return []
        scope_fqns = set(scope_syms.keys())

        # Find which scope FQNs have inbound edges FROM OTHER scope symbols
        has_scope_internal_inbound: set[str] = set()
        # Build forward adjacency (from_fqn → set of to_fqns) within scope
        forward: dict[str, set[str]] = {fqn: set() for fqn in scope_fqns}
        for e in self._edges:
            from_fqn = e.get("from_fqn", "")
            to_fqn = e.get("to_fqn", "")
            if from_fqn in scope_fqns and to_fqn in scope_fqns:
                has_scope_internal_inbound.add(to_fqn)
                forward.setdefault(from_fqn, set()).add(to_fqn)

        # Entry points: scope symbols with NO scope-internal inbound edges
        entry_points = scope_fqns - has_scope_internal_inbound

        # BFS from entry points to find all reachable symbols
        reachable: set[str] = set()
        frontier = set(entry_points)
        while frontier:
            next_frontier: set[str] = set()
            for fqn in frontier:
                if fqn in reachable:
                    continue
                reachable.add(fqn)
                next_frontier.update(forward.get(fqn, set()) - reachable)
            frontier = next_frontier

        # Unreachable = scope symbols NOT reached from any entry point
        unreachable = scope_fqns - reachable
        return [scope_syms[fqn] for fqn in unreachable]

    def graph_boundary(self, scope: str, limit: int) -> list[dict[str, Any]]:
        scope_files = {
            s.get("file_path") for s in self._symbols.values()
            if s.get("file_path", "").startswith(scope)
        }
        boundary_edges = []
        for e in self._edges:
            from_in = e.get("from_file", "") in scope_files
            to_in = e.get("to_file", "") in scope_files
            if from_in != to_in:  # One inside, one outside
                boundary_edges.append(dict(e))
        return boundary_edges[:limit]

    def graph_overview(self, scope: str | None, limit: int) -> list[dict[str, Any]]:
        # Count edges per symbol
        edge_counts: dict[str, int] = {}
        for e in self._edges:
            for fqn_key in ("from_fqn", "to_fqn"):
                fqn = e.get(fqn_key, "")
                edge_counts[fqn] = edge_counts.get(fqn, 0) + 1
        # Filter by scope
        results = []
        for s in self._symbols.values():
            if scope and not s.get("file_path", "").startswith(scope):
                continue
            fqn = s.get("fqn", "")
            record = dict(s)
            record["edge_count"] = edge_counts.get(fqn, 0)
            results.append(record)
        results.sort(key=lambda x: x.get("edge_count", 0), reverse=True)
        return results[:limit]

    def symbol_overlap(self, chunks: list[dict[str, Any]]) -> list[str]:
        fqns: set[str] = set()
        for chunk in chunks:
            fp = chunk.get("file_path", "")
            c_start = chunk.get("start_line", 0)
            c_end = chunk.get("end_line", 0)
            file_id = self._file_path_index.get(fp)
            if file_id is None:
                continue
            for s in self._symbols.values():
                if s.get("file_id") != file_id:
                    continue
                r_start = s.get("range_start", 0)
                r_end = s.get("range_end", 0)
                if r_start <= c_end and r_end >= c_start:
                    fqns.add(s["fqn"])
        return list(fqns)

    def chunk_resolution(self, fqns: list[str]) -> list[dict[str, Any]]:
        # Resolve FQNs to chunks via file_id + range overlap
        results = []
        for fqn in fqns:
            for s in self._symbols.values():
                if s.get("fqn") != fqn:
                    continue
                file_id = s.get("file_id")
                r_start = s.get("range_start", 0)
                r_end = s.get("range_end", 0)
                for c in self._chunks.values():
                    if c.get("file_id") != file_id:
                        continue
                    c_start = c.get("start_line", 0)
                    c_end = c.get("end_line", 0)
                    if c_start <= r_end and c_end >= r_start:
                        results.append(dict(c))
        return results

    def symbol_stats(self) -> dict[str, Any]:
        return {
            "symbol_count": len(self._symbols),
            "edge_count": len(self._edges),
        }

    # --- Symbol Read Query Operations ---

    def query_symbols_by_scope(self, scope: str) -> list[dict[str, Any]]:
        return [
            dict(s)
            for s in self._symbols.values()
            if s.get("file_path", "").startswith(scope)
        ]

    def query_test_symbols(self, scope: str | None) -> list[dict[str, Any]]:
        results = []
        for s in self._symbols.values():
            if s.get("kind") != "Function":
                continue
            name = s.get("name", "")
            if not name.startswith("test_"):
                continue
            if scope and not s.get("file_path", "").startswith(scope):
                continue
            results.append(dict(s))
        return results

    def query_symbol_type_signatures(self, fqns: list[str]) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        fqn_set = set(fqns)
        for s in self._symbols.values():
            fqn = s.get("fqn", "")
            if fqn in fqn_set:
                result[fqn] = s.get("type_signature")
        return result

    def query_distinct_fqns_by_file_path(self, file_path: str) -> list[str]:
        file_id = self._file_path_index.get(file_path)
        if file_id is None:
            return []
        fqns: set[str] = set()
        for s in self._symbols.values():
            if s.get("file_id") == file_id and "fqn" in s:
                fqns.add(s["fqn"])
        return list(fqns)

    # --- Scope Aggregation ---

    def get_scope_stats(self, scope_prefix: str | None) -> tuple[int, int]:
        files = 0
        chunks = 0
        for f in self._files.values():
            if scope_prefix and not f.get("path", "").startswith(scope_prefix):
                continue
            files += 1
        for c in self._chunks.values():
            file_id = c.get("file_id")
            if file_id and file_id in self._files:
                path = self._files[file_id].get("path", "")
                if scope_prefix and not path.startswith(scope_prefix):
                    continue
            chunks += 1
        return files, chunks

    def get_scope_file_paths(self, scope_prefix: str | None) -> list[str]:
        paths = []
        for f in self._files.values():
            path = f.get("path", "")
            if scope_prefix and not path.startswith(scope_prefix):
                continue
            paths.append(path)
        return paths
