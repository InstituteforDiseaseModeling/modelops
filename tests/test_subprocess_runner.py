"""Test subprocess_runner with large messages."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from modelops.worker.jsonrpc import JSONRPCClient


class TestSubprocessRunnerLargeMessages:
    """Test subprocess_runner handling of large messages."""
    
    def test_subprocess_runner_70kb_params(self, tmp_path):
        """Test subprocess_runner with 70KB parameters."""
        # Create a minimal test bundle
        bundle_path = tmp_path / "test_bundle"
        bundle_path.mkdir()

        # Create a simple wire function
        (bundle_path / "wire.py").write_text("""
def wire(entrypoint, params, seed):
    return {"result": b"ok"}
""")

        # Create pyproject.toml with entry point
        (bundle_path / "pyproject.toml").write_text("""
[project]
name = "test-bundle"
version = "0.1.0"

[project.entry-points."modelops.bundle"]
test_bundle = "wire:wire"
""")

        # Create a proper venv using subprocess
        venv_path = tmp_path / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)

        # Get venv Python
        venv_python = venv_path / "bin" / "python"
        
        # Get subprocess_runner path
        runner_script = Path(__file__).parent.parent / "src" / "modelops" / "worker" / "subprocess_runner.py"
        
        # Start subprocess
        proc = subprocess.Popen(
            [
                str(venv_python),
                str(runner_script),
                "--bundle-path", str(bundle_path),
                "--venv-path", str(venv_path),
                "--bundle-digest", "test123"
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0  # Unbuffered like in process_manager
        )
        
        try:
            # Create JSON-RPC client
            client = JSONRPCClient(proc.stdin, proc.stdout)
            
            # Wait for ready
            result = client.call("ready", {})
            assert result.get("ready") == True
            
            # Send execute with 70KB params
            large_data = "x" * 70000
            params = {
                "entrypoint": "test",
                "params": {"data": large_data},
                "seed": 42
            }
            
            # This should work without hanging or crashing
            result = client.call("execute", params)
            assert "error" not in result or result.get("error") is None
            
            # Shutdown
            client.call("shutdown", {})
            proc.wait(timeout=5)
            
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            
            # Check stderr for any errors
            stderr = proc.stderr.read()
            if stderr:
                print(f"Subprocess stderr: {stderr.decode('utf-8', errors='replace')}")
