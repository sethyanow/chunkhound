"""
Regression tests for ch-17t: test infrastructure must never load real user config
or make live API calls.

These tests verify that the test utility functions return fake/hardcoded data
and never touch .chunkhound.json, environment variables, or real API endpoints.
"""

import ast
import inspect

import pytest


class TestNoRealConfigDiscovery:
    """Verify test utilities never load real user configuration."""

    def test_find_config_file_does_not_exist(self):
        """_find_config_file() should not exist in test_utils — it reads real user config."""
        from tests import test_utils

        assert not hasattr(test_utils, "_find_config_file"), (
            "_find_config_file() still exists in test_utils — "
            "this function reads .chunkhound.json and must be deleted"
        )

    def test_get_embedding_config_returns_fake_data(self):
        """get_embedding_config_for_tests() must return hardcoded fake config, not real credentials."""
        from tests.test_utils import get_embedding_config_for_tests

        config = get_embedding_config_for_tests()
        assert config is not None, "get_embedding_config_for_tests() returned None — must return fake config"
        assert config["api_key"] == "fake-test-key", (
            f"Expected fake API key 'fake-test-key', got '{config.get('api_key')}' — "
            "function may still be reading real credentials"
        )

    def test_get_api_key_returns_fake_data(self):
        """get_api_key_for_tests() must return hardcoded fake key, not real credentials."""
        from tests.test_utils import get_api_key_for_tests

        api_key, provider = get_api_key_for_tests()
        assert api_key == "fake-test-key", (
            f"Expected fake API key 'fake-test-key', got '{api_key}' — "
            "function may still be reading real credentials"
        )
        assert provider is not None, "Provider must not be None"

    def test_get_embedding_config_source_has_no_file_io(self):
        """get_embedding_config_for_tests() source must not contain file I/O or env var reads."""
        from tests.test_utils import get_embedding_config_for_tests

        source = inspect.getsource(get_embedding_config_for_tests)
        tree = ast.parse(source)

        # Walk AST to find actual code calls (not docstrings)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "environ", (
                    "get_embedding_config_for_tests() still accesses os.environ — must return hardcoded config"
                )
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    assert func.id != "open", (
                        "get_embedding_config_for_tests() still calls open() — must return hardcoded config"
                    )
                    assert func.id != "_find_config_file", (
                        "get_embedding_config_for_tests() still calls _find_config_file — must return hardcoded config"
                    )


class TestNoRealProviderDiscovery:
    """Verify provider_configs never loads real providers or makes HTTP calls."""

    def test_services_available_does_not_exist(self):
        """services_available() should not exist — it makes HTTP calls to localhost."""
        from tests import provider_configs

        assert not hasattr(provider_configs, "services_available"), (
            "services_available() still exists in provider_configs — "
            "this function makes HTTP calls to localhost:11434 and localhost:8001"
        )

    def test_get_reranking_providers_returns_fake_provider(self):
        """get_reranking_providers() must return FakeEmbeddingProvider, not real providers."""
        from tests.provider_configs import get_reranking_providers

        providers = get_reranking_providers()
        assert len(providers) > 0, "get_reranking_providers() returned empty list — must return fake providers"

        for name, provider_class, config in providers:
            assert "fake" in name.lower() or "Fake" in provider_class.__name__, (
                f"Provider '{name}' ({provider_class.__name__}) is not a fake provider — "
                "get_reranking_providers() must only return fake/mock providers"
            )

    def test_get_reranking_providers_source_has_no_config_import(self):
        """get_reranking_providers() must not import or call Config()."""
        from tests.provider_configs import get_reranking_providers

        source = inspect.getsource(get_reranking_providers)
        assert "Config()" not in source, (
            "get_reranking_providers() still calls Config() — "
            "this loads .chunkhound.json and real API keys"
        )

    def test_provider_configs_no_config_import(self):
        """provider_configs module must not import chunkhound.core.config.config.Config."""
        from tests import provider_configs

        source = inspect.getsource(provider_configs)
        assert "from chunkhound.core.config.config import Config" not in source, (
            "provider_configs.py still imports Config from chunkhound — "
            "this triggers .chunkhound.json loading at import time"
        )
