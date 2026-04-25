"""Embedding providers for ChunkHound - pluggable vector embedding generation."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from chunkhound.interfaces.embedding_provider import (
    EmbeddingProvider as InterfaceEmbeddingProvider,
)
from chunkhound.interfaces.embedding_provider import (
    RerankResult,
)

if TYPE_CHECKING:
    from chunkhound.providers.embeddings.openai_provider import OpenAIEmbeddingProvider
    from chunkhound.providers.embeddings.tei_provider import TEIEmbeddingProvider


def __getattr__(name: str):
    """PEP 562 lazy attribute resolution for TEIEmbeddingProvider re-export.

    Importing TEIEmbeddingProvider eagerly at module load triggers a
    circular import: chunkhound.embeddings → tei_provider →
    chunkhound.providers.__init__ → duckdb_provider → chunkhound.embeddings
    (mid-load, EmbeddingManager not yet defined).

    Resolving on first access defers the TEI module load until after
    chunkhound.embeddings has finished initializing, breaking the cycle.
    Both ``chunkhound.embeddings.TEIEmbeddingProvider`` and
    ``from chunkhound.embeddings import TEIEmbeddingProvider`` work.
    """
    if name == "TEIEmbeddingProvider":
        from chunkhound.providers.embeddings.tei_provider import (
            TEIEmbeddingProvider as _TEIEmbeddingProvider,
        )
        return _TEIEmbeddingProvider
    raise AttributeError(f"module 'chunkhound.embeddings' has no attribute {name!r}")

# Core domain models

# OpenAI and tiktoken imports have been moved to the specific provider implementations
# that need them. This reduces unnecessary dependencies in the core module.


class EmbeddingProvider(Protocol):
    """Protocol for embedding providers."""

    @property
    def name(self) -> str:
        """Provider name (e.g., 'openai')."""
        ...

    @property
    def model(self) -> str:
        """Model name (e.g., 'text-embedding-3-small')."""
        ...

    @property
    def dims(self) -> int:
        """Embedding dimensions."""
        ...

    @property
    def distance(self) -> str:
        """Distance metric ('cosine' | 'l2')."""
        ...

    @property
    def batch_size(self) -> int:
        """Maximum batch size for embedding requests."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts.

        Args:
            texts: List of text strings to embed

        Returns:
            List of embedding vectors (one per input text)
        """
        ...

    def supports_reranking(self) -> bool:
        """Return True if this provider supports reranking."""
        ...

    async def rerank(self, query: str, documents: list[str], top_k: int | None = None) -> list[RerankResult]:
        """Rerank documents by relevance to query.

        Args:
            query: Query text to rank against
            documents: List of document texts to rank
            top_k: Optional limit on number of results

        Returns:
            List of RerankResult with original index and relevance score
        """
        ...


@dataclass
class LocalEmbeddingResult:
    """Local result from embedding operation (legacy)."""

    embeddings: list[list[float]]
    model: str
    provider: str
    dims: int
    total_tokens: int | None = None


# OpenAIEmbeddingProvider moved to
# chunkhound.providers.embeddings.openai_provider.
# Use create_openai_provider() below.


class EmbeddingManager:
    """Manages embedding providers and generation."""

    def __init__(self) -> None:
        self._providers: dict[str, InterfaceEmbeddingProvider] = {}
        self._default_provider: str | None = None

    def register_provider(self, provider: InterfaceEmbeddingProvider, set_default: bool = False) -> None:
        """Register an embedding provider.

        Args:
            provider: The embedding provider to register
            set_default: Whether to set this as the default provider
        """
        self._providers[provider.name] = provider
        logger.info(f"Registered embedding provider: {provider.name} (model: {provider.model})")

        if set_default or self._default_provider is None:
            self._default_provider = provider.name
            logger.info(f"Set default embedding provider: {provider.name}")

    def get_provider(self, name: str | None = None) -> InterfaceEmbeddingProvider:
        """Get an embedding provider by name.

        Args:
            name: Provider name (uses default if None)

        Returns:
            The requested embedding provider
        """
        if name is None:
            if self._default_provider is None:
                raise ValueError("No default embedding provider set")
            name = self._default_provider

        if name not in self._providers:
            raise ValueError(f"Unknown embedding provider: {name}")

        return self._providers[name]

    def get_default_provider(self) -> InterfaceEmbeddingProvider | None:
        """Get the default embedding provider if one is set.

        Returns:
            The default embedding provider, or None if no default is set
        """
        if self._default_provider is None:
            return None
        return self._providers.get(self._default_provider)

    def list_providers(self) -> list[str]:
        """List all registered provider names."""
        return list(self._providers.keys())

    async def embed_texts(
        self,
        texts: list[str],
        provider_name: str | None = None,
    ) -> LocalEmbeddingResult:
        """Generate embeddings for texts using specified provider.

        Args:
            texts: List of texts to embed
            provider_name: Provider to use (uses default if None)

        Returns:
            Embedding result with vectors and metadata
        """
        provider = self.get_provider(provider_name)

        embeddings = await provider.embed(texts)

        return LocalEmbeddingResult(
            embeddings=embeddings,
            model=provider.model,
            provider=provider.name,
            dims=provider.dims,
        )


def create_openai_provider(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str = "text-embedding-3-small",
    rerank_model: str | None = None,
    rerank_url: str = "/rerank",
    rerank_format: str = "auto",
    rerank_batch_size: int | None = None,
    api_version: str | None = None,
    azure_endpoint: str | None = None,
    azure_deployment: str | None = None,
) -> "OpenAIEmbeddingProvider":
    """Create an OpenAI embedding provider with default settings.

    Supports both standard OpenAI and Azure OpenAI endpoints. For Azure,
    provide azure_endpoint, api_version, and optionally azure_deployment.

    Args:
        api_key: OpenAI API key (uses OPENAI_API_KEY env var if None)
        base_url: Base URL for API (uses OPENAI_BASE_URL env var if None)
        model: Model name to use
        rerank_model: Model name to use for reranking (enables multi-hop search)
        rerank_url: Rerank endpoint URL (defaults to /rerank)
        rerank_format: Reranking API format -
            'cohere', 'tei', or 'auto' (default: 'auto')
        rerank_batch_size: Max documents per rerank batch
            (overrides model defaults, bounded by model caps)
        api_version: Azure OpenAI API version (e.g., '2024-02-01')
        azure_endpoint: Azure OpenAI endpoint URL (e.g., 'https://myresource.openai.azure.com')
        azure_deployment: Azure OpenAI deployment name

    Returns:
        Configured OpenAI embedding provider
    """
    # Import the new provider from the correct location
    from chunkhound.providers.embeddings.openai_provider import OpenAIEmbeddingProvider

    return OpenAIEmbeddingProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        rerank_model=rerank_model,
        rerank_url=rerank_url,
        rerank_format=rerank_format,
        rerank_batch_size=rerank_batch_size,
        api_version=api_version,
        azure_endpoint=azure_endpoint,
        azure_deployment=azure_deployment,
    )


def create_tei_provider(
    base_url: str,
    model: str,
    dims: int,
    api_key: str | None = None,
    rerank_model: str | None = None,
    rerank_url: str | None = None,
    rerank_format: str = "auto",
    rerank_batch_size: int | None = None,
) -> "TEIEmbeddingProvider":
    """Create a TEI embedding provider with fail-fast validation.

    Validates ``base_url`` scheme and ``dims`` positivity at the registry
    layer so legacy callers (those bypassing Pydantic ``EmbeddingConfig``)
    get the same error UX as Pydantic-validated callers.

    Args:
        base_url: TEI server base URL. Must start with ``http://`` or
            ``https://``.
        model: Model name as TEI knows it (e.g.
            ``jinaai/jina-embeddings-v3``).
        dims: Embedding dimensions of the deployed model. Must be a positive
            int (jina-v3=1024, jina-v5-text-nano=256, jina-v5-text-small=512).
        api_key: Optional bearer token. TEI deployments may or may not
            require auth.
        rerank_model: Reranker model name (enables reranking when set).
        rerank_url: Reranker endpoint URL. Defaults to ``None`` so
            ``supports_reranking()`` returns False unless explicitly enabled.
        rerank_format: Reranking API format. Defaults to ``"auto"``.
        rerank_batch_size: Max documents per rerank batch.

    Returns:
        Configured TEI embedding provider.

    Raises:
        ValueError: If ``base_url`` is missing/has wrong scheme, or if
            ``dims`` is not a positive int.
    """
    # Fail-fast validation BEFORE constructing the provider, so legacy
    # callers (skipping Pydantic) get the same UX as validated callers.
    if not base_url:
        raise ValueError(
            "create_tei_provider: base_url is required (TEI has no canonical default)"
        )
    if not isinstance(base_url, str) or not (
        base_url.startswith("http://") or base_url.startswith("https://")
    ):
        raise ValueError(
            f"create_tei_provider: base_url must start with http:// or https://, "
            f"got {base_url!r}"
        )
    # dims is also re-validated inside TEIEmbeddingProvider.__init__, but
    # validating here gives a consistent error path for all entry points.
    if isinstance(dims, bool) or not isinstance(dims, int) or dims <= 0:
        raise ValueError(
            f"create_tei_provider: dims must be a positive int, got {dims!r}"
        )

    # Deferred import — see module-level __getattr__ for cycle rationale.
    from chunkhound.providers.embeddings.tei_provider import (
        TEIEmbeddingProvider as _TEIEmbeddingProvider,
    )

    return _TEIEmbeddingProvider(
        base_url=base_url,
        model=model,
        dims=dims,
        api_key=api_key,
        rerank_model=rerank_model,
        rerank_url=rerank_url,
        rerank_format=rerank_format,
        rerank_batch_size=rerank_batch_size,
    )
