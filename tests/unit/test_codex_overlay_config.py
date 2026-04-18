"""Unit tests for CodexCLIProvider._build_overlay_home().

Covers the overlay builder contract directly: parse the emitted config.toml
and assert on contents for each resolution-source path. Replaces the
subprocess-based tests in tests/integration/test_codex_exec_help.py that
burned live ChatGPT subscription tokens on every integration run.

Hermetic:
- Monkeypatches `_get_base_codex_home` to `None` so `_copy_minimal_codex_state`
  never touches the developer's real `~/.codex`.
- Monkeypatches `_codex_available` to `True` to avoid spawning `codex --version`.
- Tracks overlay dirs in a fixture and cleans them up in teardown.
"""
from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from chunkhound.providers.llm.codex_cli_provider import CodexCLIProvider

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _hermetic_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate CodexCLIProvider from dev machine state for unit tests."""
    monkeypatch.setattr(CodexCLIProvider, "_get_base_codex_home", lambda self: None)
    monkeypatch.setattr(CodexCLIProvider, "_codex_available", lambda self: True)
    for key in (
        "CHUNKHOUND_CODEX_DEFAULT_MODEL",
        "CHUNKHOUND_CODEX_REASONING_EFFORT",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def make_overlay():
    """Build an overlay and track it for teardown cleanup."""
    created: list[str] = []

    def _make(provider: CodexCLIProvider) -> dict:
        overlay_path = provider._build_overlay_home()
        created.append(overlay_path)
        cfg = Path(overlay_path) / "config.toml"
        return tomllib.loads(cfg.read_text(encoding="utf-8"))

    yield _make
    for path in created:
        shutil.rmtree(path, ignore_errors=True)


def test_default_construction_omits_model_key(make_overlay):
    """No constructor args, no env vars → source is 'default' → omit model key."""
    provider = CodexCLIProvider()
    config = make_overlay(provider)

    assert "model" not in config, (
        f"Overlay emitted `model` key under default construction: {config!r}. "
        "Expected source-aware emission to skip the key when resolution source is 'default'."
    )


def test_codex_alias_omits_model_key(make_overlay):
    """model='codex' is an alias for default → source is 'default' → omit model key."""
    provider = CodexCLIProvider(model="codex")
    config = make_overlay(provider)

    assert "model" not in config, (
        f"Overlay emitted `model` key for model='codex' alias: {config!r}. "
        "The 'codex' literal is documented as an alias for the default model "
        "(codex_cli_provider.py:84) and should produce source='default'."
    )


def test_default_reasoning_effort_omits_key(make_overlay):
    """No explicit effort, no env var → source is 'default' → omit effort key."""
    provider = CodexCLIProvider()
    config = make_overlay(provider)

    assert "model_reasoning_effort" not in config, (
        f"Overlay emitted `model_reasoning_effort` key under default construction: "
        f"{config!r}. Expected source-aware emission to skip the key when "
        "resolution source is 'default', symmetric to the model key."
    )


def test_explicit_model_emits_model_key(make_overlay):
    """Non-alias model passed to constructor → source 'explicit' → emit model key."""
    provider = CodexCLIProvider(model="gpt-4o")
    config = make_overlay(provider)

    assert config.get("model") == "gpt-4o", (
        f"Expected `model = \"gpt-4o\"` in overlay TOML, got {config!r}."
    )


def test_explicit_reasoning_effort_emits_key(make_overlay):
    """Valid reasoning_effort to constructor → source 'explicit' → emit effort key."""
    provider = CodexCLIProvider(reasoning_effort="high")
    config = make_overlay(provider)

    assert config.get("model_reasoning_effort") == "high", (
        f"Expected `model_reasoning_effort = \"high\"`, got {config!r}."
    )


def test_env_var_model_override_emits_key(make_overlay, monkeypatch):
    """CHUNKHOUND_CODEX_DEFAULT_MODEL set → source 'env:...' → emit model key."""
    monkeypatch.setenv("CHUNKHOUND_CODEX_DEFAULT_MODEL", "claude-3-opus")
    provider = CodexCLIProvider()  # no constructor arg
    config = make_overlay(provider)

    assert config.get("model") == "claude-3-opus", (
        f"Expected env override to produce `model = \"claude-3-opus\"`, got {config!r}."
    )


def test_env_var_reasoning_effort_override_emits_key(make_overlay, monkeypatch):
    """CHUNKHOUND_CODEX_REASONING_EFFORT set → source 'env:...' → emit effort key."""
    monkeypatch.setenv("CHUNKHOUND_CODEX_REASONING_EFFORT", "medium")
    provider = CodexCLIProvider()  # no constructor arg
    config = make_overlay(provider)

    assert config.get("model_reasoning_effort") == "medium", (
        f"Expected env override to produce `model_reasoning_effort = \"medium\"`, "
        f"got {config!r}."
    )


def test_overlay_always_disables_history_and_omits_mcp_servers(make_overlay):
    """Regardless of model/effort sources: history.persistence='none', no mcp_servers."""
    provider = CodexCLIProvider()
    config = make_overlay(provider)

    assert config.get("history", {}).get("persistence") == "none", (
        f"Overlay must disable codex history persistence. Got {config!r}."
    )
    assert "mcp_servers" not in config, (
        f"Overlay must not register any mcp_servers. Got {config!r}."
    )


def test_explicit_model_aligns_with_resolver_output(make_overlay):
    """When emitted, `model` matches what describe_model_resolution returns."""
    # Pick any non-alias value; the resolver passes it through unchanged.
    resolved, source = CodexCLIProvider.describe_model_resolution("my-custom-model")
    assert source == "explicit", "resolver contract changed — fix the test"

    provider = CodexCLIProvider(model="my-custom-model")
    config = make_overlay(provider)

    assert config.get("model") == resolved, (
        f"Overlay model key must equal resolver output ({resolved!r}), got {config!r}."
    )


# ── Adversarial battery (ch-26k Step 10) ────────────────────────────────────


def test_uppercase_codex_alias_omits_model_key(make_overlay):
    """Semantically hostile: `model='CODEX'` is case-insensitive for the
    default-alias path (resolver line 84). Overlay must still omit the key.
    """
    provider = CodexCLIProvider(model="CODEX")
    config = make_overlay(provider)

    assert "model" not in config, (
        f"Uppercase 'CODEX' alias should resolve to source='default' and "
        f"omit the model key. Got: {config!r}"
    )


def test_whitespace_padded_codex_alias_omits_model_key(make_overlay):
    """Semantically hostile: `model='  codex  '` — stripped by the resolver
    before comparison. Overlay must still omit the key.
    """
    provider = CodexCLIProvider(model="  codex  ")
    config = make_overlay(provider)

    assert "model" not in config, (
        f"Whitespace-padded 'codex' alias should resolve to source='default' "
        f"and omit the model key. Got: {config!r}"
    )


def test_unicode_model_name_round_trips_through_toml(make_overlay):
    """Encoding boundary: non-ASCII model name must survive the TOML
    write/parse cycle intact. Failure here means codex would read a
    corrupted model identifier.
    """
    provider = CodexCLIProvider(model="モデル-α")
    config = make_overlay(provider)

    assert config.get("model") == "モデル-α", (
        f"Non-ASCII model name corrupted in overlay round-trip. Got: {config!r}"
    )


def test_model_name_with_embedded_quote_escapes_correctly(make_overlay):
    """Encoding boundary: a model name containing `\"` would break naive
    TOML emission. Provider's `_toml_string` helper must escape it such
    that the written TOML parses back to the original string.
    """
    provider = CodexCLIProvider(model='evil"model')
    config = make_overlay(provider)

    assert config.get("model") == 'evil"model', (
        f"Embedded quote corrupted in overlay round-trip. Got: {config!r}"
    )


def test_model_name_with_backslash_round_trips_through_toml(make_overlay):
    """Encoding boundary: a model name containing `\\` must be TOML-escaped.
    Unescaped backslash + non-escape-letter produces invalid TOML per spec
    (e.g. `"foo\\path"` fails parsing on `\\p`).
    """
    provider = CodexCLIProvider(model=r"win\path\model")
    config = make_overlay(provider)

    assert config.get("model") == r"win\path\model", (
        f"Backslash in model name corrupted or broke TOML. Got: {config!r}"
    )


def test_second_overlay_invocation_yields_independent_directory(monkeypatch):
    """State isolation: each call to `_build_overlay_home()` must produce
    a fresh directory. Two sequential calls with different configs must
    not contaminate each other's config.toml.
    """
    monkeypatch.setattr(CodexCLIProvider, "_get_base_codex_home", lambda self: None)
    monkeypatch.setattr(CodexCLIProvider, "_codex_available", lambda self: True)
    for key in (
        "CHUNKHOUND_CODEX_DEFAULT_MODEL",
        "CHUNKHOUND_CODEX_REASONING_EFFORT",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(key, raising=False)

    provider_a = CodexCLIProvider(model="model-a")
    provider_b = CodexCLIProvider(model="model-b")

    overlay_a = provider_a._build_overlay_home()
    overlay_b = provider_b._build_overlay_home()

    try:
        assert overlay_a != overlay_b, "Two invocations returned the same dir"

        config_a = tomllib.loads((Path(overlay_a) / "config.toml").read_text())
        config_b = tomllib.loads((Path(overlay_b) / "config.toml").read_text())

        assert config_a["model"] == "model-a"
        assert config_b["model"] == "model-b"
    finally:
        shutil.rmtree(overlay_a, ignore_errors=True)
        shutil.rmtree(overlay_b, ignore_errors=True)
