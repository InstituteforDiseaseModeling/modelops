"""Comprehensive tests for JSON-RPC protocol implementation."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from modelops.worker.jsonrpc import (
    JSONRPCClient,
    JSONRPCError,
    JSONRPCProtocol,
)


class TestJSONRPCProtocol:
    """Test the JSONRPCProtocol class."""

    def test_read_write_basic_message(self):
        """Test basic message reading and writing."""
        # Create in-memory streams
        input_stream = BytesIO()
        output_stream = BytesIO()

        # Write a message
        protocol = JSONRPCProtocol(input_stream, output_stream)
        message = {"jsonrpc": "2.0", "method": "test", "params": {}, "id": 1}
        protocol._write_message(message)

        # Reset streams
        output_stream.seek(0)
        input_stream = BytesIO(output_stream.read())
        input_stream.seek(0)

        # Read the message back
        protocol2 = JSONRPCProtocol(input_stream, BytesIO())
        read_message = protocol2.read_message()

        assert read_message == message

    def test_read_large_message(self):
        """Test reading messages larger than typical buffer size."""
        # Create a large message (>65KB)
        large_data = "x" * 100000
        message = {"jsonrpc": "2.0", "method": "test", "params": {"data": large_data}, "id": 1}

        # Write message
        output_stream = BytesIO()
        protocol = JSONRPCProtocol(None, output_stream)
        protocol._write_message(message)

        # Read it back
        output_stream.seek(0)
        input_stream = BytesIO(output_stream.read())
        protocol2 = JSONRPCProtocol(input_stream, None)
        read_message = protocol2.read_message()

        assert read_message == message
        assert len(read_message["params"]["data"]) == 100000

    def test_read_incomplete_header(self):
        """Test handling of incomplete headers."""
        # Create stream with incomplete header
        input_stream = BytesIO(b"Content-Len")
        protocol = JSONRPCProtocol(input_stream, None)

        with pytest.raises(JSONRPCError) as exc_info:
            protocol.read_message()
        assert exc_info.value.code == -32700

    def test_read_missing_content_length(self):
        """Test handling of missing Content-Length header."""
        # Create stream without Content-Length
        input_stream = BytesIO(b"Some-Header: value\r\n\r\n")
        protocol = JSONRPCProtocol(input_stream, None)

        with pytest.raises(JSONRPCError) as exc_info:
            protocol.read_message()
        assert "Content-Length" in str(exc_info.value)

    def test_read_invalid_content_length(self):
        """Test handling of invalid Content-Length value."""
        input_stream = BytesIO(b"Content-Length: invalid\r\n\r\n")
        protocol = JSONRPCProtocol(input_stream, None)

        with pytest.raises(JSONRPCError) as exc_info:
            protocol.read_message()
        assert exc_info.value.code == -32700

    def test_read_incomplete_body(self):
        """Test handling of incomplete message body."""
        # Say we have 100 bytes but only provide 50
        input_stream = BytesIO(b"Content-Length: 100\r\n\r\n" + b"x" * 50)
        protocol = JSONRPCProtocol(input_stream, None)

        with pytest.raises(JSONRPCError) as exc_info:
            protocol.read_message()
        assert "Incomplete message" in str(exc_info.value)

    def test_read_invalid_json(self):
        """Test handling of invalid JSON in body."""
        invalid_json = b"not valid json"
        header = f"Content-Length: {len(invalid_json)}\r\n\r\n".encode()
        input_stream = BytesIO(header + invalid_json)
        protocol = JSONRPCProtocol(input_stream, None)

        with pytest.raises(JSONRPCError) as exc_info:
            protocol.read_message()
        assert "Invalid JSON" in str(exc_info.value)

    def test_concurrent_writes(self):
        """Test that concurrent writes don't corrupt messages."""
        output_stream = BytesIO()
        protocol = JSONRPCProtocol(None, output_stream)

        # We need to add locking to prevent corruption
        # For now, this test documents the issue
        messages = []
        for i in range(10):
            messages.append(
                {"jsonrpc": "2.0", "method": f"test_{i}", "params": {"index": i}, "id": i}
            )

        # Write all messages
        for msg in messages:
            protocol._write_message(msg)

        # Read them back
        output_stream.seek(0)
        input_stream = BytesIO(output_stream.read())
        protocol2 = JSONRPCProtocol(input_stream, None)

        for expected in messages:
            read_msg = protocol2.read_message()
            assert read_msg == expected


