"""JSON-RPC 2.0 transport over stdio for LSP communication.

Zero chunkhound imports — stdlib only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from chunkhound.lsp.types import LSPTimeoutError, LSPTransportError

logger = logging.getLogger(__name__)


class JsonRpcTransport:
    """Async JSON-RPC 2.0 transport over subprocess stdio.

    Features:
    - Content-Length byte-counted framing (not character count)
    - Concurrent request support via ID-keyed Future dict
    - Notification routing (pluggable handler, default discard-with-log)
    - Malformed JSON tolerance (log + skip, don't crash)
    - Per-request timeout with Future cleanup
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        notification_handler: Callable[[str, dict[str, Any] | None], None]
        | None = None,
    ) -> None:
        self._process = process
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notification_handler = (
            notification_handler or self._default_notification_handler
        )
        self._read_task: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    async def create(
        cls,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        *,
        notification_handler: Callable[[str, dict[str, Any] | None], None]
        | None = None,
    ) -> JsonRpcTransport:
        """Spawn a subprocess and create a transport for it."""
        proc = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        transport = cls(proc, notification_handler=notification_handler)
        transport._read_task = asyncio.create_task(transport._read_loop())
        return transport

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the response."""
        if self._closed:
            raise LSPTransportError("Transport is closed")

        self._request_id += 1
        req_id = self._request_id

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[req_id] = future

        await self._write_message(message)

        try:
            if timeout is not None:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise LSPTimeoutError(
                f"Request {method} (id={req_id}) timed out after {timeout}s"
            ) from None
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            raise

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._closed:
            raise LSPTransportError("Transport is closed")

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            message["params"] = params

        await self._write_message(message)

    async def close(self) -> None:
        """Shut down the transport and terminate the subprocess."""
        if self._closed:
            return
        self._closed = True

        # Cancel read loop
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        # Cancel all pending requests
        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    LSPTransportError("Transport closed while request pending")
                )
        self._pending.clear()

        # Terminate process
        proc = self._process
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    @property
    def is_alive(self) -> bool:
        """Check if the subprocess is still running."""
        return self._process.returncode is None

    async def _write_message(self, message: dict[str, Any]) -> None:
        """Encode and write a JSON-RPC message with Content-Length header."""
        stdin = self._process.stdin
        if stdin is None:
            raise LSPTransportError("Process stdin not available")

        body = json.dumps(message).encode("utf-8")
        # Content-Length MUST be byte length, not character count
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        stdin.write(header + body)
        await stdin.drain()

    async def _read_loop(self) -> None:
        """Read JSON-RPC messages from stdout, routing responses and notifications."""
        stdout = self._process.stdout
        if stdout is None:
            return

        try:
            while not self._closed:
                content_length = await self._read_headers(stdout)
                if content_length is None:
                    break  # EOF

                body_bytes = await stdout.readexactly(content_length)
                try:
                    message = json.loads(body_bytes.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    logger.warning("Malformed JSON-RPC message, skipping: %s", exc)
                    continue

                self._dispatch_message(message)

        except asyncio.IncompleteReadError:
            logger.debug("LSP server closed stdout (incomplete read)")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error in LSP read loop")
        finally:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(
                        LSPTransportError("Read loop exited unexpectedly")
                    )
            self._pending.clear()

    async def _read_headers(self, stdout: asyncio.StreamReader) -> int | None:
        """Read Content-Length from headers. Returns None on EOF."""
        content_length: int | None = None

        while True:
            line = await stdout.readline()
            if not line:
                return None  # EOF

            line_str = line.decode("ascii").strip()
            if not line_str:
                break  # Empty line = end of headers

            if line_str.lower().startswith("content-length:"):
                try:
                    content_length = int(line_str.split(":", 1)[1].strip())
                except ValueError:
                    logger.warning("Invalid Content-Length header: %s", line_str)

        if content_length is None:
            logger.warning("Missing Content-Length header")
            return None

        return content_length

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        """Route a parsed message to the correct handler."""
        if "id" in message and "method" not in message:
            # Response to a request
            req_id = message["id"]
            future = self._pending.pop(req_id, None)
            if future is None:
                logger.warning("Response for unknown request ID: %s", req_id)
                return
            if not future.done():
                if "error" in message:
                    future.set_exception(
                        LSPTransportError(
                            f"LSP error {message['error'].get('code', '?')}: "
                            f"{message['error'].get('message', 'unknown')}"
                        )
                    )
                else:
                    future.set_result(message.get("result", {}))
        elif "method" in message and "id" not in message:
            # Notification (no id)
            self._notification_handler(message["method"], message.get("params"))
        elif "method" in message and "id" in message:
            # Server-initiated request — must respond or server blocks
            asyncio.ensure_future(self._respond_to_server_request(message))
        else:
            logger.warning("Unclassifiable JSON-RPC message: %s", message)

    async def _respond_to_server_request(self, message: dict[str, Any]) -> None:
        """Respond to server-initiated requests (e.g. window/workDoneProgress/create).

        Servers block on these until the client responds. We accept all
        well-known requests with a null result.
        """
        req_id = message["id"]
        method = message.get("method", "")
        logger.debug("Responding to server request: %s (id=%s)", method, req_id)

        response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": None,
        }
        try:
            await self._write_message(response)
        except LSPTransportError:
            logger.warning(
                "Failed to respond to server request %s (id=%s)", method, req_id
            )

    @staticmethod
    def _default_notification_handler(
        method: str, params: dict[str, Any] | None
    ) -> None:
        """Default: discard notifications with debug log."""
        logger.debug("LSP notification (discarded): %s", method)
