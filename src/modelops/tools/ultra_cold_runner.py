#!/usr/bin/env python3
"""Ultra-simple cold runner - fresh process per task.

CRITICAL: This script is STANDALONE with NO ModelOps dependencies!

Runs exactly one task per invocation:
- Creates fresh venv (or reuses via REUSE=1)
- Installs bundle dependencies
- Executes simulation or aggregation
- Prints result JSON to stdout
- Logs to stderr only
- Exits (process dies, state gone)

Usage:
    echo '{"entrypoint":"main","params":{},"seed":1}' | python ultra_cold_runner.py --bundle-path /path
    echo '{"target_entrypoint":"targets.foo:bar","sim_returns":[...]}' | python ultra_cold_runner.py --bundle-path /path --aggregation

Environment:
    REUSE=1              Reuse venv based on bundle digest (default: fresh each time)
    PRESERVE_TMP=1       Don't delete temp venv after task
    VENVS_DIR=/path      Where to store venvs (default: /tmp/modelops_cold_venvs)
"""

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Logging to stderr only (stdout reserved for result JSON)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - COLD - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


def _configure_git_auth() -> None:
    """Configure git to use GitHub token if available."""
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        logger.info("Configuring git authentication for private repos")
        # Configure git to use the token for GitHub
        # This sets it globally for this process and subprocesses
        subprocess.run(
            [
                "git",
                "config",
                "--global",
                f"url.https://x-access-token:{github_token}@github.com/.insteadOf",
                "https://github.com/",
            ],
            capture_output=True,
            text=True,
            check=False,  # Don't fail if git config fails
        )
    else:
        logger.warning(
            "GITHUB_TOKEN not found in environment - private repos will fail to clone!"
        )


def compute_bundle_digest(bundle_path: Path) -> str:
    """Compute stable digest for bundle based on pyproject.toml or requirements.txt."""
    pyproject = bundle_path / "pyproject.toml"
    requirements = bundle_path / "requirements.txt"

    if pyproject.exists():
        content = pyproject.read_bytes()
    elif requirements.exists():
        content = requirements.read_bytes()
    else:
        # No deps file, use bundle path name
        content = str(bundle_path).encode()

    # Include Python version for isolation
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}".encode()
    return hashlib.blake2b(content + py_version, digest_size=32).hexdigest()


def create_or_get_venv(bundle_path: Path, reuse: bool) -> Path:
    """Create fresh venv or reuse existing one."""
    if reuse:
        venvs_dir = Path(os.environ.get("VENVS_DIR", "/tmp/modelops_cold_venvs"))
        venvs_dir.mkdir(parents=True, exist_ok=True)

        digest = compute_bundle_digest(bundle_path)
        venv_path = venvs_dir / f"cold-{digest}"

        if venv_path.exists():
            logger.info(f"Reusing venv: {venv_path}")
            return venv_path
        else:
            logger.info(f"Creating reusable venv: {venv_path}")
    else:
        # Fresh ephemeral venv
        venv_path = Path(tempfile.mkdtemp(prefix="cold_venv_"))
        logger.info(f"Creating ephemeral venv: {venv_path}")

    # Create venv with uv (fast) or fallback to venv module
    if shutil.which("uv"):
        subprocess.run(
            ["uv", "venv", str(venv_path)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_path)],
            check=True,
            capture_output=True,
            timeout=120,
        )

    # CRITICAL: Bootstrap pip if missing (uv venv doesn't include pip by default)
    # This ensures pip fallback will work when uv fails
    if platform.system() == "Windows":
        python_exe = venv_path / "Scripts" / "python.exe"
    else:
        python_exe = venv_path / "bin" / "python"

    if python_exe.exists():
        logger.info("Bootstrapping pip in venv")
        ensurepip_result = subprocess.run(
            [str(python_exe), "-m", "ensurepip", "--upgrade"],
            capture_output=True,
            timeout=60,
        )
        if ensurepip_result.returncode != 0:
            logger.warning("ensurepip failed during venv creation, pip may not be available")
            logger.debug(f"ensurepip stderr: {ensurepip_result.stderr.decode()}")
    else:
        logger.warning(f"Python executable not found at {python_exe}, skipping pip bootstrap")

    return venv_path


