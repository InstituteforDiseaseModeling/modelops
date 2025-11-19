"""Minimal JSON-RPC implementation for subprocess communication.

Uses Content-Length framing like Language Server Protocol for robust
message boundary detection over stdio pipes.
"""

import logging
import queue
import sys
import threading
from typing import Any, BinaryIO, Dict

logger = logging.getLogger(__name__)


class JSONRPCError(Exception):
    """Base exception for JSON-RPC errors."""

    def __init__(self, code: int, message: str, data: Any | None = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"JSON-RPC Error {code}: {message}")


class JSONRPCProtocol:
    """Minimal JSON-RPC 2.0 protocol handler with Content-Length framing.

    This implements just enough JSON-RPC to handle our subprocess
    communication needs. Uses Content-Length headers like LSP for
    reliable message framing over pipes.
    """

    def __init__(self, input_stream: BinaryIO = None, output_stream: BinaryIO = None):
        """Initialize protocol handler.

        Args:
            input_stream: Binary input stream to read from
            output_stream: Binary output stream to write to
        """
        # Default to stdin/stdout in binary mode
        if input_stream is None:
            input_stream = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
        if output_stream is None:
            output_stream = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout

        self.input_stream = input_stream
        self.output_stream = output_stream
        self._next_id = 1

    def send_request(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC request.

        Args:
            method: Method name to call
            params: Parameters for the method
        """
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        self._next_id += 1
        self._write_message(request)

    def send_response(self, request_id: int, result: Any) -> None:
        """Send a JSON-RPC response.

        Args:
            request_id: ID of the request being responded to
            result: Result data
        """
        response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        self._write_message(response)

    def send_error(
        self,
        request_id: int | None,
        code: int,
        message: str,
        data: Any | None = None,
    ) -> None:
        """Send a JSON-RPC error response.

        Args:
            request_id: ID of the request that caused the error (None for parse errors)
            code: Error code
            message: Error message
            data: Additional error data
        """
        error_response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        if data is not None:
            error_response["error"]["data"] = data

        self._write_message(error_response)

    def read_message(self) -> dict[str, Any]:
        """Read a JSON-RPC message with Content-Length framing.

        Returns:
            Parsed JSON-RPC message

        Raises:
            JSONRPCError: If message is invalid
            EOFError: If stream is closed
        """
        # Read headers (binary mode)
        headers = {}
        while True:
            line = self.input_stream.readline()
            if not line:
                raise EOFError("Stream closed while reading headers")

            # Decode from bytes and strip CRLF
            try:
                line_str = line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                # Partial or corrupted read
                raise JSONRPCError(-32700, f"Invalid header encoding: {line[:20]!r}")

            if not line_str:
                # Empty line marks end of headers
                break

            # Parse header
            if ":" not in line_str:
                # Check if this might be a partial read
                if len(line_str) < 10 and line_str.isalpha():
                    # Likely a partial header - the stream is corrupted
                    raise JSONRPCError(-32700, f"Invalid header (partial read?): {line_str}")
                raise JSONRPCError(-32700, f"Invalid header: {line_str}")

            key, value = line_str.split(":", 1)
            # Store headers with lowercase keys for case-insensitive lookup
            headers[key.strip().lower()] = value.strip()

        # Check for Content-Length (case-insensitive)
        if "content-length" not in headers:
            raise JSONRPCError(-32700, "Missing Content-Length header")

        try:
            content_length = int(headers["content-length"])
        except ValueError:
            raise JSONRPCError(-32700, f"Invalid Content-Length: {headers['content-length']}")

        # Read body (binary mode - already in bytes)
        # CRITICAL: Must read in a loop as read() may return fewer bytes than requested
        body_chunks = []
        bytes_remaining = content_length
        while bytes_remaining > 0:
            chunk = self.input_stream.read(bytes_remaining)
            if not chunk:
                # EOF before getting all data
                raise JSONRPCError(
                    -32700,
                    f"Incomplete message: expected {content_length} bytes, got {content_length - bytes_remaining}",
                )
            body_chunks.append(chunk)
            bytes_remaining -= len(chunk)

        body = b"".join(body_chunks)

        # Parse JSON from bytes
        try:
            # Import json locally to avoid Python 3.13 scope issue
            import json as json_module

            message = json_module.loads(body.decode("utf-8"))
        except ValueError as e:
            # Note: Using ValueError to catch JSON decode errors in Python 3.13
            raise JSONRPCError(-32700, f"Invalid JSON: {e}")

        # Validate JSON-RPC structure
        if not isinstance(message, dict):
            raise JSONRPCError(-32600, "Message must be an object")

        if message.get("jsonrpc") != "2.0":
            raise JSONRPCError(-32600, "Invalid or missing jsonrpc version")

        return message

    def _write_message(self, message: dict[str, Any]) -> None:
        """Write a JSON-RPC message with Content-Length framing.

        Args:
            message: Message to send
        """
        # Serialize to JSON and encode to bytes
        # Import json locally to avoid Python 3.13 scope issue
        import json as json_module

        body = json_module.dumps(message, separators=(",", ":"))
        body_bytes = body.encode("utf-8")

        # Write Content-Length header (as bytes)
        header = f"Content-Length: {len(body_bytes)}\r\n\r\n"
        self.output_stream.write(header.encode("utf-8"))

        # Write body (as bytes - this was the bug!)
        self.output_stream.write(body_bytes)
        self.output_stream.flush()


class JSONRPCServer:
    """Simple JSON-RPC server for subprocess side."""

    def __init__(self, protocol: JSONRPCProtocol | None = None):
        """Initialize server.

        Args:
            protocol: Protocol handler (creates default if None)
        """
        self.protocol = protocol or JSONRPCProtocol()
        self.handlers = {}

    def register(self, method: str, handler):
        """Register a method handler.

        Args:
            method: Method name
            handler: Callable that handles the method
        """
        self.handlers[method] = handler

    def serve_forever(self):
        """Run the server loop, processing messages until EOF."""
        while True:
            try:
                message = self.protocol.read_message()

                # Check if it's a request
                if "method" not in message:
                    # Not a request, ignore
                    continue

                method = message["method"]
                params = message.get("params", {})
                request_id = message.get("id")

                # Look up handler
                if method not in self.handlers:
                    if request_id is not None:
                        self.protocol.send_error(request_id, -32601, f"Method not found: {method}")
                    continue

                # Call handler
                try:
                    result = self.handlers[method](**params)
                    if request_id is not None:
                        self.protocol.send_response(request_id, result)
                except Exception as e:
                    logger.exception(f"Error handling method {method}")
                    if request_id is not None:
                        self.protocol.send_error(request_id, -32603, "Internal error", str(e))

            except EOFError:
                # Normal termination
                break
            except JSONRPCError as e:
                # Protocol error
                logger.error(f"Protocol error: {e}")
                self.protocol.send_error(None, e.code, e.message, e.data)
            except Exception:
                # Unexpected error
                logger.exception("Unexpected error in server loop")
                break


class JSONRPCClient:
    """Simple JSON-RPC client for parent process side."""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO):
        """Initialize client with stdin/stdout of subprocess.

        Args:
            stdin: Subprocess stdin (for writing) - binary mode
            stdout: Subprocess stdout (for reading) - binary mode
        """
        # Note: We write to subprocess stdin and read from its stdout
        self.protocol = JSONRPCProtocol(input_stream=stdout, output_stream=stdin)
        self._lock = threading.Lock()
        self._pending: Dict[int, queue.Queue] = {}
        self._reader_exc: Exception | None = None
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self):
        """Background reader that dispatches responses to waiting callers."""
        while True:
            try:
                message = self.protocol.read_message()
            except Exception as exc:
                logger.error(f"Reader thread terminating: {exc}")
                self._reader_exc = exc
                self._dispatch_exception_to_all(exc)
                break

            request_id = message.get("id")
            if request_id is None:
                logger.warning(f"Ignoring message without id: {message}")
                continue

            queue_for_id = None
            with self._lock:
                queue_for_id = self._pending.get(request_id)

            if queue_for_id:
                queue_for_id.put(message)
            else:
                logger.warning(f"No pending call for message id {request_id}: {message}")

    def _dispatch_exception_to_all(self, exc: Exception) -> None:
        """Push exception to all pending queues and clear them."""
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()

        for _, q in items:
            q.put(exc)

    def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        """Call a remote method and wait for response.

        Args:
            method: Method name to call
            params: Method parameters as a dict

        Returns:
            Result from the method

        Raises:
            JSONRPCError: If remote method returns an error
        """
        # Track request ID
        request_id = self.protocol._next_id
        response_queue: queue.Queue = queue.Queue(maxsize=1)

        with self._lock:
            if self._reader_exc:
                raise self._reader_exc
            self._pending[request_id] = response_queue

        # Send request
        self.protocol.send_request(method, params)

        try:
            try:
                message = response_queue.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"JSON-RPC call '{method}' timed out after {timeout} seconds")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

        if isinstance(message, Exception):
            raise message

        if "error" in message:
            error = message["error"]
            raise JSONRPCError(error["code"], error["message"], error.get("data"))

        return message.get("result")
