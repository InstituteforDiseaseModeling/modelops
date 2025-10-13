#!/usr/bin/env python3
"""
Standalone subprocess runner for isolated execution.

CRITICAL: This module MUST remain standalone with NO ModelOps dependencies!

Why standalone is absolutely necessary:

1. This script runs inside isolated virtual environments (venvs) that contain
   ONLY the researcher/user's bundle dependencies, not ModelOps itself.
   
2. Environment isolation is crucial for:
   - Preventing dependency conflicts between bundles and ModelOps
   - Ensuring reproducible execution environments
   - Allowing bundles with incompatible dependencies to run on the same system
   - Maintaining clean separation between infrastructure (ModelOps) and science (bundles)

3. Even if ModelOps were available on PyPI, we would NOT install it in bundle
   venvs because:
   - Bundles may require different versions of libraries that ModelOps uses
   - We don't want bundle code to accidentally import/depend on ModelOps
   - The bundle environment should be exactly what the scientist specified

4. Communication pattern:
   - WarmProcessManager (has ModelOps) spawns this script with venv's Python
   - WarmProcess in proces_manager.py does use the jsonrpc.py module in ModelOps
   - This script (no ModelOps) runs inside the venv
   - Communication via JSON-RPC 2.0 over stdin/stdout (language-agnostic)
   - All data serialized to JSON/base64 for clean boundary

5. Why JSON-RPC is inlined here:
   - Cannot import from modelops.worker.jsonrpc (ModelOps not in venv)
   - Must be self-contained for true isolation
   - The protocol is simple enough to inline without issues

Debugging note: After extensive debugging, we found that mixing Python
environments (parent with ModelOps, child without) caused subtle issues.
The solution is complete isolation with this standalone runner.
"""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------
# Logging (stderr only; stdout is reserved for JSON-RPC)
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("runner")

# -----------------------------------------------------------------------------
# Minimal JSON-RPC 2.0 over stdio (LSP-style Content-Length framing)
# -----------------------------------------------------------------------------