def install_bundle_deps(venv_path: Path, bundle_path: Path) -> None:
    """Install bundle dependencies into venv.

    Battle-tested version ported from subprocess_runner.py with:
    - Git authentication for private repos
    - ensurepip bootstrap before pip fallback
    - Multiple retry strategies
    - Homebrew Python detection
    """
    # Determine python executable path (platform-specific)
    if platform.system() == "Windows":
        python_exe = venv_path / "Scripts" / "python.exe"
    else:
        python_exe = venv_path / "bin" / "python"

    if not python_exe.exists():
        raise RuntimeError(f"Python executable not found: {python_exe}")

    pyproject = bundle_path / "pyproject.toml"
    requirements = bundle_path / "requirements.txt"

    logger.info("Installing bundle dependencies...")

    # Check for problematic dependency configurations
    suspects = []
    for fname in ("pyproject.toml", "uv.lock", "requirements.txt", ".constraints.txt"):
        p = bundle_path / fname
        if p.exists():
            t = p.read_text()
            # Check for malformed numpy git URLs
            if "github.com/numpy/" in t and "github.com/numpy/numpy" not in t:
                suspects.append(f"{fname}: malformed numpy git URL detected")
            # Check for problematic uv sources
            if "[tool.uv.sources]" in t and "numpy" in t:
                suspects.append(f"{fname}: uv sources override for numpy detected")
            # Check for local file dependencies that won't work in container
            if "file:///" in t and "/Users/" in t:
                suspects.append(f"{fname}: local file:// dependency won't work in container")

    if suspects:
        logger.warning(
            "Bundle has problematic dependency configurations:\n  - %s",
            "\n  - ".join(suspects),
        )
        # Clean up problematic files that might interfere
        for fname in (".constraints.txt", "uv.lock"):
            p = bundle_path / fname
            if p.exists():
                logger.warning(f"Removing {fname} to avoid dependency resolution issues")
                p.unlink()

    # Configure git auth before installing (for private repos)
    _configure_git_auth()

    # Prefer uv if present (fast), else pip
    uv = shutil.which("uv")

    if pyproject.exists():
        logger.info("Found pyproject.toml, installing with %s", "uv" if uv else "pip")
        if uv:
            # Try uv first (allow git dependencies by NOT restricting to PyPI-only)
            result = subprocess.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(python_exe),
                    str(bundle_path),
                ],
                capture_output=True,
                text=True,
                cwd=str(bundle_path),
            )

            if result.returncode != 0:
                logger.warning("uv failed; falling back to pip (PyPI only)")
                logger.debug(f"uv stderr: {result.stderr}")

                # Check if we're in an externally managed environment
                try:
                    pip_check = subprocess.run(
                        [str(python_exe), "-m", "pip", "--version"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if "externally-managed" in pip_check.stderr.lower():
                        logger.error("Python environment is externally managed, cannot use pip")
                        raise RuntimeError(
                            "Cannot install packages: Python is externally managed. Ensure uv is available."
                        )
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass

                # Try to ensure pip is available first
                ensurepip_result = subprocess.run(
                    [str(python_exe), "-m", "ensurepip", "--upgrade"],
                    capture_output=True,
                    text=True,
                )
                if ensurepip_result.returncode != 0:
                    logger.warning("ensurepip failed, pip may not be available")
                    logger.debug(f"ensurepip stderr: {ensurepip_result.stderr}")

                # Try pip install
                pip_args = [
                    str(python_exe),
                    "-m",
                    "pip",
                    "install",
                    "--isolated",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-input",
                ]

                # Add break-system-packages flag if we detect it might be needed
                if "homebrew" in str(python_exe).lower() or "/opt/homebrew" in str(python_exe):
                    logger.warning(
                        "Detected Homebrew Python, adding --break-system-packages flag"
                    )
                    pip_args.append("--break-system-packages")

                pip_args.append(str(bundle_path))

                pip_result = subprocess.run(
                    pip_args,
                    capture_output=True,
                    text=True,
                    cwd=str(bundle_path),
                )

                if pip_result.returncode != 0:
                    logger.error(f"pip install also failed: {pip_result.stderr}")
                    raise RuntimeError(
                        f"Failed to install from pyproject.toml with both uv and pip:\n"
                        f"uv error: {result.stderr}\n"
                        f"pip error: {pip_result.stderr}"
                    )
        else:
            # No uv, use pip directly
            subprocess.run(
                [
                    str(python_exe),
                    "-m",
                    "pip",
                    "install",
                    "--isolated",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-input",
                    str(bundle_path),
                ],
                check=True,
                capture_output=True,
                cwd=str(bundle_path),
            )

    elif requirements.exists():
        logger.info("Found requirements.txt, installing with %s", "uv" if uv else "pip")
        if uv:
            # Try uv first (allow git dependencies by NOT restricting to PyPI-only)
            result = subprocess.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(python_exe),
                    "-r",
                    str(requirements),
                ],
                capture_output=True,
                text=True,
                cwd=str(bundle_path),
            )

            if result.returncode != 0:
                logger.warning("uv failed; falling back to pip (PyPI only)")
                logger.debug(f"uv stderr: {result.stderr}")

                # Similar handling as pyproject.toml case
                ensurepip_result = subprocess.run(
                    [str(python_exe), "-m", "ensurepip", "--upgrade"],
                    capture_output=True,
                    text=True,
                )
                if ensurepip_result.returncode != 0:
                    logger.warning("ensurepip failed, pip may not be available")
                    logger.debug(f"ensurepip stderr: {ensurepip_result.stderr}")

                pip_args = [
                    str(python_exe),
                    "-m",
                    "pip",
                    "install",
                    "--isolated",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-input",
                ]

                if "homebrew" in str(python_exe).lower() or "/opt/homebrew" in str(python_exe):
                    logger.warning(
                        "Detected Homebrew Python, adding --break-system-packages flag"
                    )
                    pip_args.append("--break-system-packages")

                pip_args.extend(["-r", str(requirements)])

                pip_result = subprocess.run(
                    pip_args,
                    capture_output=True,
                    text=True,
                    cwd=str(bundle_path),
                )

                if pip_result.returncode != 0:
                    logger.error(f"pip install also failed: {pip_result.stderr}")
                    raise RuntimeError(
                        f"Failed to install from requirements.txt with both uv and pip:\n"
                        f"uv error: {result.stderr}\n"
                        f"pip error: {pip_result.stderr}"
                    )
        else:
            # No uv, use pip directly
            subprocess.run(
                [
                    str(python_exe),
                    "-m",
                    "pip",
                    "install",
                    "--isolated",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-input",
                    "-r",
                    str(requirements),
                ],
                check=True,
                capture_output=True,
                cwd=str(bundle_path),
            )
    else:
        logger.warning("No dependency file found (pyproject.toml or requirements.txt)")

    logger.info("Dependencies installed successfully")


