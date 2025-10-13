"""Warm process management for efficient subprocess reuse.

WARNING: Parallel Testing Limitations
--------------------------------------
This module uses file-based locking to safely handle concurrent venv creation.
When running tests in parallel (e.g., with pytest-xdist), multiple test workers
may attempt to create/access the same venv simultaneously, causing deadlocks.

To avoid this in tests:
1. Run integration tests serially: pytest -n0 or make test-integration-serial
2. Set MODELOPS_FORCE_FRESH_VENV=true to force unique venvs per process
3. Use fixtures that properly isolate resources

The locking mechanism is essential for production where multiple workers
legitimately need to share the same venv cache.
"""

import base64
import hashlib
import logging
import os
import select
import subprocess
import sys
import fcntl
import uuid
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple, List, Any

from .jsonrpc import JSONRPCClient

logger = logging.getLogger(__name__)


@dataclass
class WarmProcess:
    """A warm subprocess ready to execute tasks."""

    process: subprocess.Popen
    client: JSONRPCClient
    bundle_digest: str
    use_count: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock)  # Reentrant lock for same thread
    
    def is_alive(self) -> bool:
        """Check if the process is still running."""
        return self.process.poll() is None
    
    def terminate(self):
        """Terminate the process gracefully."""
        with self._lock:
            if self.is_alive():
                try:
                    # Try graceful shutdown first
                    self.process.terminate()
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Force kill if graceful shutdown fails
                    self.process.kill()
                    self.process.wait()

    def safe_call(self, method: str, params: dict, timeout: float = 10.0):
        """Make a thread-safe JSON-RPC call to the subprocess.

        Args:
            method: Method name to call
            params: Method parameters
            timeout: Timeout in seconds

        Returns:
            Result from the subprocess

        Raises:
            Various exceptions if the call fails
        """
        with self._lock:
            if not self.is_alive():
                raise RuntimeError("Process is not alive")

            return self.client.call(method, params)


