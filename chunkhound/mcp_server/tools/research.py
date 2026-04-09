"""Research MCP tool — deep code research via LLM synthesis."""

from typing import Any

from chunkhound.core.config.config import Config
from chunkhound.database_factory import DatabaseServices
from chunkhound.embeddings import EmbeddingManager
from chunkhound.llm_manager import LLMManager
from chunkhound.services.research.factory import ResearchServiceFactory

from .registry import register_tool

CODE_RESEARCH_DESCRIPTION = """Start here for any coding task. Call code_research first to understand the relevant code area before writing or modifying code.

WORKFLOW:
1. **Understand** — call code_research to map architecture, components, and data flow
2. **Deepen** — call again with focused queries on specific subsystems discovered in step 1
3. **Pinpoint** — switch to search (regex/semantic) for exact file locations and symbol references
4. **Inspect** — use Explore/grep/read for granular line-level follow-up

WHAT IT RETURNS: Cited markdown report covering architecture overview, key code locations, component relationships, and cross-file data flows.

EXAMPLES:
- "How does authentication work?" — traces the full auth flow across files
- "What happens when a request hits /api/users?" — maps the request lifecycle
- "Explain error handling patterns" — identifies cross-cutting concerns

SCOPE: Use the path parameter to restrict analysis to a subdirectory for faster, focused results.

One call replaces 5-10 manual searches. Call it liberally — understanding first, coding second."""


@register_tool(
    description=CODE_RESEARCH_DESCRIPTION,
    requires_embeddings=True,
    requires_llm=True,
    requires_reranker=True,
    name="code_research",
)
async def deep_research_impl(
    services: DatabaseServices,
    embedding_manager: EmbeddingManager,
    llm_manager: LLMManager | None,
    query: str,
    progress: Any = None,
    path: str | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """Core deep research implementation.

    Args:
        services: Database services bundle
        embedding_manager: Embedding manager instance
        llm_manager: LLM manager instance
        query: Natural language question about codebase architecture or behavior, e.g. "how does authentication work end-to-end?" or "explain the request lifecycle"
        progress: Optional Rich Progress instance for terminal UI (None for MCP)
        path: Optional relative subdirectory to restrict analysis scope, e.g. "src/auth" or "lib/payments" (no leading slash)
        config: Application configuration (optional, defaults to environment config)

    Returns:
        Dict with answer and metadata

    Raises:
        Exception: If LLM or reranker not configured
    """
    # Validate LLM is configured
    if not llm_manager:
        raise Exception(
            "No LLM provider configured. Code research requires an LLM. "
            "Configure an llm section in your chunkhound configuration."
        )

    # Validate reranker is configured
    if not embedding_manager or not embedding_manager.list_providers():
        raise Exception("No embedding providers available. Code research requires reranking support.")

    embedding_provider = embedding_manager.get_provider()
    if not (hasattr(embedding_provider, "supports_reranking") and embedding_provider.supports_reranking()):
        raise Exception(
            "Code research requires a provider with reranking support. "
            "Configure a rerank_model in your embedding configuration."
        )

    # Create default config from environment if not provided
    if config is None:
        config = Config.from_environment()

    # Create code research service using factory (v1 or v2 based on config)
    research_service = ResearchServiceFactory.create(
        config=config,
        db_services=services,
        embedding_manager=embedding_manager,
        llm_manager=llm_manager,
        tool_name="code_research",
        progress=progress,
        path_filter=path,
    )

    return await research_service.deep_research(query)
