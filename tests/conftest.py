import os
import socket

from loguru import logger

logger.remove()

import pytest

_LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}
)


@pytest.fixture(autouse=True)
def _block_outbound_network_for_integration(request, monkeypatch):
    """Enforce the tier rule: integration-marked tests may not reach the
    public internet. Python-level `socket.socket.connect` is monkeypatched
    to raise RuntimeError for any non-loopback destination.

    Subprocess children (codex, curl, git, etc.) have their own socket
    namespace and bypass this hook — they MUST be classified into the
    correct tier manually. See AGENTS.md "Test Tiers".
    """
    if "integration" not in request.node.keywords:
        return

    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        # AF_UNIX: address is a filesystem path → local IPC, always OK
        if isinstance(address, (bytes, str)):
            return real_connect(self, address)
        host = address[0] if address else None
        if host in _LOOPBACK_HOSTS:
            return real_connect(self, address)
        raise RuntimeError(
            f"Integration tier forbids outbound network; "
            f"test attempted connect to {host!r} via {type(self).__name__}"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


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
    "provider": "voyageai",
    "model": "voyage-4",
    "base_url": "https://api.voyageai.com/v1",
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