def run_simulation(venv_path: Path, bundle_path: Path, task_data: dict) -> dict:
    """Run simulation task in isolated subprocess."""
    # Determine python executable
    if platform.system() == "Windows":
        python_exe = venv_path / "Scripts" / "python.exe"
    else:
        python_exe = venv_path / "bin" / "python"

    entrypoint = task_data["entrypoint"]
    params = task_data["params"]
    seed = task_data["seed"]

    logger.info(f"Running simulation: entrypoint={entrypoint}, seed={seed}")

    # Build execution script (inline to avoid another file)
    exec_script = f"""
import sys
import importlib
import base64
import hashlib
import json
import contextlib

# Add bundle to path
sys.path.insert(0, {str(bundle_path)!r})

# Invalidate caches after modifying sys.path (CRITICAL!)
importlib.invalidate_caches()

# Discover wire function
from importlib.metadata import entry_points
eps = list(entry_points(group="modelops.wire"))

if not eps:
    raise RuntimeError("No modelops.wire entry point found in bundle")
if len(eps) > 1:
    raise RuntimeError(f"Multiple wire entry points found: {{[ep.name for ep in eps]}}")

wire_fn = eps[0].load()

# Redirect stdout to stderr during execution (prevents matplotlib/user prints from corrupting JSON)
# This matches the warm worker's behavior (subprocess_runner.py:634)
with contextlib.redirect_stdout(sys.stderr):
    result_bytes = wire_fn({entrypoint!r}, {params!r}, {seed!r})

# Serialize outputs
outputs = {{}}
for name, data in result_bytes.items():
    if not isinstance(data, bytes):
        if isinstance(data, str):
            data = data.encode()
        else:
            data = json.dumps(data).encode()

    checksum = hashlib.blake2b(data, digest_size=32).hexdigest()
    outputs[name] = {{
        "size": len(data),
        "checksum": checksum,
        "inline": base64.b64encode(data).decode('ascii'),
    }}

# Generate task_id
tid_input = f"{seed}-{{','.join(sorted(outputs.keys()))}}".encode()
task_id = hashlib.blake2b(tid_input, digest_size=32).hexdigest()

# Print result JSON to stdout
print(json.dumps({{"task_id": task_id, "outputs": outputs}}))
"""

    # Run with isolated mode (-I) and unbuffered (-u)
    # NO TIMEOUT - simulations can take hours (Dask handles task-level timeouts)
    result = subprocess.run(
        [str(python_exe), "-I", "-u", "-c", exec_script],
        capture_output=True,
        text=True,
        cwd=str(bundle_path),
    )

    if result.returncode != 0:
        logger.error(f"Simulation failed: {result.stderr}")
        return {
            "_fatal_error": {
                "code": result.returncode,
                "stderr": result.stderr[:1000],
                "stdout": result.stdout[:500],
            }
        }

    # Forward any logs from child
    if result.stderr:
        sys.stderr.write(result.stderr)

    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse result: {e}")
        logger.error(f"stdout: {result.stdout[:500]}")
        logger.error(f"stderr: {result.stderr[:1000]}")
        return {
            "_fatal_error": {
                "code": 1,
                "error": "Invalid JSON output",
                "stdout": result.stdout[:500],
                "stderr": result.stderr[:1000],
            }
        }


