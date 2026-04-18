import os
import shutil
import subprocess

import pytest

from chunkhound.providers.llm.codex_cli_provider import CodexCLIProvider


@pytest.mark.integration
def test_codex_exec_help_available():
    """Smoke-check that `codex exec --help` runs successfully.

    Skips if Codex CLI is not available on PATH and `CHUNKHOUND_CODEX_BIN` is unset.
    Uses a temporary `CODEX_HOME` to avoid touching user configuration/history.
    """
    codex_bin = os.getenv("CHUNKHOUND_CODEX_BIN") or shutil.which("codex")
    if not codex_bin:
        pytest.skip("Codex CLI not found; set CHUNKHOUND_CODEX_BIN or install `codex`.")

    env = os.environ.copy()
    provider = CodexCLIProvider(model="codex")
    base_home = provider._get_base_codex_home()
    if not base_home:
        pytest.xfail("No base CODEX_HOME found to inherit auth from.")

    overlay = provider._build_overlay_home()
    try:
        env["CODEX_HOME"] = overlay
        proc = subprocess.run(
            [codex_bin, "exec", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=30,
            check=False,
        )
    finally:
        shutil.rmtree(overlay, ignore_errors=True)

    # `--help` should succeed and print usage text
    combined = (proc.stdout + proc.stderr).decode("utf-8", errors="ignore").lower()
    assert proc.returncode == 0, f"codex exec --help failed: rc={proc.returncode}, out={combined!r}"
    assert "usage" in combined and "codex exec" in combined, (
        "Help output did not contain expected usage text. Output was: " + combined
    )
