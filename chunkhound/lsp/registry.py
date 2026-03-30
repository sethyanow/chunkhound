"""Language server registry — maps language IDs to server configurations.

Zero chunkhound imports — stdlib only + sibling lsp.types.

32 entries for all Language enum members with tree-sitter grammars
(excludes TEXT, PDF, UNKNOWN). Languages without a well-known server
have command=None.
"""

from __future__ import annotations

from chunkhound.lsp.types import ServerConfig

LANGUAGE_SERVER_REGISTRY: dict[str, ServerConfig] = {
    # --- Python ---
    "python": ServerConfig(
        language_id="python",
        command="pyright-langserver",
        args=["--stdio"],
    ),
    # --- JavaScript / TypeScript family ---
    "javascript": ServerConfig(
        language_id="javascript",
        command="typescript-language-server",
        args=["--stdio"],
    ),
    "typescript": ServerConfig(
        language_id="typescript",
        command="typescript-language-server",
        args=["--stdio"],
    ),
    "tsx": ServerConfig(
        language_id="tsx",
        command="typescript-language-server",
        args=["--stdio"],
    ),
    "jsx": ServerConfig(
        language_id="jsx",
        command="typescript-language-server",
        args=["--stdio"],
    ),
    # --- Systems languages ---
    "go": ServerConfig(
        language_id="go",
        command="gopls",
        args=["serve"],
    ),
    "rust": ServerConfig(
        language_id="rust",
        command="rust-analyzer",
        args=[],
    ),
    "c": ServerConfig(
        language_id="c",
        command="clangd",
        args=["--background-index"],
    ),
    "cpp": ServerConfig(
        language_id="cpp",
        command="clangd",
        args=["--background-index"],
    ),
    "zig": ServerConfig(
        language_id="zig",
        command="zls",
        args=[],
    ),
    # --- JVM languages ---
    "java": ServerConfig(
        language_id="java",
        command="jdtls",
        args=[],
    ),
    "kotlin": ServerConfig(
        language_id="kotlin",
        command="kotlin-language-server",
        args=[],
    ),
    "groovy": ServerConfig(
        language_id="groovy",
        command="groovy-language-server",
        args=[],
    ),
    # --- C-family ---
    "csharp": ServerConfig(
        language_id="csharp",
        command="csharp-ls",
        args=[],
    ),
    "objc": ServerConfig(
        language_id="objc",
        command="clangd",
        args=["--background-index"],
    ),
    # --- Scripting languages ---
    "bash": ServerConfig(
        language_id="bash",
        command="bash-language-server",
        args=["start"],
    ),
    "lua": ServerConfig(
        language_id="lua",
        command="lua-language-server",
        args=[],
    ),
    "php": ServerConfig(
        language_id="php",
        command="phpactor",
        args=["language-server"],
    ),
    "elixir": ServerConfig(
        language_id="elixir",
        command="elixir-ls",
        args=[],
    ),
    "haskell": ServerConfig(
        language_id="haskell",
        command="haskell-language-server-wrapper",
        args=["--lsp"],
    ),
    # --- Web frameworks ---
    "svelte": ServerConfig(
        language_id="svelte",
        command="svelteserver",
        args=["--stdio"],
    ),
    "vue": ServerConfig(
        language_id="vue",
        command="vue-language-server",
        args=["--stdio"],
    ),
    # --- Mobile / Native ---
    "swift": ServerConfig(
        language_id="swift",
        command="sourcekit-lsp",
        args=[],
    ),
    "dart": ServerConfig(
        language_id="dart",
        command="dart",
        args=["language-server", "--protocol=lsp"],
    ),
    # --- Config / Data languages ---
    "toml": ServerConfig(
        language_id="toml",
        command="taplo",
        args=["lsp", "stdio"],
    ),
    "yaml": ServerConfig(
        language_id="yaml",
        command="yaml-language-server",
        args=["--stdio"],
    ),
    "json": ServerConfig(
        language_id="json",
        command="vscode-json-languageserver",
        args=["--stdio"],
    ),
    "hcl": ServerConfig(
        language_id="hcl",
        command="terraform-ls",
        args=["serve"],
    ),
    "sql": ServerConfig(
        language_id="sql",
        command="sqls",
        args=[],
    ),
    # --- Documentation ---
    "markdown": ServerConfig(
        language_id="markdown",
        command="marksman",
        args=["server"],
    ),
    # --- No well-known language server ---
    "makefile": ServerConfig(
        language_id="makefile",
        command=None,  # No standard makefile language server
    ),
    "matlab": ServerConfig(
        language_id="matlab",
        command=None,  # MATLAB language server requires proprietary toolbox
    ),
}