def run_aggregation(venv_path: Path, bundle_path: Path, agg_data: dict) -> dict:
    """Run aggregation task in isolated subprocess."""
    # Determine python executable
    if platform.system() == "Windows":
        python_exe = venv_path / "Scripts" / "python.exe"
    else:
        python_exe = venv_path / "bin" / "python"

    target_entrypoint = agg_data["target_entrypoint"]
    sim_returns = agg_data["sim_returns"]

    logger.info(f"Running aggregation: target={target_entrypoint}, n_returns={len(sim_returns)}")

    # Build execution script
    exec_script = f"""
import sys
import importlib
import base64
import json
import io
import contextlib

# Add bundle to path
sys.path.insert(0, {str(bundle_path)!r})

# Invalidate caches after modifying sys.path (CRITICAL!)
importlib.invalidate_caches()

# Parse target entrypoint
target_entrypoint = {target_entrypoint!r}
if ':' not in target_entrypoint:
    raise ValueError(f"Invalid target entrypoint: {{target_entrypoint}}")

module_path, target_name = target_entrypoint.rsplit(':', 1)

# Import target module
target_module = importlib.import_module(module_path)
target_callable = getattr(target_module, target_name)

# Deserialize sim_returns (decode base64 inline data)
sim_returns_raw = {json.dumps(sim_returns)!r}
sim_returns_data = json.loads(sim_returns_raw)

sim_returns = []
for sr_dict in sim_returns_data:
    outputs = {{}}
    for name, art_dict in sr_dict["outputs"].items():
        inline_b64 = art_dict.get("inline")
        inline_bytes = base64.b64decode(inline_b64) if inline_b64 else None
        outputs[name] = {{
            "size": art_dict["size"],
            "checksum": art_dict["checksum"],
            "inline": inline_bytes,
        }}
    sim_returns.append({{"task_id": sr_dict["task_id"], "outputs": outputs}})

# Check if Calabaria-style target (decorated with @calibration_target)
import inspect
sig = inspect.signature(target_callable)

if len(sig.parameters) == 0 or (len(sig.parameters) == 1 and "data_paths" in sig.parameters):
    # Calabaria target - call with no args, returns Target object
    import polars as pl

    # Redirect stdout to stderr during target evaluation (prevents matplotlib/user prints)
    with contextlib.redirect_stdout(sys.stderr):
        target_obj = target_callable()  # Decorator handles data_paths

        # Convert SimReturns to Calabaria SimOutputs (DataFrames)
        sim_outputs = []
        for sim_return in sim_returns:
            sim_output = {{}}
            for name, artifact in sim_return["outputs"].items():
                # Skip non-Arrow outputs
                if name in ("metadata", "error"):
                    continue

                # Decode Arrow bytes
                if artifact["inline"]:
                    try:
                        df = pl.read_ipc(io.BytesIO(artifact["inline"]))
                        sim_output[name] = df
                    except Exception:
                        pass

            sim_outputs.append(sim_output)

        # Evaluate the Target
        target_eval = target_obj.evaluate(sim_outputs)

    # Extract loss from result - handle both TargetLossResult and TargetLikelihoodResult
    if hasattr(target_eval, "loss"):
        # TargetLossResult - use loss directly
        loss = float(target_eval.loss)
    elif hasattr(target_eval, "loglik_per_rep"):
        # TargetLikelihoodResult - compute loss as negative log-mean-exp
        # loss = -log_marginal = -(logsumexp(loglik_per_rep) - log(R))
        import numpy as np
        from scipy.special import logsumexp

        loglik = target_eval.loglik_per_rep
        R = len(loglik)
        log_marginal = logsumexp(loglik) - np.log(R)
        loss = -float(log_marginal)
    else:
        raise AttributeError(
            f"Target evaluation result has neither 'loss' nor 'loglik_per_rep'. "
            f"Got {{type(target_eval).__name__}} with attributes: {{dir(target_eval)}}"
        )

    # Build result
    result = {{
        "loss": loss,
        "diagnostics": {{
            "target_type": type(target_obj).__name__,
            "model_output": target_obj.model_output,
            "target_name": target_eval.name if hasattr(target_eval, "name") else None,
            "weight": target_eval.weight if hasattr(target_eval, "weight") else None,
        }},
        "n_replicates": len(sim_returns),
    }}
else:
    # Old-style target - takes sim_returns directly
    # Redirect stdout to stderr during target evaluation
    with contextlib.redirect_stdout(sys.stderr):
        result = target_callable(sim_returns)

    if not isinstance(result, dict):
        raise TypeError(f"Target must return dict, got {{type(result).__name__}}")
    if "loss" not in result:
        raise ValueError("Target must return dict with 'loss' key")

# Print result JSON to stdout
print(json.dumps(result))
"""

    # Run with isolated mode (-I) and unbuffered (-u)
    # NO TIMEOUT - simulations can take hours (Dask handles task-level timeouts)
    result = subprocess.run(
        [str(python_exe), "-I", "-u", "-c", exec_script],
        capture_output=True,
        text=True,
        cwd=str(bundle_path),
    )

    if result.returncode != 0:
        logger.error(f"Aggregation failed: {result.stderr}")
        return {
            "_fatal_error": {
                "code": result.returncode,
                "stderr": result.stderr[:1000],
                "stdout": result.stdout[:500],
            }
        }

    # Forward any logs from child
    if result.stderr:
        sys.stderr.write(result.stderr)

    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse result: {e}")
        logger.error(f"stdout: {result.stdout[:500]}")
        logger.error(f"stderr: {result.stderr[:1000]}")
        return {
            "_fatal_error": {
                "code": 1,
                "error": "Invalid JSON output",
                "stdout": result.stdout[:500],
                "stderr": result.stderr[:1000],
            }
        }


