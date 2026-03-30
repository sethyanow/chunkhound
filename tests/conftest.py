import os

from loguru import logger

logger.remove()

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "heavy: mark tests that generate large synthetic trees (skipped by default)")
    config.addinivalue_line("markers", "integration: mark tests that require live API keys and network access (skipped by default)")


def pytest_collection_modifyitems(config, items):
    run_heavy = os.getenv("CHUNKHOUND_RUN_HEAVY_TESTS") == "1"
    run_integration = os.getenv("CHUNKHOUND_RUN_INTEGRATION_TESTS") == "1"
    skip_heavy = pytest.mark.skip(reason="heavy tests skipped by default (set CHUNKHOUND_RUN_HEAVY_TESTS=1 to run)")
    skip_integration = pytest.mark.skip(reason="integration tests skipped by default (set CHUNKHOUND_RUN_INTEGRATION_TESTS=1 to run)")
    for item in items:
        if not run_heavy and "heavy" in item.keywords:
            item.add_marker(skip_heavy)
        if not run_integration and "integration" in item.keywords:
            item.add_marker(skip_integration)


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
