"""Git subprocess fixture utilities for recording and replaying git commands.

Enables tests that currently call real git subprocess to run against
captured JSON fixtures instead.

Three categories of git tests require different approaches:

1. **Already monkeypatched** — tests that mock subprocess.run themselves.
   These just need marker changes (unit instead of integration). No fixtures needed.

2. **Filesystem-only** — tests that create .git dirs and .gitignore files but
   never call the git binary. Already unit-testable with tmp_path.

3. **Real git subprocess** — tests that call git init, git check-ignore, etc.
   These need GitFixturePlayer to replay captured subprocess output, plus
   filesystem setup for any files the tested code reads directly.

Usage for recording (run once against real git to capture fixtures):
    recorder = GitFixtureRecorder()
    recorder.wrap_subprocess(monkeypatch)  # auto-records all subprocess.run calls
    # ... run the test normally against real git ...
    recorder.save(Path("tests/fixtures/data/git_parity_basic.json"))

Usage for replaying (in converted unit tests):
    player = GitFixturePlayer.load(Path("tests/fixtures/data/git_parity_basic.json"))
    monkeypatch.setattr(subprocess, "run", player.subprocess_run)
    # test runs against recorded fixtures instead of real git
"""

import json
import subprocess
from pathlib import Path
from typing import Any


class FakeCompletedProcess:
    """Lightweight stand-in for subprocess.CompletedProcess.

    Provides the same attributes tests access: stdout, stderr, returncode.
    Used both for recording real results and replaying from fixtures.
    """

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FakeCompletedProcess":
        return cls(
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            returncode=data.get("returncode", 0),
        )


class GitFixtureRecorder:
    """Records git subprocess calls and their results for fixture generation.

    Can be used manually (call add() for each command) or automatically
    via wrap_subprocess() which intercepts all subprocess.run calls.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def add(self, args: list[str], result: FakeCompletedProcess) -> None:
        """Record a git command and its result."""
        self.entries.append({
            "args": args,
            "result": result.to_dict(),
        })

    def wrap_subprocess(self, monkeypatch: Any) -> None:
        """Wrap subprocess.run to record all calls while still executing them.

        This lets you run the test against real git and capture the results
        for later replay. The original subprocess.run is called, and the
        result is recorded.
        """
        original_run = subprocess.run

        def recording_run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            result = original_run(args, **kwargs)
            args_list = list(args) if not isinstance(args, list) else args
            self.add(
                args_list,
                FakeCompletedProcess(
                    stdout=result.stdout if isinstance(result.stdout, str) else "",
                    stderr=result.stderr if isinstance(result.stderr, str) else "",
                    returncode=result.returncode,
                ),
            )
            return result

        monkeypatch.setattr(subprocess, "run", recording_run)

    def save(self, path: Path) -> None:
        """Save recorded entries to a JSON fixture file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.entries, indent=2))


class GitFixturePlayer:
    """Replays git subprocess calls from recorded fixtures.

    Supports two matching modes:
    - Sequential (default): calls are matched in order, regardless of args.
      Handles tests that call the same command multiple times with different
      expected outputs (e.g. git check-ignore for different paths).
    - By args: matches on the command args. Useful when call order may vary.

    For tests that also need filesystem setup (e.g. .gitignore files that the
    tested code reads), create those files in tmp_path BEFORE replaying.
    """

    def __init__(
        self,
        entries: list[dict[str, Any]],
        match_by_args: bool = False,
    ) -> None:
        self._entries = list(entries)
        self._call_index = 0
        self._match_by_args = match_by_args
        # Build args→result index for by-args matching
        self._args_index: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            key = _args_key(entry["args"])
            self._args_index.setdefault(key, []).append(entry)

    @classmethod
    def load(cls, path: Path, match_by_args: bool = False) -> "GitFixturePlayer":
        """Load fixtures from a JSON file."""
        data = json.loads(path.read_text())
        return cls(data, match_by_args=match_by_args)

    def run(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        """Return the recorded result for this command.

        Raises:
            KeyError: If no matching fixture entry found.
        """
        if self._match_by_args:
            return self._match_args(args)
        return self._match_sequential(args)

    def _match_sequential(self, args: list[str]) -> FakeCompletedProcess:
        if self._call_index >= len(self._entries):
            raise KeyError(
                f"No fixture entry for call #{self._call_index}: {args}. "
                f"Only {len(self._entries)} entries recorded."
            )
        entry = self._entries[self._call_index]
        self._call_index += 1
        return FakeCompletedProcess.from_dict(entry["result"])

    def _match_args(self, args: list[str]) -> FakeCompletedProcess:
        key = _args_key(args)
        entries = self._args_index.get(key, [])
        if not entries:
            raise KeyError(
                f"No fixture entry matching args: {args}. "
                f"Available: {list(self._args_index.keys())}"
            )
        # Pop first match (supports multiple calls with same args)
        entry = entries.pop(0)
        return FakeCompletedProcess.from_dict(entry["result"])

    def subprocess_run(self, args: Any, **kwargs: Any) -> FakeCompletedProcess:
        """Drop-in replacement for subprocess.run that replays fixtures.

        Accepts and ignores keyword arguments (cwd, capture_output, text,
        check, env, timeout) to match subprocess.run's signature.

        Handles the check=True behavior: if check is True and the recorded
        returncode is non-zero, raises subprocess.CalledProcessError.
        """
        args_list = list(args) if not isinstance(args, list) else args
        result = self.run(args_list)
        if kwargs.get("check") and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, args_list,
                output=result.stdout, stderr=result.stderr,
            )
        return result


def _args_key(args: list[str]) -> str:
    """Create a hashable key from command args, normalizing paths."""
    return " ".join(args)