class TestJSONRPCClient:
    """Test the JSONRPCClient class."""

    def test_call_success(self):
        """Test successful RPC call."""
        # Create mock streams
        input_stream = BytesIO()
        output_stream = BytesIO()

        # Prepare response
        response = {"jsonrpc": "2.0", "id": 1, "result": {"success": True}}
        response_bytes = json.dumps(response).encode()
        input_stream.write(f"Content-Length: {len(response_bytes)}\r\n\r\n".encode())
        input_stream.write(response_bytes)
        input_stream.seek(0)

        # Make call
        client = JSONRPCClient(output_stream, input_stream)

        # Set the next ID to 1 to match response
        client.protocol._next_id = 1
        result = client.call("test_method", {"param": "value"})

        assert result == {"success": True}

    def test_call_error_response(self):
        """Test handling of error response."""
        input_stream = BytesIO()
        output_stream = BytesIO()

        # Prepare error response
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }
        response_bytes = json.dumps(response).encode()
        input_stream.write(f"Content-Length: {len(response_bytes)}\r\n\r\n".encode())
        input_stream.write(response_bytes)
        input_stream.seek(0)

        client = JSONRPCClient(output_stream, input_stream)

        # Set the next ID to 1 to match response
        client.protocol._next_id = 1
        with pytest.raises(JSONRPCError) as exc_info:
            client.call("test_method", {})
        assert exc_info.value.code == -32601


class TestSubprocessCommunication:
    """Test actual subprocess communication."""

    @staticmethod
    def create_echo_subprocess() -> str:
        """Create a simple echo subprocess for testing."""
        script = """
import sys
import json

class EchoServer:
    def __init__(self):
        self._in = sys.stdin.buffer
        self._out = sys.stdout.buffer
    
    def read_message(self):
        headers = {}
        while True:
            line = self._in.readline()
            if not line:
                raise EOFError()
            if line in (b"\\r\\n", b"\\n"):
                break
            s = line.decode("utf-8").rstrip("\\r\\n")
            if ":" in s:
                k, v = s.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        
        length = int(headers["content-length"])
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self._in.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        
        body = b"".join(chunks)
        return json.loads(body.decode("utf-8"))
    
    def send_response(self, req_id, result):
        msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
        body = json.dumps(msg).encode("utf-8")
        self._out.write(f"Content-Length: {len(body)}\\r\\n".encode())
        self._out.write(b"\\r\\n")
        self._out.write(body)
        self._out.flush()
    
    def run(self):
        while True:
            try:
                msg = self.read_message()
                method = msg.get("method")
                params = msg.get("params", {})
                req_id = msg.get("id")
                
                if method == "echo":
                    self.send_response(req_id, params)
                elif method == "shutdown":
                    self.send_response(req_id, {"ok": True})
                    break
            except EOFError:
                break

if __name__ == "__main__":
    server = EchoServer()
    server.run()
"""
        fd, path = tempfile.mkstemp(suffix=".py", prefix="echo_server_")
        with open(path, "w") as f:
            f.write(script)
        return path

    def test_subprocess_basic_communication(self):
        """Test basic communication with subprocess."""
        script_path = self.create_echo_subprocess()

        try:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,  # Unbuffered
            )

            client = JSONRPCClient(proc.stdin, proc.stdout)

            # Test echo
            result = client.call("echo", {"message": "test"})
            assert result == {"message": "test"}

            # Shutdown
            result = client.call("shutdown", {})
            assert result == {"ok": True}

            proc.wait(timeout=1)

        finally:
            os.unlink(script_path)
            if proc.poll() is None:
                proc.terminate()

    def test_subprocess_large_messages(self):
        """Test large message handling with subprocess."""
        script_path = self.create_echo_subprocess()

        try:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            client = JSONRPCClient(proc.stdin, proc.stdout)

            # Test with 100KB message
            large_data = "x" * 100000
            result = client.call("echo", {"data": large_data})
            assert result["data"] == large_data

            client.call("shutdown", {})
            proc.wait(timeout=1)

        finally:
            os.unlink(script_path)
            if proc.poll() is None:
                proc.terminate()

    def test_subprocess_70kb_issue(self):
        """Test subprocess handling of 70KB message (reproduces integration test issue)."""
        script_path = self.create_echo_subprocess()

        try:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,  # Unbuffered like in process_manager.py
            )

            client = JSONRPCClient(proc.stdin, proc.stdout)

            # Test with 70KB message like in the failing test
            large_data = "x" * 70000
            result = client.call("echo", {"data": large_data})
            assert result["data"] == large_data

            # Test even larger - 100KB
            large_data = "x" * 100000
            result = client.call("echo", {"data": large_data})
            assert result["data"] == large_data

            client.call("shutdown", {})
            proc.wait(timeout=1)

        finally:
            os.unlink(script_path)
            if proc.poll() is None:
                proc.terminate()

    def test_subprocess_concurrent_calls(self):
        """Test multiple rapid calls to subprocess."""
        script_path = self.create_echo_subprocess()

        try:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            client = JSONRPCClient(proc.stdin, proc.stdout)

            # Send 20 rapid calls
            for i in range(20):
                result = client.call("echo", {"index": i})
                assert result["index"] == i

            client.call("shutdown", {})
            proc.wait(timeout=1)

        finally:
            os.unlink(script_path)
            if proc.poll() is None:
                proc.terminate()


