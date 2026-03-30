"""Tests for path prefix scoping in semantic and regex search.

Verifies that path_filter uses strict prefix matching (not substring)
by default, preventing cross-directory leakage when paths share substrings.

Bug: both search_semantic and search_regex use LIKE '%path%' (substring)
instead of LIKE 'path%' (prefix). This causes results from nested paths
like 'vendor/src/auth/' to appear when searching for 'src/auth'.
"""

import pytest
from pathlib import Path

from chunkhound.providers.database.duckdb_provider import DuckDBProvider
from chunkhound.services.indexing_coordinator import IndexingCoordinator
from chunkhound.services.search_service import SearchService
from chunkhound.core.types.common import Language
from chunkhound.parsers.parser_factory import create_parser_for_language
from tests.fixtures.fake_providers import FakeEmbeddingProvider


async def _setup_path_scoping_fixture(
    tmp_path: Path,
) -> tuple[SearchService, DuckDBProvider]:
    """Create a workspace with files in multiple directories for path scoping tests.

    Layout:
        src/auth/login.py          — target (should match path="src/auth")
        vendor/src/auth/legacy.py  — nested duplicate (should NOT match prefix)
        src/payments/billing.py    — sibling dir (should NOT match)
    """
    workspace_dir = tmp_path

    auth_dir = workspace_dir / "src" / "auth"
    auth_dir.mkdir(parents=True)

    vendor_auth_dir = workspace_dir / "vendor" / "src" / "auth"
    vendor_auth_dir.mkdir(parents=True)

    payments_dir = workspace_dir / "src" / "payments"
    payments_dir.mkdir(parents=True)

    auth_file = auth_dir / "login.py"
    auth_file.write_text(
        "def authenticate_user(username, password):\n"
        "    '''Authenticate user credentials.'''\n"
        "    return check_credentials(username, password)\n",
        encoding="utf-8",
    )

    vendor_auth_file = vendor_auth_dir / "legacy.py"
    vendor_auth_file.write_text(
        "def authenticate_legacy(username, password):\n"
        "    '''Legacy authentication handler.'''\n"
        "    return validate_legacy_creds(username, password)\n",
        encoding="utf-8",
    )

    payments_file = payments_dir / "billing.py"
    payments_file.write_text(
        "def process_payment(amount, currency):\n"
        "    '''Process payment transaction.'''\n"
        "    return charge_card(amount, currency)\n",
        encoding="utf-8",
    )

    db = DuckDBProvider(":memory:", base_directory=workspace_dir)
    db.connect()

    embedding_provider = FakeEmbeddingProvider()
    parser = create_parser_for_language(Language.PYTHON)
    coordinator = IndexingCoordinator(
        db, workspace_dir, embedding_provider, {Language.PYTHON: parser}
    )

    await coordinator.process_file(auth_file)
    await coordinator.process_file(vendor_auth_file)
    await coordinator.process_file(payments_file)

    search_service = SearchService(db, embedding_provider)
    return search_service, db


@pytest.mark.asyncio
async def test_semantic_search_path_prefix_no_nested_leakage(
    tmp_path: Path,
) -> None:
    """path="src/auth" must not return results from vendor/src/auth/.

    The current bug: LIKE '%src/auth/%' matches both 'src/auth/login.py'
    and 'vendor/src/auth/legacy.py'. The fix: LIKE 'src/auth/%' matches
    only paths starting with that prefix.
    """
    search_service, _ = await _setup_path_scoping_fixture(tmp_path)

    results, _ = await search_service.search_semantic(
        query="authenticate user",
        page_size=10,
        offset=0,
        path_filter="src/auth",
        force_strategy="single_hop",
    )

    assert results, "Should find results in src/auth/"
    for result in results:
        file_path = result.get("file_path", "")
        assert file_path.startswith("src/auth/"), (
            f"Leakage: result '{file_path}' does not start with 'src/auth/' — "
            f"substring LIKE match is returning results from nested paths"
        )


