"""Tests for git-aware indexing in SimpleEventHandler._should_index().

Verifies that _should_index checks git status so the realtime index
reflects committed + staged state, not just filesystem presence.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pygit2
import pytest

from chunkhound.core.config.config import Config
from chunkhound.services.realtime_indexing_service import SimpleEventHandler

pytestmark = pytest.mark.integration



def _handler(root: Path) -> SimpleEventHandler:
    cfg = Config(target_dir=root)
    q: asyncio.Queue = asyncio.Queue()
    return SimpleEventHandler(q, config=cfg)


def _init_repo(root: Path) -> pygit2.Repository:
    """Initialize a git repo and make an initial commit."""
    repo = pygit2.init_repository(str(root))
    sig = pygit2.Signature("test", "test@test.com")
    # Need at least one commit for status to work properly
    tree = repo.index.write_tree()
    repo.create_commit("HEAD", sig, sig, "initial", tree, [])
    return repo


def test_git_rm_cached_file_not_indexed(tmp_path: Path) -> None:
    """A file removed from git tracking (git rm --cached) should not be indexed.

    After git rm --cached, the file remains on disk but is untracked (WT_NEW).
    _should_index should return False because untracked files should be excluded.
    """
    root = tmp_path
    repo = _init_repo(root)

    # Create and commit a Python file
    py_file = root / "module.py"
    py_file.write_text("def hello(): pass\n")
    repo.index.add("module.py")
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("test", "test@test.com")
    parent = repo.head.target
    repo.create_commit("HEAD", sig, sig, "add module", tree, [parent])

    # Now remove from tracking (git rm --cached) — file stays on disk
    repo.index.remove("module.py")
    repo.index.write()

    handler = _handler(root)

    # File is on disk but untracked after git rm --cached
    # _should_index should return False
    assert handler._should_index(py_file) is False, (
        "File removed from git tracking (git rm --cached) should not be indexed"
    )


def test_untracked_file_not_indexed(tmp_path: Path) -> None:
    """An untracked file (never added to git) should not be indexed."""
    root = tmp_path
    _init_repo(root)

    untracked = root / "scratch.py"
    untracked.write_text("x = 1\n")

    handler = _handler(root)
    assert handler._should_index(untracked) is False, (
        "Untracked file (WT_NEW) should not be indexed"
    )


def test_staged_new_file_is_indexed(tmp_path: Path) -> None:
    """A newly staged file (git add, not yet committed) SHOULD be indexed."""
    root = tmp_path
    repo = _init_repo(root)

    staged = root / "new_feature.py"
    staged.write_text("def feature(): pass\n")
    repo.index.add("new_feature.py")
    repo.index.write()

    handler = _handler(root)
    assert handler._should_index(staged) is True, (
        "Staged new file (INDEX_NEW) should be indexed"
    )


def test_non_git_project_falls_back(tmp_path: Path) -> None:
    """A project with no .git directory should fall back to pattern-only filtering."""
    root = tmp_path
    # No git init — just a directory with a Python file
    py_file = root / "app.py"
    py_file.write_text("print('hello')\n")

    handler = _handler(root)
    assert handler._should_index(py_file) is True, (
        "Non-git project should fall back to pattern-only (current behavior)"
    )


def test_non_ascii_path_in_git_repo(tmp_path: Path) -> None:
    """File paths with non-ASCII characters should resolve correctly in git repos."""
    root = tmp_path
    repo = _init_repo(root)

    # Create a file with non-ASCII directory name
    unicode_dir = root / "módulos"
    unicode_dir.mkdir()
    py_file = unicode_dir / "código.py"
    py_file.write_text("x = 1\n")
    repo.index.add("módulos/código.py")
    repo.index.write()

    handler = _handler(root)
    assert handler._should_index(py_file) is True, (
        "Staged file with non-ASCII path should be indexed"
    )


def test_git_index_lock_falls_back_gracefully(tmp_path: Path) -> None:
    """When .git/index.lock exists (rebase in progress), should not crash.

    pygit2 may raise GitError when the index is locked. _check_git_state
    should catch this and fall back to allowing the file (return True).
    """
    root = tmp_path
    repo = _init_repo(root)

    py_file = root / "module.py"
    py_file.write_text("def hello(): pass\n")
    repo.index.add("module.py")
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("test", "test@test.com")
    parent = repo.head.target
    repo.create_commit("HEAD", sig, sig, "add module", tree, [parent])

    # Simulate a locked index (rebase in progress)
    lock_file = root / ".git" / "index.lock"
    lock_file.write_text("")

    handler = _handler(root)
    # Should not crash — falls back to allowing the file
    result = handler._should_index(py_file)
    assert result is True, (
        "Git index lock should not crash _should_index — should fall back to True"
    )
