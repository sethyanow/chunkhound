"""Regression test for the integration-tier outbound-network block.

Asserts that the autouse conftest fixture `_block_outbound_network_for_integration`
prevents integration-marked tests from reaching the public internet via
Python-level `socket.socket.connect`.

Subprocess children (codex, curl, git, etc.) bypass this hook — see AGENTS.md
"Test Tiers" for the full rule.
"""
from __future__ import annotations

import socket

import pytest


@pytest.mark.integration
def test_outbound_tcp_connect_is_blocked():
    """Attempting a non-loopback TCP connect raises RuntimeError."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="Integration tier forbids"):
            s.connect(("1.1.1.1", 53))
    finally:
        s.close()


@pytest.mark.integration
def test_loopback_tcp_connect_reaches_real_stack():
    """Loopback addresses are allowed through; connect fails normally
    (nothing listening) but NOT with our RuntimeError."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        # Port chosen to avoid typical listeners; connect should fail with
        # ConnectionRefusedError or similar OSError — NOT RuntimeError.
        with pytest.raises(OSError) as exc_info:
            s.connect(("127.0.0.1", 59321))
        assert "Integration tier forbids" not in str(exc_info.value), (
            "Loopback connect was incorrectly blocked by the tier fixture."
        )
    finally:
        s.close()