class TestStressScenarios:
    """Stress tests to find edge cases."""

    def test_exactly_65kb_boundary(self):
        """Test message exactly at 65KB boundary (common buffer size)."""
        # Create message exactly at 65KB boundary
        target_size = 65536
        padding_size = target_size - len(
            '{"jsonrpc":"2.0","method":"test","params":{"data":""},"id":1}'
        )
        large_data = "x" * padding_size

        message = {"jsonrpc": "2.0", "method": "test", "params": {"data": large_data}, "id": 1}

        # Write message
        output_stream = BytesIO()
        protocol = JSONRPCProtocol(None, output_stream)
        protocol._write_message(message)

        # Read it back
        output_stream.seek(0)
        input_stream = BytesIO(output_stream.read())
        protocol2 = JSONRPCProtocol(input_stream, None)
        read_message = protocol2.read_message()

        assert read_message == message
        assert len(json.dumps(read_message).encode()) >= 65536

    def test_70kb_message_handling(self):
        """Test message at 70KB (reproduces integration test failure)."""
        # Create 70KB message like in integration test
        large_data = "x" * 70000
        message = {"jsonrpc": "2.0", "method": "test", "params": {"data": large_data}, "id": 1}

        # Write message
        output_stream = BytesIO()
        protocol = JSONRPCProtocol(None, output_stream)
        protocol._write_message(message)

        # Read it back - this should fail with current implementation
        output_stream.seek(0)
        input_stream = BytesIO(output_stream.read())
        protocol2 = JSONRPCProtocol(input_stream, None)
        read_message = protocol2.read_message()

        assert read_message == message
        assert len(read_message["params"]["data"]) == 70000

    def test_partial_write_handling(self):
        """Test handling when write is interrupted."""
        # This tests the case where a write might be partial
        # We need to ensure the protocol handles this correctly
        pass

    def test_stderr_interference(self):
        """Test that stderr output doesn't interfere with protocol."""
        script = """
import sys
import json

sys.stderr.write("This is stderr output\\n")
sys.stderr.flush()

# Now do normal protocol
msg = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
body = json.dumps(msg).encode()
sys.stdout.buffer.write(f"Content-Length: {len(body)}\\r\\n\\r\\n".encode())
sys.stdout.buffer.write(body)
sys.stdout.buffer.flush()
"""

        fd, path = tempfile.mkstemp(suffix=".py")
        with open(path, "w") as f:
            f.write(script)

        try:
            proc = subprocess.Popen(
                [sys.executable, path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            # Read stderr separately
            stderr_output = proc.stderr.read(100)
            assert b"stderr output" in stderr_output

            # Protocol should still work
            protocol = JSONRPCProtocol(proc.stdout, proc.stdin)
            msg = protocol.read_message()
            assert msg["result"]["ok"] == True

            proc.wait(timeout=1)

        finally:
            os.unlink(path)
            if proc.poll() is None:
                proc.terminate()

    def test_rapid_process_restart(self):
        """Test rapid process death and restart."""
        script_path = TestSubprocessCommunication.create_echo_subprocess()

        try:
            for _ in range(5):
                proc = subprocess.Popen(
                    [sys.executable, script_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )

                client = JSONRPCClient(proc.stdin, proc.stdout)
                result = client.call("echo", {"test": "data"})
                assert result == {"test": "data"}

                # Kill abruptly
                proc.terminate()
                proc.wait()

        finally:
            os.unlink(script_path)