class JSONRPCError(Exception):
    def __init__(self, code: int, message: str, data: Optional[Any] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

class JSONRPCProtocol:
    def __init__(self):
        # binary mode for exact byte lengths
        self._in = sys.stdin.buffer
        self._out = sys.stdout.buffer

    def _read_exactly(self, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            b = self._in.read(remaining)
            if not b:
                break
            chunks.append(b)
            remaining -= len(b)
        data = b"".join(chunks)
        if len(data) != n:
            raise JSONRPCError(-32700, f"Incomplete message body ({len(data)}/{n} bytes)")
        return data

    def read_message(self) -> Dict[str, Any]:
        headers: Dict[str, str] = {}
        # Read headers until blank line (accept CRLF or LF)
        while True:
            line = self._in.readline()
            if not line:
                raise EOFError("stdin closed")
            # Accept both CRLF and LF as blank line delimiter
            if line in (b"\r\n", b"\n"):
                break
            s = line.decode("utf-8").rstrip("\r\n")
            if ":" not in s:
                raise JSONRPCError(-32700, f"Invalid header: {s}")
            k, v = s.split(":", 1)
            # Store headers with lowercase keys for case-insensitive lookup
            headers[k.strip().lower()] = v.strip()

        if "content-length" not in headers:
            raise JSONRPCError(-32700, "Missing Content-Length header")
        try:
            length = int(headers["content-length"])
        except ValueError:
            raise JSONRPCError(-32700, f"Invalid Content-Length: {headers['content-length']}")

        body = self._read_exactly(length)
        try:
            # Import json locally to avoid Python 3.13 scope issue
            import json as json_module
            msg = json_module.loads(body.decode("utf-8"))
        except ValueError as e:
            # Note: Using ValueError to catch JSON decode errors in Python 3.13
            raise JSONRPCError(-32700, f"Invalid JSON: {e}")
        if not isinstance(msg, dict):
            raise JSONRPCError(-32600, "Message must be an object")
        return msg

    def _write(self, payload: Dict[str, Any]) -> None:
        # Import json locally to avoid Python 3.13 scope issue
        import json as json_module
        body = json_module.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._out.write(f"Content-Length: {len(body)}\r\n".encode("ascii"))
        self._out.write(b"\r\n")
        self._out.write(body)
        self._out.flush()

    def send_response(self, req_id: Any, result: Any) -> None:
        if req_id is None:
            return  # notifications get no response
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def send_error(self, req_id: Any, code: int, message: str, data: Optional[Any] = None) -> None:
        if req_id is None:
            logger.error("JSON-RPC error (notification): %s (%s)", message, code)
            return
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._write({"jsonrpc": "2.0", "id": req_id, "error": err})

# -----------------------------------------------------------------------------
# Subprocess Runner
# -----------------------------------------------------------------------------

class SubprocessRunner:
    """Executes simulation tasks inside the venv interpreter."""

    def __init__(self, bundle_path: Path, venv_path: Path, bundle_digest: str):
        self.bundle_path = bundle_path
        self.venv_path = venv_path
        self.bundle_digest = bundle_digest
        self.wire_fn: Optional[Callable[[str, Dict[str, Any], int], Dict[str, bytes]]] = None
        self._setup()

    # ------------------------- setup & installs -------------------------

    def _deps_fingerprint(self) -> str:
        # Fingerprint dependency inputs (stable across filesystem path changes)
        h = hashlib.blake2b(digest_size=16)
        for name in ("uv.lock", "poetry.lock", "requirements.txt", "pyproject.toml"):
            p = self.bundle_path / name
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()

    def _setup(self) -> None:
        logger.info("Setting up bundle %s", self.bundle_digest[:12])
        logger.info("Interpreter: %s", sys.executable)
        
        # Validate we're running in the expected venv
        if not sys.executable.startswith(str(self.venv_path)):
            logger.warning(
                "Interpreter mismatch: expected %s/bin/python, got %s",
                self.venv_path, sys.executable
            )
        
        deps_marker = self.venv_path / ".deps_installed"
        wanted = self._deps_fingerprint()

        need_install = True
        if deps_marker.exists():
            have = deps_marker.read_text().strip()
            if have == wanted:
                # Functional verification: try to discover the wire function
                # This is the ultimate test that everything is properly installed
                try:
                    # Try to discover wire function (but don't save it yet)
                    test_wire = self._discover_wire_function()
                    # If we got here, installation is good
                    need_install = False
                    logger.info("Dependencies verified (wire function discovered successfully)")
                except Exception as e:
                    # Discovery failed - packages not properly installed
                    logger.warning("Marker exists but wire discovery failed (%s), will reinstall", e)
                    # Delete the bad marker
                    if deps_marker.exists():
                        deps_marker.unlink()
                    need_install = True

        if need_install:
            # Use file locking to prevent concurrent installations
            lock_file = self.venv_path / ".install.lock"
            lock_file.touch(exist_ok=True)
            
            with open(lock_file, 'r+') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    # Check again after acquiring lock (double-check pattern)
                    if deps_marker.exists():
                        have = deps_marker.read_text().strip()
                        if have == wanted:
                            logger.info("Dependencies installed by another process")
                            need_install = False
                    
                    if need_install:
                        try:
                            self._install_dependencies()
                            # Only write marker if installation succeeded
                            self._atomic_write(deps_marker, wanted)
                        except Exception as e:
                            logger.error("Failed to install dependencies: %s", e)
                            # Clean up bad marker if it somehow exists
                            if deps_marker.exists():
                                deps_marker.unlink()
                            # Don't write marker if installation failed
                            raise
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        self.wire_fn = self._discover_wire_function()

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write file atomically using temp file + rename.
        
        Args:
            path: Target file path
            content: Content to write
        """
        # Create temp file in same directory for atomic rename
        temp_fd, temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp"
        )
        try:
            os.write(temp_fd, content.encode('utf-8'))
            os.close(temp_fd)
            # Atomic rename (on same filesystem)
            os.replace(temp_path, path)
        except:
            os.close(temp_fd)
            os.unlink(temp_path)
            raise
    
    def _run(self, cmd: list, check: bool = True) -> int:
        logger.info("Running: %s", " ".join(cmd))
        # Use existing environment to preserve git config and GITHUB_TOKEN
        env = {**os.environ, "PYTHONNOUSERSITE": "1"}  # keep user-site out
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(self.bundle_path))
        if result.stdout:
            logger.debug(result.stdout)
        if result.returncode != 0:
            logger.error(result.stderr)
            if check:
                raise RuntimeError(f"Install failed: {' '.join(cmd)}")
        return result.returncode

    def _configure_git_auth(self) -> None:
        """Configure git to use GitHub token if available."""
        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token:
            logger.info("Configuring git authentication for private repos")
            # Configure git to use the token for GitHub
            # This sets it globally for this process and subprocesses
            subprocess.run(
                ["git", "config", "--global",
                 f"url.https://x-access-token:{github_token}@github.com/.insteadOf",
                 "https://github.com/"],
                capture_output=True,
                text=True,
                check=False  # Don't fail if git config fails
            )
        else:
            # TODO: Remove this error post-dev when all repos are public
            logger.error("GITHUB_TOKEN not found in environment - private repos will fail to clone!")

    def _install_dependencies(self) -> None:
        logger.info("Installing dependencies for bundle at %s", self.bundle_path)
        pyproject = self.bundle_path / "pyproject.toml"
        requirements = self.bundle_path / "requirements.txt"

        # Check for problematic dependency configurations
        suspects = []
        for fname in ("pyproject.toml", "uv.lock", "requirements.txt", ".constraints.txt"):
            p = self.bundle_path / fname
            if p.exists():
                t = p.read_text()
                # Check for malformed numpy git URLs
                if "github.com/numpy/" in t and not "github.com/numpy/numpy" in t:
                    suspects.append(f"{fname}: malformed numpy git URL detected")
                # Check for problematic uv sources
                if "[tool.uv.sources]" in t and "numpy" in t:
                    suspects.append(f"{fname}: uv sources override for numpy detected")
                # Check for local file dependencies that won't work in container
                if "file:///" in t and "/Users/" in t:
                    suspects.append(f"{fname}: local file:// dependency won't work in container")

        if suspects:
            logger.warning("Bundle has problematic dependency configurations:\n  - %s", "\n  - ".join(suspects))
            # Clean up problematic files that might interfere
            for fname in (".constraints.txt", "uv.lock"):
                p = self.bundle_path / fname
                if p.exists():
                    logger.warning(f"Removing {fname} to avoid dependency resolution issues")
                    p.unlink()

        # Configure git auth before installing (for private repos)
        self._configure_git_auth()

        # Prefer uv if present (fast), else pip
        # IMPORTANT: We ARE running inside the venv (sys.executable is venv python)
        # But uv needs to be told which Python to use explicitly
        uv = shutil.which("uv")
        if pyproject.exists():
            logger.info("Found pyproject.toml, installing with %s", "uv" if uv else "pip")
            if uv:
                # Try uv first with explicit PyPI index to avoid config issues
                rc = self._run([uv, "pip", "install",
                               "--index-url", "https://pypi.org/simple",
                               "--python", sys.executable,
                               str(self.bundle_path)], check=False)

                if rc != 0:
                    logger.warning("uv failed; falling back to pip (PyPI only)")
                    # Check if we're in an externally managed environment
                    try:
                        # Try to detect if pip will fail due to PEP 668
                        result = subprocess.run(
                            [sys.executable, "-m", "pip", "--version"],
                            capture_output=True, text=True, timeout=5
                        )
                        if "externally-managed" in result.stderr.lower():
                            logger.error("Python environment is externally managed, cannot use pip")
                            raise RuntimeError("Cannot install packages: Python is externally managed. Ensure uv is available.")
                    except (subprocess.TimeoutExpired, FileNotFoundError):
                        pass

                    # Try to ensure pip is available first
                    ensurepip_result = self._run([sys.executable, "-m", "ensurepip", "--upgrade"], check=False)
                    if ensurepip_result != 0:
                        logger.warning("ensurepip failed, pip may not be available")

                    # Try pip install with --break-system-packages as last resort for dev environments
                    # This is safe in a venv context
                    pip_args = [sys.executable, "-m", "pip", "install",
                               "--isolated", "--disable-pip-version-check",
                               "--no-cache-dir", "--no-input"]

                    # Add break-system-packages flag if we detect it might be needed
                    if "homebrew" in sys.executable.lower() or "/opt/homebrew" in sys.executable:
                        logger.warning("Detected Homebrew Python, adding --break-system-packages flag")
                        pip_args.append("--break-system-packages")

                    pip_args.append(str(self.bundle_path))
                    self._run(pip_args)
            else:
                self._run([sys.executable, "-m", "pip", "install",
                          "--isolated", "--disable-pip-version-check",
                          "--no-cache-dir", "--no-input",
                          str(self.bundle_path)])
        elif requirements.exists():
            logger.info("Found requirements.txt, installing with %s", "uv" if uv else "pip")
            if uv:
                # Try uv first with explicit PyPI index
                rc = self._run([uv, "pip", "install",
                               "--index-url", "https://pypi.org/simple",
                               "--python", sys.executable,
                               "-r", str(requirements)], check=False)

                if rc != 0:
                    logger.warning("uv failed; falling back to pip (PyPI only)")
                    # Similar handling as pyproject.toml case
                    ensurepip_result = self._run([sys.executable, "-m", "ensurepip", "--upgrade"], check=False)
                    if ensurepip_result != 0:
                        logger.warning("ensurepip failed, pip may not be available")

                    pip_args = [sys.executable, "-m", "pip", "install",
                               "--isolated", "--disable-pip-version-check",
                               "--no-cache-dir", "--no-input"]

                    if "homebrew" in sys.executable.lower() or "/opt/homebrew" in sys.executable:
                        logger.warning("Detected Homebrew Python, adding --break-system-packages flag")
                        pip_args.append("--break-system-packages")

                    pip_args.extend(["-r", str(requirements)])
                    self._run(pip_args)
            else:
                self._run([sys.executable, "-m", "pip", "install",
                          "--isolated", "--disable-pip-version-check",
                          "--no-cache-dir", "--no-input",
                          "-r", str(requirements)])
            # Ensure bundle code itself is importable when there's no pyproject
            if str(self.bundle_path) not in sys.path:
                sys.path.insert(0, str(self.bundle_path))
        else:
            logger.warning("No dependency file found (pyproject.toml or requirements.txt)")

    # ------------------------- wire discovery --------------------------

    def _discover_wire_function(self) -> Callable:
        import importlib
        import importlib.metadata as im

        importlib.invalidate_caches()

        # Try entry points first (works naturally under venv interpreter)
        try:
            eps_list = list(im.entry_points().select(group="modelops.wire"))
        except Exception:
            eps = im.entry_points(group="modelops.wire")
            eps_list = list(eps) if eps else []

        if eps_list:
            if len(eps_list) > 1:
                names = [ep.name for ep in eps_list]
                raise RuntimeError(f"Multiple wire entry points found: {names}")
            ep = eps_list[0]
            logger.info("Using wire entry point: %s = %s", ep.name, ep.value)
            logger.info("Bundle working directory: %s", self.bundle_path)
            logger.info("Current working directory: %s", os.getcwd())
            return ep.load()

        logger.info("No entry points found; trying manifest/convention fallback")
        return self._resolve_wire_fallback()

    def _resolve_wire_fallback(self) -> Callable:
        # 1) .modelops/manifest.json with {"wire": "module.or/path.py:func"}
        spec = None
        manifest = self.bundle_path / ".modelops" / "manifest.json"
        if manifest.exists():
            try:
                # Import json locally to avoid Python 3.13 scope issue
                import json as json_module
                spec = json_module.loads(manifest.read_text()).get("wire")
            except Exception:
                spec = None

        # 2) .modelops/modelops.toml (optional; only if tomllib is available)
        if spec is None:
            toml_path = self.bundle_path / ".modelops" / "modelops.toml"
            if toml_path.exists():
                try:
                    import tomllib  # py>=3.11
                    spec = tomllib.loads(toml_path.read_text())["modelops"]["wire"]
                except Exception:
                    spec = None

        if spec:
            return self._load_wire_spec(spec)

        # 3) Conventional: bundle root wire.py:wire
        candidate = self.bundle_path / "wire.py"
        if candidate.exists():
            return self._load_wire_file(candidate, "wire")

        raise RuntimeError("No wire specified: install an entry point, provide .modelops manifest, or add wire.py")

    def _load_wire_spec(self, spec: str) -> Callable:
        if ":" not in spec:
            raise RuntimeError(f"Invalid wire spec '{spec}', expected 'module.or/path.py:func'")
        mod, func = spec.split(":", 1)
        if mod.endswith(".py") or "/" in mod:
            path = (self.bundle_path / mod).resolve() if not mod.startswith("/") else Path(mod)
            return self._load_wire_file(path, func)
        # dotted module
        if str(self.bundle_path) not in sys.path:
            sys.path.insert(0, str(self.bundle_path))
        import importlib
        importlib.invalidate_caches()
        m = importlib.import_module(mod)
        f = getattr(m, func, None)
        if not callable(f):
            raise RuntimeError(f"{spec} is not callable")
        return f

    def _load_wire_file(self, path: Path, func: str) -> Callable:
        import importlib.util
        if not path.exists():
            raise RuntimeError(f"Wire file not found: {path}")
        spec = importlib.util.spec_from_file_location("bundle_wire", str(path))
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        assert spec and spec.loader
        spec.loader.exec_module(module)                 # type: ignore[assignment]
        f = getattr(module, func, None)
        if not callable(f):
            raise RuntimeError(f"{path}:{func} is not callable")
        return f

    # ------------------------- RPC methods -----------------------------

    def ready(self) -> Dict[str, Any]:
        return {
            "ready": True,
            "bundle_digest": self.bundle_digest,
            "python": sys.executable,
            "version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "pid": os.getpid(),
            "venv": str(self.venv_path),
        }

    def execute(
        self,
        entrypoint: str,
        params: Dict[str, Any],
        seed: int,
        bundle_digest: Optional[str] = None,
    ) -> Dict[str, str]:
        if bundle_digest and bundle_digest != self.bundle_digest:
            raise ValueError(f"Bundle digest mismatch: expected {self.bundle_digest}, got {bundle_digest}")

        logger.info("Executing %s (seed=%s)", entrypoint, seed)
        try:
            # Redirect stdout to stderr during wire function execution
            # This prevents user prints from corrupting JSON-RPC frames
            with contextlib.redirect_stdout(sys.stderr):
                result_bytes = self.wire_fn(entrypoint, params, seed)  # type: ignore[misc]
            
            artifacts: Dict[str, str] = {}
            for name, data in result_bytes.items():
                if not isinstance(data, (bytes, bytearray)):
                    logger.warning("Converting non-bytes result for %s", name)
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                    else:
                        # Import json locally to avoid Python 3.13 scope issue
                        import json as json_module
                        data = json_module.dumps(data).encode("utf-8")
                artifacts[name] = base64.b64encode(bytes(data)).decode("ascii")
            return artifacts
        except Exception as e:
            logger.exception("Execution failed")
            # Import json locally to avoid Python 3.13 scope issue
            import json as json_module
            err = json_module.dumps({"error": str(e), "type": type(e).__name__, "entrypoint": entrypoint}).encode("utf-8")
            return {"error": base64.b64encode(err).decode("ascii")}

    def aggregate(
        self,
        target_entrypoint: str,
        sim_returns: List[Dict[str, Any]],  # Serialized SimReturns
        target_data: Optional[Dict[str, Any]] = None,
        bundle_digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute target evaluation/aggregation in the subprocess.
        
        This runs a target entrypoint (like 'targets.covid/deaths') which
        computes loss by comparing simulation outputs to empirical data.
        
        Args:
            target_entrypoint: Target evaluation entrypoint
            sim_returns: List of simulation results (serialized)
            target_data: Optional empirical data for comparison
            bundle_digest: Optional bundle digest for validation
            
        Returns:
            Aggregation result with loss and diagnostics
        """
        # Import base64 at the top of the method to avoid scope issues
        import base64

        if bundle_digest and bundle_digest != self.bundle_digest:
            raise ValueError(f"Bundle digest mismatch: expected {self.bundle_digest}, got {bundle_digest}")

        logger.info("Executing aggregation %s with %d results", target_entrypoint, len(sim_returns))
        logger.info("Bundle path: %s", self.bundle_path)
        logger.info("Current sys.path (first 3): %s", sys.path[:3])

        try:
            # Ensure bundle is in sys.path for imports (only if it exists)
            if self.bundle_path.exists() and str(self.bundle_path) not in sys.path:
                logger.info("Adding bundle path to sys.path")
                sys.path.insert(0, str(self.bundle_path))
            
            # Parse target entrypoint to get import path and target name
            # Support both ":" separator (new format) and "/" separator (old format)
            if ":" in target_entrypoint:
                # New format: "targets.prevalence:prevalence_target"
                import_path, target_name = target_entrypoint.rsplit(":", 1)
            else:
                # Old format: "targets.covid/deaths"
                import_path, target_name = target_entrypoint.rsplit("/", 1)
            
            # Import the target module - handle different formats
            parts = import_path.split(".")
            logger.info("Import path parts: %s", parts)

            # Special case: if first part is 'dummy', skip it (for contract validation workaround)
            if parts[0] == "dummy" and len(parts) > 1:
                # Remove dummy prefix, use the rest
                import_path = ".".join(parts[1:])
                parts = parts[1:]
                logger.info("Removed dummy prefix, new import_path: %s", import_path)

            logger.info("Attempting to import module: %s", import_path)
            if len(parts) == 1:
                # Simple module like "targets"
                module = __import__(import_path)
                # Get the target function directly from module
                evaluator = getattr(module, target_name)
            elif len(parts) == 2:
                # Module.Class format like "targets.COVID"
                module_name, class_name = parts
                module = __import__(module_name, fromlist=[class_name])
                target_class = getattr(module, class_name)
                
                # Look for evaluate method or callable
                if hasattr(target_class, 'evaluate'):
                    evaluator = target_class.evaluate
                elif callable(target_class):
                    evaluator = target_class
                else:
                    # Try to get specific target method
                    evaluator = getattr(target_class, target_name)
            else:
                # Complex module path like "my.package.module"
                # Use importlib for better nested module handling
                import importlib
                module = importlib.import_module(import_path)
                # Try to get the target function/class
                if hasattr(module, target_name):
                    evaluator = getattr(module, target_name)
                else:
                    # Maybe it's a class with the last part as class name
                    *module_parts, class_name = parts
                    module_path = ".".join(module_parts)
                    module = importlib.import_module(module_path)
                    target_class = getattr(module, class_name)
                    evaluator = getattr(target_class, target_name)
            
            # Deserialize SimReturns back to objects
            # For now we'll pass the raw dicts since we don't have SimReturn class here
            # The evaluator will need to handle the dict format
            
            # Debug logging
            logger.info(f"Calling evaluator with {len(sim_returns)} returns")
            logger.info(f"First SimReturn keys: {list(sim_returns[0].keys()) if sim_returns else 'None'}")
            
            # Redirect stdout to stderr during evaluation
            with contextlib.redirect_stdout(sys.stderr):
                # Check if this is a Calabaria-style target that returns a Target object
                import inspect
                sig = inspect.signature(evaluator)

                if len(sig.parameters) == 0 or (len(sig.parameters) == 1 and 'data_paths' in sig.parameters):
                    # This is a Calabaria target function decorated with @calibration_target
                    # It returns a Target object that we need to evaluate
                    logger.info("Detected Calabaria-style target function")
                    target_obj = evaluator()  # Call with no args (decorator handles data paths)

                    # Convert sim_returns (dicts) to SimOutputs (DataFrames) for Calabaria
                    import polars as pl
                    import io
                    import base64

                    sim_outputs = []

                    for sim_return in sim_returns:
                        sim_output = {}
                        outputs = sim_return.get('outputs', {})

                        for name, table_artifact in outputs.items():
                            # Skip non-Arrow outputs (like 'metadata' which is JSON)
                            if name == 'metadata':
                                logger.debug(f"Skipping non-Arrow output: {name}")
                                continue

                            # Extract Arrow bytes from various formats (embedded logic)
                            try:
                                # Handle different artifact formats
                                if isinstance(table_artifact, bytes):
                                    arrow_bytes = table_artifact
                                elif isinstance(table_artifact, dict):
                                    data = table_artifact.get('inline') or table_artifact.get('data')
                                    if data is None:
                                        raise ValueError(f"TableArtifact for '{name}' missing 'inline' or 'data' field")

                                    # Handle base64 string (from JSON-RPC serialization)
                                    if isinstance(data, str):
                                        arrow_bytes = base64.b64decode(data)
                                    else:
                                        arrow_bytes = data
                                else:
                                    raise TypeError(f"Unexpected artifact type for '{name}': {type(table_artifact)}")

                                # Convert to DataFrame
                                df = pl.read_ipc(io.BytesIO(arrow_bytes))
                            except pl.exceptions.ComputeError as e:
                                # Polars-specific error (invalid Arrow data)
                                logger.error(f"Invalid Arrow IPC data for output '{name}': {e}")
                                raise ValueError(f"Output '{name}' contains invalid Arrow IPC data") from e
                            except Exception as e:
                                # Other extraction errors
                                logger.error(f"Failed to extract Arrow data for output '{name}': {e}")
                                raise

                            sim_output[name] = df
                        sim_outputs.append(sim_output)

                    # Call actual target evaluation
                    logger.info(f"Evaluating target with {len(sim_outputs)} simulation outputs")
                    target_eval = target_obj.evaluate(sim_outputs)

                    # Extract results from TargetEvaluation
                    result = {
                        "loss": float(target_eval.loss),
                        "diagnostics": {
                            "target_type": type(target_obj).__name__,
                            "model_output": target_obj.model_output,
                            "n_sim_returns": len(sim_returns),
                            "target_name": target_eval.name if hasattr(target_eval, 'name') else None,
                            "weight": target_eval.weight if hasattr(target_eval, 'weight') else None,
                        },
                        "n_replicates": len(sim_returns),
                        "outputs": {}
                    }

                    logger.info(f"Target evaluation complete: loss={result['loss']}")
                    return result

                else:
                    # Old-style evaluator that takes (sim_returns, target_data)
                    result = evaluator(sim_returns, target_data)

                    logger.info(f"Old-style evaluator returned: {result}")

                    # Validate result structure
                    if not isinstance(result, dict):
                        raise ValueError(f"Target evaluator must return dict, got {type(result)}")
                    if 'loss' not in result:
                        raise ValueError("Target evaluator must return 'loss' in result dict")

                    # Ensure loss is finite
                    loss = float(result['loss'])
                    if not math.isfinite(loss):
                        raise ValueError(f"Loss must be finite, got {loss}")

                    # Package result for JSON-RPC transport
                    return {
                        "loss": loss,
                        "diagnostics": result.get('diagnostics', {}),
                        "n_replicates": len(sim_returns),
                        "outputs": {}
                    }
            
        except Exception as e:
            logger.exception("Aggregation failed")
            # Match the error format from execute() method - must be base64 encoded
            error_info = {
                "error": str(e),
                "type": type(e).__name__,
                "target_entrypoint": target_entrypoint,
                "traceback": traceback.format_exc()  # Add full traceback for debugging
            }
            # Import json locally to avoid Python 3.13 scope issue
            import json as json_module
            err_json = json_module.dumps(error_info).encode("utf-8")
            return {"error": base64.b64encode(err_json).decode("ascii")}

# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone subprocess runner")
    parser.add_argument("--bundle-path", required=True, help="Path to bundle")
    parser.add_argument("--venv-path", required=True, help="Path to venv")
    parser.add_argument("--bundle-digest", required=True, help="Bundle digest")
    args = parser.parse_args()

    try:
        runner = SubprocessRunner(
            bundle_path=Path(args.bundle_path),
            venv_path=Path(args.venv_path),
            bundle_digest=args.bundle_digest,
        )

        rpc = JSONRPCProtocol()
        logger.info("JSON-RPC server started")

        while True:
            req_id = None
            try:
                msg = rpc.read_message()
                method = msg.get("method")
                params = msg.get("params", {}) or {}
                req_id = msg.get("id")

                if method == "ready":
                    rpc.send_response(req_id, runner.ready())
                elif method == "execute":
                    if not isinstance(params, dict):
                        raise JSONRPCError(-32602, "Invalid params (expected object)")
                    rpc.send_response(req_id, runner.execute(**params))
                elif method == "aggregate":
                    if not isinstance(params, dict):
                        raise JSONRPCError(-32602, "Invalid params (expected object)")
                    rpc.send_response(req_id, runner.aggregate(**params))
                elif method == "shutdown":
                    rpc.send_response(req_id, {"ok": True})
                    logger.info("Shutdown requested")
                    break
                else:
                    rpc.send_error(req_id, -32601, f"Method not found: {method}")

            except EOFError:
                logger.info("stdin closed; exiting")
                break
            except JSONRPCError as e:
                rpc.send_error(req_id, e.code, e.message, e.data)
            except Exception as e:
                logger.exception("Unhandled error in server loop")
                rpc.send_error(req_id, -32603, "Internal error", str(e))

    except Exception:
        logger.exception("Failed to initialize runner")
        sys.exit(1)

if __name__ == "__main__":
    main()
