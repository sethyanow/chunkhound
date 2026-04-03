"""MCP tool registry — re-exports from domain modules.

All tool definitions live in domain modules (graph.py, search.py,
lsp_tools.py, stats.py, research.py). This package provides a single
import surface: `from chunkhound.mcp_server.tools import X` works for
all public names.

Domain module imports at the bottom trigger @register_tool decorators,
populating registry.py's TOOL_REGISTRY.
"""

# ---------------------------------------------------------------------------
# Registry infrastructure
# ---------------------------------------------------------------------------
from .registry import (
    TOOL_REGISTRY as TOOL_REGISTRY,
)
from .registry import (
    Tool as Tool,
)
from .registry import (
    execute_tool as execute_tool,
)
from .registry import (
    register_tool as register_tool,
)

# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
from .response import (
    MAX_ALLOWED_TOKENS as MAX_ALLOWED_TOKENS,
)
from .response import (
    MAX_RESPONSE_TOKENS as MAX_RESPONSE_TOKENS,
)
from .response import (
    MIN_RESPONSE_TOKENS as MIN_RESPONSE_TOKENS,
)
from .response import (
    PaginationInfo as PaginationInfo,
)
from .response import (
    SearchResponse as SearchResponse,
)
from .response import (
    estimate_tokens as estimate_tokens,
)
from .response import (
    limit_response_size as limit_response_size,
)

# ---------------------------------------------------------------------------
# Tool descriptions (re-exported for CLI and MCP server imports)
# ---------------------------------------------------------------------------
from .research import (
    CODE_RESEARCH_DESCRIPTION as CODE_RESEARCH_DESCRIPTION,
)
from .research import (
    deep_research_impl as deep_research_impl,
)
from .search import (
    SEARCH_DESCRIPTION as SEARCH_DESCRIPTION,
)
from .search import (
    SEARCH_DESCRIPTION_NO_RESEARCH as SEARCH_DESCRIPTION_NO_RESEARCH,
)
from .search import (
    search_impl as search_impl,
)

# ---------------------------------------------------------------------------
# Domain module imports — trigger @register_tool registration
# ---------------------------------------------------------------------------
from . import fusion as fusion  # noqa: F401
from . import graph as graph  # noqa: F401
from . import lsp_tools as lsp_tools  # noqa: F401
from . import research as research  # noqa: F401
from . import search as _search_module  # noqa: F401
from . import stats as stats  # noqa: F401
