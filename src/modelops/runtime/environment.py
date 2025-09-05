"""Runtime environment management for isolated bundle execution."""

import os
import json
import subprocess
import pathlib
import sys
import shutil
import tempfile
from typing import Optional

try:
    from modelops_bundle import BundleClient  # noqa: optional
except Exception:
    BundleClient = None  # fallback to CLI or raise a crisp error

BUNDLES = pathlib.Path(os.getenv("MODEL_OPS_BUNDLE_CACHE_DIR", "/var/cache/modelops/bundles"))
VENVS = pathlib.Path(os.getenv("MODEL_OPS_VENV_CACHE_DIR", "/var/cache/modelops/venv"))
UV_BIN = os.getenv("UV_BIN", "uv")  # TODO/PLACEHOLDER: pin/ship uv binary


def ensure_bundle(digest: str) -> pathlib.Path:
    """Ensure a bundle is available locally at the expected cache location.
    
    Args:
        digest: Bundle digest in format "sha256:abcdef..."
        
    Returns:
        Path to the bundle directory containing pyproject.toml, uv.lock, etc.
    """
    # TODO: This is a thin wrapper around modelops_bundle.BundleClient.ensure_local
    # For now, placeholder implementation
    if BundleClient is None:
        raise RuntimeError("modelops_bundle is not installed. Cannot fetch bundles.")
    
    # BundleClient.ensure_local will download and extract to BUNDLES/<digest>/
    bundle_dir = BUNDLES / digest.replace(":", "_")
    
    # TODO: Call BundleClient.ensure_local(digest, target_dir=bundle_dir) when available
    # For now, assume the bundle exists or create a placeholder
    if not bundle_dir.exists():
        bundle_dir.mkdir(parents=True, exist_ok=True)
        # TODO: Actually fetch the bundle
        raise NotImplementedError(f"Bundle fetching not yet implemented for {digest}")
    
    return bundle_dir


def ensure_venv(digest: str, bundle_dir: pathlib.Path) -> pathlib.Path:
    """Create or reuse a virtual environment for a bundle.
    
    Args:
        digest: Bundle digest for cache keying
        bundle_dir: Path to the bundle containing pyproject.toml and uv.lock
        
    Returns:
        Path to the virtual environment root
    """
    venv = VENVS / digest.replace(":", "_")
    if (venv / "bin" / "python").exists():
        return venv
    
    venv.parent.mkdir(parents=True, exist_ok=True)
    
    # Create venv
    subprocess.run([UV_BIN, "venv", str(venv)], check=True)
    
    # Sync deps (prefer offline wheelhouse if present)
    wheelhouse = bundle_dir / "wheelhouse"
    lock = bundle_dir / "uv.lock"
    pyproject = bundle_dir / "pyproject.toml"
    
    cmd = [UV_BIN, "sync", "--frozen", "--python", str(venv / "bin" / "python")]
    if wheelhouse.exists():
        cmd += ["--find-links", str(wheelhouse), "--no-index"]
    
    # TODO/PLACEHOLDER: pass pyproject/lock location via CWD or flags
    subprocess.run(cmd, cwd=bundle_dir, check=True)
    
    return venv


def run_in_env(
    venv: pathlib.Path, 
    entrypoint: str, 
    params: dict, 
    seed: int, 
    bundle_dir: pathlib.Path
) -> dict[str, bytes]:
    """Spawn user interpreter; communicate via stdin/stdout JSON + base64 IPC.
    
    Args:
        venv: Path to the virtual environment
        entrypoint: Python module entrypoint (e.g., "mymodule:main")
        params: Parameters to pass to the entrypoint
        seed: Random seed for reproducibility
        bundle_dir: Path to the bundle directory
        
    Returns:
        Dictionary mapping output names to byte contents
    """
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["MODELOPS_BUNDLE_PATH"] = str(bundle_dir)
    
    # Child runner is a tiny shim we ship in the bundle or in modelops
    child = subprocess.run(
        [str(venv / "bin" / "python"), "-m", "modelops_user_runner", entrypoint],
        input=json.dumps({"params": params, "seed": seed}).encode(),
        env=env,
        stdout=subprocess.PIPE, 
        stderr=subprocess.PIPE, 
        check=True  # TODO/PLACEHOLDER: timeouts
    )
    
    # STDOUT should be a JSON mapping of {name: base64_bytes}
    out = json.loads(child.stdout.decode())
    
    # TODO/PLACEHOLDER: validate size caps & types; convert base64→bytes
    # For now, try hex first, fallback to base64
    result = {}
    for k, v in out.items():
        try:
            # Try hex decoding first
            result[k] = bytes.fromhex(v)
        except ValueError:
            # Fallback to base64 decoding
            import base64
            result[k] = base64.b64decode(v)
    return result