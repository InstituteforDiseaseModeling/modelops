"""Subprocess runner for isolated execution.

This module is executed as a subprocess by WarmProcessManager.
It sets up the environment and runs simulation tasks via JSON-RPC.

Supports both requirements.txt and pyproject.toml for dependencies.
Discovers wire function via Python entry points.
"""

import argparse
import base64
import importlib.metadata
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Callable

from .jsonrpc import JSONRPCServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr  # Log to stderr to keep stdout for JSON-RPC
)
logger = logging.getLogger(__name__)


class SubprocessRunner:
    """Runs inside the subprocess to execute simulation tasks."""
    
    def __init__(self, bundle_path: Path, venv_path: Path, bundle_digest: str):
        """Initialize the runner.
        
        Args:
            bundle_path: Path to the bundle directory
            venv_path: Path for the virtual environment
            bundle_digest: Digest of the bundle
        """
        self.bundle_path = bundle_path
        self.venv_path = venv_path
        self.bundle_digest = bundle_digest
        self._setup_environment()
    
    def _setup_environment(self):
        """Set up the virtual environment and install dependencies."""
        logger.info(f"Setting up environment for bundle {self.bundle_digest[:12]}")
        
        # Check if venv already exists (from previous run)
        if self.venv_path.exists():
            logger.info(f"Using existing venv at {self.venv_path}")
        else:
            # Create new virtual environment using uv
            logger.info(f"Creating new venv at {self.venv_path}")
            try:
                subprocess.run(
                    ["uv", "venv", str(self.venv_path)],
                    check=True,
                    capture_output=True,
                    text=True
                )
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to create venv: {e.stderr}")
                raise
            
            # Install dependencies from bundle
            self._install_dependencies()
        
        # Add bundle path to Python path for imports
        if str(self.bundle_path) not in sys.path:
            sys.path.insert(0, str(self.bundle_path))
        
        # Discover the wire function via entry points
        self.wire_fn = self._discover_wire_function()
    
    def _install_dependencies(self):
        """Install bundle dependencies using uv.
        
        Supports both pyproject.toml and requirements.txt.
        """
        pyproject_file = self.bundle_path / "pyproject.toml"
        requirements_file = self.bundle_path / "requirements.txt"
        
        if pyproject_file.exists():
            logger.info("Installing dependencies from pyproject.toml")
            try:
                # For pyproject.toml, use uv pip install with the directory
                subprocess.run(
                    [
                        "uv", "pip", "install",
                        "--python", str(self.venv_path / "bin" / "python"),
                        str(self.bundle_path)  # Install the bundle itself
                    ],
                    check=True,
                    capture_output=True,
                    text=True
                )
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to install from pyproject.toml: {e.stderr}")
                raise
        elif requirements_file.exists():
            logger.info("Installing dependencies from requirements.txt")
            try:
                subprocess.run(
                    [
                        "uv", "pip", "install",
                        "--python", str(self.venv_path / "bin" / "python"),
                        "-r", str(requirements_file)
                    ],
                    check=True,
                    capture_output=True,
                    text=True
                )
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to install from requirements.txt: {e.stderr}")
                raise
        else:
            logger.warning("No dependencies file found (pyproject.toml or requirements.txt)")
    
    def _discover_wire_function(self) -> Callable:
        """Discover wire function via Python entry points.
        
        Looks for the 'modelops.wire' entry point group.
        Calabaria (or another framework) should register its wire function there.
        
        Returns:
            The wire function callable
            
        Raises:
            RuntimeError: If no wire function found or multiple found
        """
        eps = importlib.metadata.entry_points(group='modelops.wire')
        
        if not eps:
            raise RuntimeError(
                "No modelops.wire entry point found. "
                "Ensure Calabaria is installed in the environment. "
                "The framework should register an entry point like: "
                "[project.entry-points.'modelops.wire'] execute = 'calabaria.wire:wire_function'"
            )
        
        eps_list = list(eps)
        if len(eps_list) > 1:
            names = [ep.name for ep in eps_list]
            raise RuntimeError(
                f"Multiple modelops.wire entry points found: {names}. "
                "Only one wire implementation should be installed."
            )
        
        # Load the entry point
        ep = eps_list[0]
        logger.info(f"Using wire function from entry point: {ep.name} = {ep.value}")
        return ep.load()
    
    def ready(self) -> Dict[str, Any]:
        """Signal that the subprocess is ready.
        
        Returns:
            Ready status
        """
        return {
            "ready": True,
            "bundle_digest": self.bundle_digest,
            "pid": os.getpid()
        }
    
    def execute(self, entrypoint: str, params: Dict[str, Any], seed: int) -> Dict[str, str]:
        """Execute a simulation task using discovered wire function.
        
        Args:
            entrypoint: Entrypoint identifying model and scenario
            params: Simulation parameters
            seed: Random seed
            
        Returns:
            Base64-encoded artifacts
        """
        logger.info(f"Executing {entrypoint} with seed {seed}")
        
        try:
            # Call the discovered wire function
            # It returns Dict[str, bytes] per the WireFunction protocol
            result_bytes = self.wire_fn(entrypoint, params, seed)
            
            # Base64 encode for JSON transport
            artifacts = {}
            for name, data in result_bytes.items():
                if not isinstance(data, bytes):
                    logger.warning(f"Wire function returned non-bytes for {name}, converting")
                    if isinstance(data, str):
                        data = data.encode()
                    else:
                        data = json.dumps(data).encode()
                
                artifacts[name] = base64.b64encode(data).decode('ascii')
            
            return artifacts
            
        except Exception as e:
            logger.exception("Simulation execution failed")
            # Return error as artifact
            error_data = json.dumps({
                "error": str(e),
                "type": type(e).__name__,
                "entrypoint": entrypoint
            }).encode()
            return {
                "error": base64.b64encode(error_data).decode('ascii')
            }


def main():
    """Main entry point for subprocess runner."""
    parser = argparse.ArgumentParser(description="ModelOps subprocess runner")
    parser.add_argument("--bundle-path", required=True, help="Path to bundle directory")
    parser.add_argument("--venv-path", required=True, help="Path for virtual environment")
    parser.add_argument("--bundle-digest", required=True, help="Bundle digest")
    
    args = parser.parse_args()
    
    # Create runner
    runner = SubprocessRunner(
        bundle_path=Path(args.bundle_path),
        venv_path=Path(args.venv_path),
        bundle_digest=args.bundle_digest
    )
    
    # Create JSON-RPC server
    server = JSONRPCServer()
    
    # Register methods
    server.register("ready", runner.ready)
    server.register("execute", runner.execute)
    
    # Run server
    logger.info("Starting JSON-RPC server")
    server.serve_forever()
    logger.info("JSON-RPC server stopped")


if __name__ == "__main__":
    main()
