import os

from loguru import logger

logger.remove()

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "heavy: mark tests that generate large synthetic trees (skipped by default)")


def pytest_collection_modifyitems(config, items):
    run_heavy = os.getenv("CHUNKHOUND_RUN_HEAVY_TESTS") == "1"
    skip_heavy = pytest.mark.skip(reason="heavy tests skipped by default (set CHUNKHOUND_RUN_HEAVY_TESTS=1 to run)")
    for item in items:
        if not run_heavy and "heavy" in item.keywords:
            item.add_marker(skip_heavy)


# -- Acceptance test config: real API URLs for VCR cassette matching --
# Defaults use real OpenAI URLs so cassette request matching works during replay.
# Env vars override for recording (user provides real API key).
_ACCEPTANCE_EMBEDDING_DEFAULTS: dict[str, str | int] = {
    "api_key": "vcr-replay-placeholder",
    "provider": "openai",
    "model": "text-embedding-3-small",
    "base_url": "https://api.openai.com/v1",
    "rerank_model": "",
    "rerank_url": "",
    "rerank_format": "auto",
    "rerank_batch_size": 10,
}

_ACCEPTANCE_ENV_MAP: dict[str, str] = {
    "CHUNKHOUND_EMBEDDING__API_KEY": "api_key",
    "CHUNKHOUND_EMBEDDING__PROVIDER": "provider",
    "CHUNKHOUND_EMBEDDING__MODEL": "model",
    "CHUNKHOUND_EMBEDDING__BASE_URL": "base_url",
}


def _build_acceptance_embedding_config() -> dict[str, str | int]:
    config = dict(_ACCEPTANCE_EMBEDDING_DEFAULTS)
    for env_var, config_key in _ACCEPTANCE_ENV_MAP.items():
        value = os.environ.get(env_var)
        if value and value.strip():
            config[config_key] = value.strip()
    return config


@pytest.fixture
def acceptance_embedding_config_factory():
    """Factory fixture — call after monkeypatch.setenv to pick up env vars."""
    return _build_acceptance_embedding_config


@pytest.fixture
def acceptance_embedding_config():
    """Returns acceptance embedding config dict (real URLs, env var overrides)."""
    return _build_acceptance_embedding_config()


@pytest.fixture(scope="session")
def vcr_config():
    """VCR configuration for acceptance tests — filters auth headers from cassettes."""
    return {
        "filter_headers": ["authorization", "x-api-key"],
        "cassette_library_dir": os.path.join(os.path.dirname(__file__), "cassettes"),
        "record_mode": "none",
    }


@pytest.fixture
def clean_environment(monkeypatch):
    """Ensure tests run with a clean environment.

    - Unset CHUNKHOUND_* variables that can alter discovery/backends.
    - Unset common embedding API keys to avoid accidental network init.
    """
    to_clear = [k for k in os.environ.keys() if k.startswith("CHUNKHOUND_")]
    to_clear += [
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "VOYAGE_API_KEY",
        "ANTHROPIC_API_KEY",
    ]
    for k in to_clear:
        monkeypatch.delenv(k, raising=False)
    yield