def main():
    parser = argparse.ArgumentParser(description="Ultra-simple cold runner")
    parser.add_argument("--bundle-path", required=True, type=Path, help="Path to bundle")
    parser.add_argument("--aggregation", action="store_true", help="Run aggregation instead of simulation")
    args = parser.parse_args()

    bundle_path = args.bundle_path.resolve()
    if not bundle_path.exists():
        logger.error(f"Bundle not found: {bundle_path}")
        print(json.dumps({"_fatal_error": {"code": 1, "error": "Bundle not found"}}))
        sys.exit(1)

    # Read task from stdin
    task_json = sys.stdin.read()
    if not task_json:
        logger.error("No task JSON on stdin")
        print(json.dumps({"_fatal_error": {"code": 1, "error": "No input"}}))
        sys.exit(1)

    try:
        task_data = json.loads(task_json)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid task JSON: {e}")
        print(json.dumps({"_fatal_error": {"code": 1, "error": f"Invalid JSON: {e}"}}))
        sys.exit(1)

    # Determine if we reuse venvs
    reuse = os.environ.get("REUSE", "0") in ("1", "true", "TRUE")
    preserve = os.environ.get("PRESERVE_TMP", "0") in ("1", "true", "TRUE")

    # Create/get venv
    try:
        venv_path = create_or_get_venv(bundle_path, reuse)
    except Exception as e:
        logger.exception("Failed to create venv")
        print(json.dumps({"_fatal_error": {"code": 1, "error": f"Venv creation failed: {e}"}}))
        sys.exit(1)

    # Install dependencies
    try:
        install_bundle_deps(venv_path, bundle_path)
    except Exception as e:
        logger.exception("Failed to install dependencies")
        print(json.dumps({"_fatal_error": {"code": 1, "error": f"Dependency install failed: {e}"}}))
        if not reuse and not preserve:
            shutil.rmtree(venv_path, ignore_errors=True)
        sys.exit(1)

    # Run task
    try:
        if args.aggregation:
            result = run_aggregation(venv_path, bundle_path, task_data)
        else:
            result = run_simulation(venv_path, bundle_path, task_data)

        # Print result to stdout (single JSON line)
        print(json.dumps(result))

        # Clean up ephemeral venv
        if not reuse and not preserve:
            logger.info(f"Cleaning up venv: {venv_path}")
            shutil.rmtree(venv_path, ignore_errors=True)

        # Exit with appropriate code
        sys.exit(0 if "_fatal_error" not in result else 1)

    except Exception as e:
        logger.exception("Fatal error during execution")
        print(json.dumps({"_fatal_error": {"code": 1, "error": str(e), "type": type(e).__name__}}))

        if not reuse and not preserve:
            shutil.rmtree(venv_path, ignore_errors=True)

        sys.exit(1)


if __name__ == "__main__":
    main()
