"""Warm process management for efficient subprocess reuse."""

import base64
import logging
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .jsonrpc import JSONRPCClient

logger = logging.getLogger(__name__)


@dataclass
class WarmProcess:
    """A warm subprocess ready to execute tasks."""
    
    process: subprocess.Popen
    client: JSONRPCClient
    bundle_digest: str
    use_count: int = 0
    
    def is_alive(self) -> bool:
        """Check if the process is still running."""
        return self.process.poll() is None
    
    def terminate(self):
        """Terminate the process gracefully."""
        if self.is_alive():
            try:
                # Try graceful shutdown first
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Force kill if graceful shutdown fails
                self.process.kill()
                self.process.wait()


class WarmProcessManager:
    """Manages a pool of warm subprocesses for bundle execution.
    
    Keeps processes warm and reuses them for the same bundle digest
    to avoid repeated initialization overhead. Uses LRU eviction
    when the pool is full.
    """
    
    def __init__(self, max_processes: int = 128, venvs_dir: Path = Path("/tmp/modelops/venvs")):
        """Initialize the process manager.
        
        Args:
            max_processes: Maximum number of warm processes to maintain
            venvs_dir: Directory for virtual environments
        """
        self.max_processes = max_processes
        self.venvs_dir = Path(venvs_dir)
        self.venvs_dir.mkdir(parents=True, exist_ok=True)
        
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
        # Check if we have a warm process for this digest
        if bundle_digest in self._processes:
            process = self._processes[bundle_digest]
            
            # Verify it's still alive
            if process.is_alive():
                # Move to end (most recently used)
                self._processes.move_to_end(bundle_digest)
                process.use_count += 1
                logger.debug(f"Reusing warm process for bundle {bundle_digest[:12]} "
                           f"(use #{process.use_count})")
                return process
            else:
                # Process died, remove it
                logger.warning(f"Warm process for bundle {bundle_digest[:12]} died")
                del self._processes[bundle_digest]
        
        # Need to create a new process
        if len(self._processes) >= self.max_processes:
            # Evict least recently used
            self._evict_lru()
        
        # Create new warm process
        process = self._create_process(bundle_digest, bundle_path)
        self._processes[bundle_digest] = process
        return process
    
    def _create_process(self, bundle_digest: str, bundle_path: Path) -> WarmProcess:
        """Create a new warm subprocess.
        
        Args:
            bundle_digest: Bundle digest
            bundle_path: Path to the bundle
            
        Returns:
            New WarmProcess
        """
        logger.info(f"Creating warm process for bundle {bundle_digest[:12]}")
        
        # Set up the virtual environment path
        venv_path = self.venvs_dir / bundle_digest
        
        # Create the subprocess
        # The subprocess will:
        # 1. Set up its virtual environment
        # 2. Install bundle dependencies
        # 3. Start JSON-RPC server and wait for tasks
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "modelops.worker.subprocess_runner",
                "--bundle-path", str(bundle_path),
                "--venv-path", str(venv_path),
                "--bundle-digest", bundle_digest
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0  # Unbuffered for real-time communication
        )
        
        # Create JSON-RPC client for communication
        client = JSONRPCClient(process.stdin, process.stdout)
        
        # Wait for ready signal
        try:
            result = client.call("ready", {})  # Pass empty params dict
            if not result.get("ready"):
                raise RuntimeError(f"Process not ready: {result}")
        except Exception as e:
            process.terminate()
            process.wait()
            raise RuntimeError(f"Failed to initialize process: {e}")
        
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
            # Execute task via JSON-RPC
            result = process.client.call(
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
            if bundle_digest in self._processes:
                del self._processes[bundle_digest]
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
