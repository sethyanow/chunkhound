"""Unit tests for LSP client notification handlers.

Tests _handle_notification for window/logMessage and $/progress notifications.
"""

import logging

import pytest

from chunkhound.lsp.client import LSPClient
from chunkhound.lsp.types import ServerConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def client() -> LSPClient:
    """Create an LSPClient without starting a server (for unit-testing handlers)."""
    config = ServerConfig(language_id="python", command=None)
    return LSPClient(config)


class TestWindowLogMessage:
    """Tests for window/logMessage notification routing."""

    def test_type_1_routes_to_error(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"type": 1, "message": "something broke"})
        assert any(r.levelno == logging.ERROR and "something broke" in r.message for r in caplog.records)

    def test_type_2_routes_to_warning(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"type": 2, "message": "watch out"})
        assert any(r.levelno == logging.WARNING and "watch out" in r.message for r in caplog.records)

    def test_type_3_routes_to_info(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"type": 3, "message": "fyi"})
        assert any(r.levelno == logging.INFO and "fyi" in r.message for r in caplog.records)

    def test_type_4_routes_to_debug(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"type": 4, "message": "verbose"})
        assert any(r.levelno == logging.DEBUG and "verbose" in r.message for r in caplog.records)

    def test_missing_type_defaults_to_debug(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"message": "no type field"})
        assert any(r.levelno == logging.DEBUG and "no type field" in r.message for r in caplog.records)

    def test_params_none_does_not_crash(self, client: LSPClient) -> None:
        # Must not raise AttributeError
        client._handle_notification("window/logMessage", None)


class TestProgressNotification:
    """Tests for $/progress notification lifecycle tracking."""

    def test_begin_creates_progress_entry(self, client: LSPClient) -> None:
        client._handle_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "begin", "title": "Indexing"},
        })
        assert "t1" in client._progress
        assert client._progress["t1"]["title"] == "Indexing"

    def test_report_updates_percentage(self, client: LSPClient) -> None:
        # Setup: begin first
        client._handle_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "begin", "title": "Indexing"},
        })
        # Report
        client._handle_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "report", "percentage": 50},
        })
        assert client._progress["t1"]["percentage"] == 50

    def test_end_removes_progress_entry(self, client: LSPClient) -> None:
        # Setup: begin first
        client._handle_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "begin", "title": "Indexing"},
        })
        # End
        client._handle_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "end"},
        })
        assert "t1" not in client._progress

    def test_report_for_unknown_token_no_error(self, client: LSPClient) -> None:
        # Report for token never begun — must not KeyError
        client._handle_notification("$/progress", {
            "token": "unknown",
            "value": {"kind": "report", "percentage": 75},
        })

    def test_end_for_unknown_token_no_error(self, client: LSPClient) -> None:
        # End for token never begun — must not KeyError
        client._handle_notification("$/progress", {
            "token": "unknown",
            "value": {"kind": "end"},
        })

    def test_params_none_does_not_crash(self, client: LSPClient) -> None:
        client._handle_notification("$/progress", None)

    def test_missing_token_does_not_create_none_entry(self, client: LSPClient) -> None:
        # No token field → must not create _progress[None]
        client._handle_notification("$/progress", {"value": {"kind": "begin", "title": "X"}})
        assert None not in client._progress


class TestAdversarialLogMessage:
    """Adversarial stress tests for window/logMessage handler."""

    def test_type_zero_defaults_to_debug(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        """Type 0 is not in LSP spec — should default to DEBUG, not crash."""
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"type": 0, "message": "zero"})
        assert any(r.levelno == logging.DEBUG and "zero" in r.message for r in caplog.records)

    def test_negative_type_defaults_to_debug(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"type": -1, "message": "neg"})
        assert any(r.levelno == logging.DEBUG and "neg" in r.message for r in caplog.records)

    def test_type_as_string_defaults_to_debug(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        """JSON deserialization could yield string type from a buggy server."""
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"type": "1", "message": "strtype"})
        # String "1" won't match int key 1 in level_map → defaults to DEBUG
        assert any(r.levelno == logging.DEBUG and "strtype" in r.message for r in caplog.records)

    def test_empty_message(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"type": 3, "message": ""})
        assert any(r.levelno == logging.INFO for r in caplog.records)

    def test_multibyte_message(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {"type": 3, "message": "Indexing caf\u00e9 \u2603"})
        assert any("caf\u00e9" in r.message for r in caplog.records)

    def test_empty_params_dict(self, client: LSPClient, caplog: pytest.LogCaptureFixture) -> None:
        """Empty dict — valid but missing all fields."""
        with caplog.at_level(logging.DEBUG):
            client._handle_notification("window/logMessage", {})
        assert any(r.levelno == logging.DEBUG for r in caplog.records)


class TestAdversarialProgress:
    """Adversarial stress tests for $/progress handler."""

    def test_begin_twice_same_token_overwrites(self, client: LSPClient) -> None:
        """Second begin on same token should overwrite — not accumulate."""
        client._handle_notification("$/progress", {
            "token": "t1", "value": {"kind": "begin", "title": "First"},
        })
        client._handle_notification("$/progress", {
            "token": "t1", "value": {"kind": "begin", "title": "Second"},
        })
        assert client._progress["t1"]["title"] == "Second"
        assert len(client._progress) == 1

    def test_unknown_kind_is_ignored(self, client: LSPClient) -> None:
        """Kind not in begin/report/end — no crash, no state change."""
        client._handle_notification("$/progress", {
            "token": "t1", "value": {"kind": "cancel"},
        })
        assert "t1" not in client._progress

    def test_missing_kind_is_ignored(self, client: LSPClient) -> None:
        """Value dict with no kind field."""
        client._handle_notification("$/progress", {
            "token": "t1", "value": {},
        })
        assert "t1" not in client._progress

    def test_integer_token(self, client: LSPClient) -> None:
        """LSP spec allows integer tokens — must not crash."""
        client._handle_notification("$/progress", {
            "token": 42, "value": {"kind": "begin", "title": "Numeric"},
        })
        assert 42 in client._progress
        client._handle_notification("$/progress", {
            "token": 42, "value": {"kind": "end"},
        })
        assert 42 not in client._progress

    def test_many_concurrent_tokens(self, client: LSPClient) -> None:
        """20 tokens open simultaneously — stress dict sizing."""
        for i in range(20):
            client._handle_notification("$/progress", {
                "token": f"t{i}", "value": {"kind": "begin", "title": f"Task {i}"},
            })
        assert len(client._progress) == 20
        for i in range(20):
            client._handle_notification("$/progress", {
                "token": f"t{i}", "value": {"kind": "end"},
            })
        assert len(client._progress) == 0

    def test_report_after_end_is_noop(self, client: LSPClient) -> None:
        """Report arriving after end — stale notification, must not recreate entry."""
        client._handle_notification("$/progress", {
            "token": "t1", "value": {"kind": "begin", "title": "X"},
        })
        client._handle_notification("$/progress", {
            "token": "t1", "value": {"kind": "end"},
        })
        # Late report
        client._handle_notification("$/progress", {
            "token": "t1", "value": {"kind": "report", "percentage": 99},
        })
        assert "t1" not in client._progress

    def test_empty_value_dict(self, client: LSPClient) -> None:
        """Value is empty — no kind, no crash."""
        client._handle_notification("$/progress", {
            "token": "t1", "value": {},
        })
        assert "t1" not in client._progress