@pytest.mark.asyncio
async def test_semantic_search_path_prefix_overlap(tmp_path: Path) -> None:
    """path="src/auth" must not return results from src/authorization/.

    Regression guard: the trailing-slash normalization ('src/auth' → 'src/auth/')
    combined with prefix matching ensures 'src/auth/' can't match 'src/authorization/'.
    """
    workspace_dir = tmp_path

    auth_dir = workspace_dir / "src" / "auth"
    auth_dir.mkdir(parents=True)
    authz_dir = workspace_dir / "src" / "authorization"
    authz_dir.mkdir(parents=True)

    auth_file = auth_dir / "login.py"
    auth_file.write_text(
        "def authenticate_user(username, password):\n"
        "    '''Authenticate user credentials.'''\n"
        "    return check_credentials(username, password)\n",
        encoding="utf-8",
    )
    authz_file = authz_dir / "rbac.py"
    authz_file.write_text(
        "def authorize_user(user_id, resource):\n"
        "    '''Check authorization permissions.'''\n"
        "    return check_permissions(user_id, resource)\n",
        encoding="utf-8",
    )

    db = DuckDBProvider(":memory:", base_directory=workspace_dir)
    db.connect()

    embedding_provider = FakeEmbeddingProvider()
    parser = create_parser_for_language(Language.PYTHON)
    coordinator = IndexingCoordinator(
        db, workspace_dir, embedding_provider, {Language.PYTHON: parser}
    )

    await coordinator.process_file(auth_file)
    await coordinator.process_file(authz_file)

    search_service = SearchService(db, embedding_provider)
    results, _ = await search_service.search_semantic(
        query="authenticate authorize user",
        page_size=10,
        offset=0,
        path_filter="src/auth",
        force_strategy="single_hop",
    )

    assert results, "Should find results in src/auth/"
    for result in results:
        file_path = result.get("file_path", "")
        assert file_path.startswith("src/auth/"), (
            f"Prefix overlap: result '{file_path}' leaked from src/authorization/ "
            f"when filtering for src/auth"
        )


@pytest.mark.asyncio
async def test_regex_search_path_prefix_no_nested_leakage(tmp_path: Path) -> None:
    """Regex search path="src/auth" must not return results from vendor/src/auth/.

    Same substring LIKE bug exists in _executor_search_regex (line 2172).
    """
    search_service, db = await _setup_path_scoping_fixture(tmp_path)

    results, _ = await search_service.search_regex_async(
        pattern="authenticate",
        page_size=10,
        offset=0,
        path_filter="src/auth",
    )

    assert results, "Should find regex results in src/auth/"
    for result in results:
        file_path = result.get("file_path", "")
        assert file_path.startswith("src/auth/"), (
            f"Leakage: regex result '{file_path}' does not start with 'src/auth/' — "
            f"substring LIKE match in _executor_search_regex"
        )


@pytest.mark.asyncio
async def test_semantic_search_fuzzy_path_restores_substring(tmp_path: Path) -> None:
    """fuzzy_path=True restores substring matching for monorepo use case.

    When the database is rooted at a monorepo parent, callers may pass
    repo-relative paths like 'services/engine' that match stored paths
    'orion-suite/services/engine/...'. fuzzy_path=True opts into this.
    """
    search_service, _ = await _setup_path_scoping_fixture(tmp_path)

    # With fuzzy_path=True, path="src/auth" should match via substring,
    # including the nested vendor/src/auth/ path
    results, _ = await search_service.search_semantic(
        query="authenticate user",
        page_size=10,
        offset=0,
        path_filter="src/auth",
        force_strategy="single_hop",
        fuzzy_path=True,
    )

    assert results, "fuzzy_path=True should find results via substring match"
    # Should find results from BOTH src/auth/ AND vendor/src/auth/
    paths = {r.get("file_path", "") for r in results}
    has_nested = any("vendor/" in p for p in paths)
    assert has_nested, (
        f"fuzzy_path=True should match nested paths via substring, "
        f"but only found: {paths}"
    )
