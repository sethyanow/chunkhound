"""TEI (Text Embeddings Inference) embedding provider for ChunkHound.

Subclasses :class:`OpenAIEmbeddingProvider` to inherit batching, retry, and
token-limit fallback. Adds the bits TEI needs that the OpenAI base does not:

  * Required ``dims`` parameter — TEI hosts custom models (jina-v3, jina-v5)
    whose embedding dimensions cannot be inferred from the model name.
  * ``@property name`` returns ``"tei"`` — distinct provider identity for
    DB table keying and metrics.
  * ``_embed_batch_internal`` override (Cycle B) maps the asymmetric-retrieval
    ``task`` hint to the OpenAI SDK's ``extra_body={"task": "retrieval.X"}``
    field, which TEI's OpenAI-compat layer forwards to the model.

ch-agj Cycle A: class shell only (this file). Cycle B adds task→extra_body.
"""

from __future__ import annotations

from chunkhound.providers.embeddings.openai_provider import OpenAIEmbeddingProvider


class TEIEmbeddingProvider(OpenAIEmbeddingProvider):
    """Embedding provider for Hugging Face TEI servers.

    TEI exposes an OpenAI-compatible endpoint, so most of the request /
    retry / batch logic is reused from the parent class via inheritance.
    The two divergences are:

    1. ``dims`` is REQUIRED at construction (parent hardcodes 1536 fallback
       which is wrong for jina models — v3=1024, v5-nano=256, v5-small=512).
    2. ``name`` returns ``"tei"`` so DB tables key under
       ``(provider="tei", model, dims)`` distinct from the OpenAI tables.

    Reranking is inherited from the parent's HTTP path; if the user does
    not configure it (default), ``supports_reranking()`` returns False.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dims: int,
        api_key: str | None = None,
        batch_size: int = 100,
        timeout: int = 30,
        retry_attempts: int = 3,
        retry_delay: float = 1.0,
        max_tokens: int | None = None,
        rerank_model: str | None = None,
        rerank_url: str | None = None,
        rerank_format: str = "auto",
        rerank_batch_size: int | None = None,
    ) -> None:
        """Initialize the TEI embedding provider.

        Args:
            base_url: TEI server base URL (e.g. ``http://localhost:8080/v1``).
                Required — TEI has no canonical default.
            model: Model name as TEI knows it (e.g.
                ``jinaai/jina-embeddings-v3``).
            dims: Embedding dimensions of the deployed model. REQUIRED —
                TEI cannot infer this and the parent's fallback (1536) is
                wrong for every jina model.
            api_key: Optional bearer token. TEI deployments may or may not
                require auth.
            batch_size: Maximum batch size for API requests.
            timeout: Request timeout in seconds.
            retry_attempts: Number of retry attempts on transient failures.
            retry_delay: Base delay between retry attempts (seconds).
            max_tokens: Maximum tokens per request (uses parent default
                when None).
            rerank_model: Reranker model name (only when reranking is
                enabled). Defaults to None — embedding-only is the
                expected configuration.
            rerank_url: Reranker endpoint URL. Defaults to None (overrides
                parent's ``/rerank`` default) so ``supports_reranking()``
                returns False unless the user explicitly opts in.
            rerank_format: Reranking API format. Defaults to ``"auto"``.
            rerank_batch_size: Max documents per rerank batch.

        Raises:
            ValueError: If ``dims`` is not a positive int. ``True``/``False``
                are explicitly rejected even though Python treats ``bool``
                as a subclass of ``int``.
        """
        # Validate dims BEFORE super().__init__ — fail-fast on bad config.
        # bool is rejected explicitly because it's an int subclass in Python.
        if isinstance(dims, bool) or not isinstance(dims, int) or dims <= 0:
            raise ValueError(
                f"TEI dims must be a positive int, got {dims!r}"
            )

        # Set _dims BEFORE super().__init__ so that if any parent init code
        # ever touches self.dims (it doesn't today, but defensive against
        # parent drift), the override resolves cleanly.
        self._dims: int = dims

        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            rerank_model=rerank_model,
            rerank_url=rerank_url,
            rerank_format=rerank_format,
            rerank_batch_size=rerank_batch_size,
            batch_size=batch_size,
            timeout=timeout,
            retry_attempts=retry_attempts,
            retry_delay=retry_delay,
            max_tokens=max_tokens,
        )

    @property
    def name(self) -> str:
        """Provider name — always ``"tei"`` regardless of base_url shape.

        Overrides parent which returns ``"openai"`` or ``"azure_openai"``.
        DB tables key under ``(provider, model, dims)``, so a stable
        ``"tei"`` here keeps TEI embeddings isolated from OpenAI/Azure.
        """
        return "tei"

    @property
    def dims(self) -> int:
        """Embedding dimensions — returns the value set at construction.

        Override is load-bearing: parent's ``dims`` hardcodes 1536 for
        non-OpenAI models, which is wrong for every model TEI hosts.
        """
        return self._dims