class WarmProcessManager:
    """Manages a pool of warm subprocesses for bundle execution.
    
    Keeps processes warm and reuses them for the same bundle digest
    to avoid repeated initialization overhead. Uses LRU eviction
    when the pool is full.
    """
    
    def __init__(self, max_processes: int = 128, venvs_dir: Path = Path("/tmp/modelops/venvs"), force_fresh_venv: bool = False):
        """Initialize the process manager.
        
        Args:
            max_processes: Maximum number of warm processes to maintain
            venvs_dir: Directory for virtual environments
            force_fresh_venv: Force fresh venv creation for each execution (debugging)
        """
        self.max_processes = max_processes
        self.venvs_dir = Path(venvs_dir)
        self.venvs_dir.mkdir(parents=True, exist_ok=True)
        self.force_fresh_venv = force_fresh_venv
        
        # Use OrderedDict for LRU behavior
        self._processes: OrderedDict[str, WarmProcess] = OrderedDict()
    
    def get_process(self, bundle_digest: str, bundle_path: Path) -> WarmProcess:
        """Get or create a warm process for the given bundle.
        
        Args:
            bundle_digest: SHA256 digest of the bundle
            bundle_path: Local path to the bundle
            
        Returns:
            WarmProcess ready to execute tasks
        """
        # Skip cache entirely when forcing fresh venvs
        if self.force_fresh_venv:
            logger.info(f"Forcing fresh venv for bundle {bundle_digest[:12]} (MODELOPS_FORCE_FRESH_VENV=true)")
            # Still need to check pool size
            if len(self._processes) >= self.max_processes:
                self._evict_lru()
            # Create new process with unique venv
            process = self._create_process_with_lock(bundle_digest, bundle_path)
            # Use a unique key for storage to prevent reuse
            unique_key = f"{bundle_digest}-{uuid.uuid4().hex[:8]}"
            self._processes[unique_key] = process
            return process
        
        # Check if we have a warm process for this digest
        if bundle_digest in self._processes:
            process = self._processes[bundle_digest]
            
            # Verify it's still alive
            if process.is_alive():
                # Validate the process still serves the correct digest
                # This can fail if the process is dying or pipes are broken
                try:
                    # Use safe_call to prevent race conditions
                    result = process.safe_call("ready", {}, timeout=5.0)
                    if result.get("bundle_digest") == bundle_digest:
                        # Move to end (most recently used)
                        self._processes.move_to_end(bundle_digest)
                        process.use_count += 1
                        logger.debug(f"Reusing warm process for bundle {bundle_digest[:12]} "
                                   f"(use #{process.use_count})")
                        return process
                    else:
                        logger.warning(f"Process digest mismatch for {bundle_digest[:12]}")
                        process.terminate()
                        del self._processes[bundle_digest]
                except (EOFError, BrokenPipeError, ConnectionError) as e:
                    # Expected errors when process is dying
                    logger.debug(f"Process appears to be dying: {e}")
                    process.terminate()
                    if bundle_digest in self._processes:
                        del self._processes[bundle_digest]
                except Exception as e:
                    # Unexpected errors - log as warning
                    logger.warning(f"Failed to validate process: {e}")
                    process.terminate()
                    if bundle_digest in self._processes:
                        del self._processes[bundle_digest]
            else:
                # Process died, remove it
                logger.warning(f"Warm process for bundle {bundle_digest[:12]} died")
                del self._processes[bundle_digest]
        
        # Need to create a new process
        if len(self._processes) >= self.max_processes:
            # Evict least recently used
            self._evict_lru()
        
        # Create new warm process with locking to prevent races
        process = self._create_process_with_lock(bundle_digest, bundle_path)
        self._processes[bundle_digest] = process
        return process
    
    def _create_process_with_lock(self, bundle_digest: str, bundle_path: Path) -> WarmProcess:
        """Create process with filesystem lock to prevent concurrent venv creation.
        
        Args:
            bundle_digest: Bundle digest
            bundle_path: Path to bundle
            
        Returns:
            New WarmProcess
        """
        venv_key = self._get_venv_key(bundle_digest, bundle_path)
        lock_file = self.venvs_dir / f"{venv_key}.lock"
        
        # Ensure lock file exists
        lock_file.touch()
        
        with open(lock_file, 'r+') as lock:
            # Acquire exclusive lock
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                # Check if another process created the venv while we waited
                venv_path = self.venvs_dir / venv_key
                python_bin = venv_path / "bin" / "python"
                
                if venv_path.exists() and python_bin.exists():
                    logger.info(f"Venv created by another process: {venv_path}")
                    # Just start the subprocess, venv already exists
                    return self._start_subprocess(venv_path, bundle_path, bundle_digest)
                
                # Create the process (which creates venv)
                return self._create_process(bundle_digest, bundle_path)
            finally:
                # Release lock
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    
    def _start_subprocess(self, venv_path: Path, bundle_path: Path, bundle_digest: str) -> WarmProcess:
        """Start a subprocess with existing venv.
        
        Args:
            venv_path: Path to virtual environment
            bundle_path: Path to bundle
            bundle_digest: Bundle digest
            
        Returns:
            New WarmProcess
        """
        logger.info(f"Starting subprocess with existing venv: {venv_path}")
        
        # Get path to standalone runner script
        runner_script = Path(__file__).parent / "subprocess_runner.py"
        if not runner_script.exists():
            raise RuntimeError(f"Subprocess runner script not found: {runner_script}")
        
        # Use venv's Python for true isolation
        venv_python = venv_path / "bin" / "python"
        
        # Clean environment
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"  # Ensure unbuffered output
        
        # Start the subprocess with venv's Python and standalone runner
        # CRITICAL: Use DEVNULL for stderr to prevent deadlock with large messages
        # The subprocess logs to stderr, and if the stderr pipe buffer fills up
        # while we're writing large data to stdin, we get a deadlock.
        process = subprocess.Popen(
            [
                str(venv_python),  # Use venv's Python for clean isolation
                str(runner_script),
                "--bundle-path", str(bundle_path),
                "--venv-path", str(venv_path),
                "--bundle-digest", bundle_digest
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # TEMPORARY: Capture for debugging
            text=False,  # Binary mode for proper Content-Length framing
            bufsize=0,   # Unbuffered for immediate communication
            close_fds=True,  # Prevent fd leakage
            cwd=str(bundle_path),  # Run from bundle directory so relative paths work
            env=env
        )
        
        # Check for immediate failure
        if process.poll() is not None:
            # Process died immediately - capture all available output
            stderr = process.stderr.read() if process.stderr else b""
            stdout = process.stdout.read() if process.stdout else b""
            
            # Decode with error handling
            stderr_text = stderr.decode('utf-8', errors='replace') if stderr else "No stderr output"
            stdout_text = stdout.decode('utf-8', errors='replace') if stdout else "No stdout output"
            
            logger.error(f"Subprocess died immediately with exit code {process.returncode}")
            logger.error(f"Stderr: {stderr_text}")
            logger.error(f"Stdout: {stdout_text}")
            
            # Include both in error message for debugging
            error_msg = f"Subprocess failed to start (exit {process.returncode}).\nStderr: {stderr_text}\nStdout: {stdout_text}"
            raise RuntimeError(error_msg)
        
        # Create JSON-RPC client
        client = JSONRPCClient(process.stdin, process.stdout)
        
        # Wait for ready signal
        try:
            # Create WarmProcess first so we can use safe_call
            warm_process = WarmProcess(
                process=process,
                client=client,
                bundle_digest=bundle_digest,
                use_count=1
            )

            result = warm_process.safe_call("ready", {}, timeout=10.0)
            if not result.get("ready"):
                raise RuntimeError(f"Process not ready: {result}")

            return warm_process
        except Exception as e:
            # Try to capture any stderr before killing process
            stderr_output = ""
            if process.stderr and process.poll() is None:
                try:
                    # Non-blocking read of available stderr
                    import select
                    if select.select([process.stderr], [], [], 0.1)[0]:
                        stderr_bytes = process.stderr.read()
                        if stderr_bytes:
                            stderr_output = stderr_bytes.decode('utf-8', errors='replace')
                            logger.error(f"Subprocess stderr during initialization:\n{stderr_output}")
                except Exception:
                    pass  # Best effort

            process.terminate()
            process.wait()

            # Include stderr in the error message if available
            error_msg = f"Failed to initialize process: {e}"
            if stderr_output:
                error_msg += f"\nSubprocess stderr:\n{stderr_output}"

            raise RuntimeError(error_msg)
    
    def _compute_deps_hash(self, bundle_path: Path) -> str:
        """Compute hash of dependency files.
        
        Args:
            bundle_path: Path to the bundle
            
        Returns:
            16-character hex digest of dependencies
        """
        hasher = hashlib.blake2b(digest_size=8)  # 16 hex chars
        
        # Hash lock files and dependency specs
        for dep_file in ["uv.lock", "poetry.lock", "requirements.txt", "pyproject.toml"]:
            dep_path = bundle_path / dep_file
            if dep_path.exists():
                hasher.update(dep_path.read_bytes())
        
        return hasher.hexdigest()
    
    def _get_venv_key(self, bundle_digest: str, bundle_path: Path) -> str:
        """Generate venv directory name with all isolation factors.

        Args:
            bundle_digest: Bundle content hash
            bundle_path: Path to bundle

        Returns:
            Venv key like: {digest[:12]}-py3.11-{deps[:8]}
            Or with force_fresh_venv: {digest[:12]}-py3.11-{deps[:8]}-{uuid[:8]}
        """
        py_version = f"py{sys.version_info.major}.{sys.version_info.minor}"
        deps_hash = self._compute_deps_hash(bundle_path)
        base_key = f"{bundle_digest[:12]}-{py_version}-{deps_hash[:8]}"

        # Add unique suffix when forcing fresh venvs
        if self.force_fresh_venv:
            unique_id = str(uuid.uuid4())[:8]
            return f"{base_key}-{unique_id}"

        return base_key
    
    def _create_process(self, bundle_digest: str, bundle_path: Path) -> WarmProcess:
        """Create a new warm subprocess.
        
        Args:
            bundle_digest: Bundle digest
            bundle_path: Path to the bundle
            
        Returns:
            New WarmProcess
        """
        logger.info(f"Creating warm process for bundle {bundle_digest[:12]}")
        
        # Generate venv key with Python version and deps hash
        venv_key = self._get_venv_key(bundle_digest, bundle_path)
        venv_path = self.venvs_dir / venv_key
        logger.info(f"Using venv path: {venv_path}")
        
        # Create venv if it doesn't exist
        venv_python = venv_path / "bin" / "python"
        if not venv_path.exists():
            logger.info(f"Creating new venv at {venv_path}")
            try:
                subprocess.run(
                    ["uv", "venv", str(venv_path)],
                    check=True,
                    capture_output=True,
                    text=True
                )
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to create venv: {e.stderr}")
                raise
        
        # Get path to standalone runner script
        runner_script = Path(__file__).parent / "subprocess_runner.py"
        if not runner_script.exists():
            raise RuntimeError(f"Subprocess runner script not found: {runner_script}")
        
        # Clean environment
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"  # Ensure unbuffered output
        
        # Create the subprocess using venv's Python
        # The subprocess will:
        # 1. Install bundle dependencies into venv
        # 2. Discover wire function via entry points
        # 3. Start JSON-RPC server and wait for tasks
        # CRITICAL: Use DEVNULL for stderr to prevent deadlock with large messages
        # TEMPORARY: Capture stderr for debugging subprocess failures
        process = subprocess.Popen(
            [
                str(venv_python),  # Use venv's Python for clean isolation
                str(runner_script),
                "--bundle-path", str(bundle_path),
                "--venv-path", str(venv_path),
                "--bundle-digest", bundle_digest
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # TEMPORARY: Capture for debugging
            text=False,  # Binary mode for proper Content-Length framing
            bufsize=0,   # Unbuffered for immediate communication
            close_fds=True,  # Prevent fd leakage
            cwd=str(bundle_path),  # Run from bundle directory so relative paths work
            env=env
        )
        
        # Check for immediate failure
        if process.poll() is not None:
            # Process died immediately - capture all available output
            stderr = process.stderr.read() if process.stderr else b""
            stdout = process.stdout.read() if process.stdout else b""
            
            # Decode with error handling
            stderr_text = stderr.decode('utf-8', errors='replace') if stderr else "No stderr output"
            stdout_text = stdout.decode('utf-8', errors='replace') if stdout else "No stdout output"
            
            logger.error(f"Subprocess died immediately with exit code {process.returncode}")
            logger.error(f"Stderr: {stderr_text}")
            logger.error(f"Stdout: {stdout_text}")
            
            # Include both in error message for debugging
            error_msg = f"Subprocess failed to start (exit {process.returncode}).\nStderr: {stderr_text}\nStdout: {stdout_text}"
            raise RuntimeError(error_msg)
        
        # Create JSON-RPC client for communication
        client = JSONRPCClient(process.stdin, process.stdout)
        
        # Wait for ready signal
        try:
            result = client.call("ready", {})  # Pass empty params dict
            if not result.get("ready"):
                raise RuntimeError(f"Process not ready: {result}")
        except Exception as e:
            # Try to capture any available stderr
            stderr_output = None
            stderr_text = ""
            if process.stderr:
                try:
                    # Try to read any available stderr without blocking
                    # Make stderr non-blocking
                    flags = fcntl.fcntl(process.stderr.fileno(), fcntl.F_GETFL)
                    fcntl.fcntl(process.stderr.fileno(), fcntl.F_SETFL, flags | os.O_NONBLOCK)

                    # Read any available stderr
                    stderr_output = process.stderr.read()
                    if stderr_output:
                        stderr_text = stderr_output.decode('utf-8', errors='replace')
                        logger.error(f"Subprocess stderr: {stderr_text}")
                except Exception as read_err:
                    logger.debug(f"Could not read stderr: {read_err}")

            process.terminate()
            process.wait()

            error_msg = f"Failed to initialize process: {e}"
            if stderr_text:
                error_msg += f"\nStderr: {stderr_text}"
            raise RuntimeError(error_msg)
        
        return WarmProcess(
            process=process,
            client=client,
            bundle_digest=bundle_digest,
            use_count=1
        )
    
    def _evict_lru(self):
        """Evict the least recently used process."""
        if not self._processes:
            return
        
        # Get least recently used (first item)
        digest, process = next(iter(self._processes.items()))
        
        logger.info(f"Evicting LRU process for bundle {digest[:12]} "
                   f"(used {process.use_count} times)")
        
        # Terminate the process
        process.terminate()
        
        # Remove from pool
        del self._processes[digest]
    
    def execute_task(self, bundle_digest: str, bundle_path: Path,
                     entrypoint: str, params: Dict, seed: int) -> Dict[str, str]:
        """Execute a task in a warm process.
        
        Args:
            bundle_digest: Bundle digest
            bundle_path: Path to the bundle
            entrypoint: Entrypoint identifying model and scenario
            params: Task parameters
            seed: Random seed
            
        Returns:
            Task results as dict of artifact name to base64-encoded strings
        """
        # Get or create warm process
        process = self.get_process(bundle_digest, bundle_path)
        
        try:
            # Execute task via JSON-RPC using safe_call
            result = process.safe_call(
                "execute",
                {
                    "entrypoint": entrypoint,
                    "params": params,
                    "seed": seed
                }
            )
            
            # Result should already be base64-encoded strings
            return result
            
        except Exception as e:
            # Process might be broken, remove it
            logger.error(f"Task execution failed: {e}")
            process.terminate()
            # Can't reliably remove by digest when force_fresh_venv is True
            # Just remove the matching process object if we find it
            for key, proc in list(self._processes.items()):
                if proc is process:
                    del self._processes[key]
                    break
            raise
    
    def execute_aggregation(
        self, 
        bundle_digest: str, 
        bundle_path: Path,
        target_entrypoint: str,
        sim_returns: List[Dict[str, Any]],  # Already serialized SimReturns
        target_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute aggregation task in a warm process.
        
        This reuses the SAME warm process pool as simulations!
        The process already has the bundle installed and can run
        target evaluation code just like simulation code.
        
        Args:
            bundle_digest: Bundle digest (for process selection)
            bundle_path: Path to the bundle
            target_entrypoint: Target evaluation entrypoint
            sim_returns: List of simulation results (already serialized)
            target_data: Optional empirical data
            
        Returns:
            Aggregation result with loss and diagnostics
        """
        # Get or create warm process - SAME pool as simulations!
        process = self.get_process(bundle_digest, bundle_path)
        
        try:
            # Execute aggregation via JSON-RPC using safe_call
            result = process.safe_call(
                "aggregate",  # New method!
                {
                    "target_entrypoint": target_entrypoint,
                    "sim_returns": sim_returns,
                    "target_data": target_data
                }
            )
            
            # Check for errors
            if 'error' in result:
                from modelops.utils.error_utils import format_aggregation_error
                raise RuntimeError(format_aggregation_error(result))
            
            return result
            
        except Exception as e:
            logger.error(f"Aggregation execution failed: {e}")
            # Process might be dead, remove it
            process.terminate()
            # Can't reliably remove by digest when force_fresh_venv is True
            # Just remove the matching process object if we find it
            for key, proc in list(self._processes.items()):
                if proc is process:
                    del self._processes[key]
                    break
            raise
    
    def shutdown_all(self):
        """Shutdown all warm processes."""
        logger.info(f"Shutting down {len(self._processes)} warm processes")
        
        for digest, process in list(self._processes.items()):
            logger.debug(f"Terminating process for bundle {digest[:12]}")
            process.terminate()
        
        self._processes.clear()
    
    def active_count(self) -> int:
        """Return the count of active processes."""
        return len(self._processes)
