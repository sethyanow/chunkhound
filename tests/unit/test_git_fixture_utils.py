"""Tests for git subprocess fixture utilities.

These utilities record and replay git subprocess calls, enabling
tests that currently call real git to run against captured fixtures.
"""

import json
import subprocess

import pytest

pytestmark = pytest.mark.unit

from tests.fixtures.git_fixture_utils import (
    FakeCompletedProcess,
    GitFixturePlayer,
    GitFixtureRecorder,
)


class TestFakeCompletedProcess:
    def test_attributes(self):
        p = FakeCompletedProcess(stdout="hello", stderr="", returncode=0)
        assert p.stdout == "hello"
        assert p.stderr == ""
        assert p.returncode == 0

    def test_to_dict(self):
        p = FakeCompletedProcess(stdout="out", stderr="err", returncode=1)
        d = p.to_dict()
        assert d == {"stdout": "out", "stderr": "err", "returncode": 1}

    def test_from_dict(self):
        d = {"stdout": "out", "stderr": "err", "returncode": 0}
        p = FakeCompletedProcess.from_dict(d)
        assert p.stdout == "out"
        assert p.returncode == 0


class TestGitFixtureRecorder:
    def test_record_captures_call(self):
        recorder = GitFixtureRecorder()
        result = FakeCompletedProcess(stdout="v2.40.0", stderr="", returncode=0)
        recorder.add(["git", "--version"], result)
        assert len(recorder.entries) == 1
        assert recorder.entries[0]["args"] == ["git", "--version"]
        assert recorder.entries[0]["result"]["stdout"] == "v2.40.0"

    def test_save_and_load(self, tmp_path):
        recorder = GitFixtureRecorder()
        recorder.add(
            ["git", "status"],
            FakeCompletedProcess(stdout="clean", stderr="", returncode=0),
        )
        fixture_path = tmp_path / "git_calls.json"
        recorder.save(fixture_path)
        assert fixture_path.exists()
        data = json.loads(fixture_path.read_text())
        assert len(data) == 1

    def test_save_creates_parent_dirs(self, tmp_path):
        recorder = GitFixtureRecorder()
        recorder.add(["git", "status"], FakeCompletedProcess())
        nested = tmp_path / "sub" / "dir" / "calls.json"
        recorder.save(nested)
        assert nested.exists()

    def test_wrap_subprocess_records_real_calls(self, monkeypatch):
        """wrap_subprocess intercepts real subprocess.run and records results."""
        recorder = GitFixtureRecorder()
        # Patch subprocess.run to a known fake (don't call real git in unit tests)
        def fake_real_run(*_a, **_kw):  # noqa: ARG001
            class R:
                stdout = "fake output"
                stderr = ""
                returncode = 0
            return R()

        monkeypatch.setattr(subprocess, "run", fake_real_run)
        # Now wrap — this should call our fake_real_run AND record
        recorder.wrap_subprocess(monkeypatch)

        subprocess.run(["git", "--version"])
        assert len(recorder.entries) == 1
        assert recorder.entries[0]["args"] == ["git", "--version"]
        assert recorder.entries[0]["result"]["stdout"] == "fake output"


class TestGitFixturePlayer:
    def test_replay_sequential(self):
        entries = [
            {"args": ["git", "init"], "result": {"stdout": "Initialized", "stderr": "", "returncode": 0}},
            {"args": ["git", "status"], "result": {"stdout": "clean", "stderr": "", "returncode": 0}},
        ]
        player = GitFixturePlayer(entries)
        r1 = player.run(["git", "init"])
        assert r1.stdout == "Initialized"
        r2 = player.run(["git", "status"])
        assert r2.stdout == "clean"

    def test_replay_exhausted_raises(self):
        player = GitFixturePlayer([])
        with pytest.raises(KeyError, match="No fixture entry"):
            player.run(["git", "push"])

    def test_load_from_file(self, tmp_path):
        fixture_path = tmp_path / "git_calls.json"
        fixture_path.write_text(json.dumps([
            {"args": ["git", "log"], "result": {"stdout": "commit abc", "stderr": "", "returncode": 0}},
        ]))
        player = GitFixturePlayer.load(fixture_path)
        result = player.run(["git", "log"])
        assert result.stdout == "commit abc"

    def test_match_by_args(self):
        entries = [
            {"args": ["git", "check-ignore", "-q", "a.log"], "result": {"stdout": "", "stderr": "", "returncode": 0}},
            {"args": ["git", "check-ignore", "-q", "keep.py"], "result": {"stdout": "", "stderr": "", "returncode": 1}},
        ]
        player = GitFixturePlayer(entries, match_by_args=True)
        # Can match in any order
        r2 = player.run(["git", "check-ignore", "-q", "keep.py"])
        assert r2.returncode == 1
        r1 = player.run(["git", "check-ignore", "-q", "a.log"])
        assert r1.returncode == 0

    def test_match_by_args_missing_raises(self):
        player = GitFixturePlayer([], match_by_args=True)
        with pytest.raises(KeyError, match="No fixture entry matching"):
            player.run(["git", "status"])

    def test_subprocess_run_drop_in(self):
        """subprocess_run works as a drop-in for subprocess.run."""
        entries = [
            {"args": ["git", "status"], "result": {"stdout": "On branch main\n", "stderr": "", "returncode": 0}},
        ]
        player = GitFixturePlayer(entries)
        result = player.subprocess_run(
            ["git", "status"], cwd="/fake", capture_output=True, text=True
        )
        assert result.stdout == "On branch main\n"
        assert result.returncode == 0

    def test_subprocess_run_check_true_nonzero_raises(self):
        """subprocess_run raises CalledProcessError when check=True and rc!=0."""
        entries = [
            {"args": ["git", "push"], "result": {"stdout": "", "stderr": "rejected", "returncode": 1}},
        ]
        player = GitFixturePlayer(entries)
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            player.subprocess_run(["git", "push"], check=True)
        assert exc_info.value.returncode == 1
        assert exc_info.value.stderr == "rejected"

    def test_subprocess_run_check_true_zero_ok(self):
        """subprocess_run doesn't raise when check=True and rc==0."""
        entries = [
            {"args": ["git", "init"], "result": {"stdout": "ok", "stderr": "", "returncode": 0}},
        ]
        player = GitFixturePlayer(entries)
        result = player.subprocess_run(["git", "init"], check=True)
        assert result.stdout == "ok"

    def test_repeated_same_command_sequential(self):
        """Sequential mode handles same command called multiple times."""
        entries = [
            {"args": ["git", "check-ignore", "-q", "a.log"],
             "result": {"stdout": "", "stderr": "", "returncode": 0}},
            {"args": ["git", "check-ignore", "-q", "keep.py"],
             "result": {"stdout": "", "stderr": "", "returncode": 1}},
        ]
        player = GitFixturePlayer(entries)
        # Sequential: first call gets first entry regardless of args
        r1 = player.run(["git", "check-ignore", "-q", "a.log"])
        assert r1.returncode == 0
        r2 = player.run(["git", "check-ignore", "-q", "keep.py"])
        assert r2.returncode == 1
