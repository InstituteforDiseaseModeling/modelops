"""Single-task subprocess runner for cold execution.

This module is invoked as a fresh Python subprocess for each task.
It runs EXACTLY ONE task and exits immediately - no state persists.

This ensures complete isolation:
- Fresh Python interpreter
- Fresh module imports
- Fresh C++ extension loading (.so files)
- No cached globals or statics

Usage:
    python -m modelops.worker.cold_runner --bundle-path /path/to/bundle < task.json
    python -m modelops.worker.cold_runner --bundle-path /path/to/bundle --aggregation < agg_task.json
"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Configure logging to stderr (stdout is for result JSON)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - PID %(process)d - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


def ensure_dependencies_installed(bundle_path: Path) -> None:
    """Ensure bundle dependencies are installed in current venv.

    Uses file locking to prevent concurrent installations. This is
    similar to subprocess_runner.py but simplified for cold executor.

    Args:
        bundle_path: Path to unpacked bundle
    """
    venv_path = Path(sys.prefix)  # Current venv path
    deps_marker = venv_path / ".deps_installed"

    # Check if dependencies are already installed
    if deps_marker.exists():
        # Verify installation by trying to discover wire function
        try:
            from importlib.metadata import entry_points
            eps = list(entry_points(group="modelops.wire"))
            if eps:
                logger.info(f"Dependencies already installed and verified")
                return
        except Exception:
            # Discovery failed, need to reinstall
            logger.warning("Marker exists but wire discovery failed, will reinstall")
            deps_marker.unlink()

    # Install dependencies with file locking
    lock_file = venv_path / ".install.lock"
    lock_file.touch(exist_ok=True)

    with open(lock_file, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Double-check after acquiring lock
            if deps_marker.exists():
                logger.info("Dependencies installed by another process")
                return

            logger.info(f"Installing dependencies for bundle: {bundle_path}")
            _install_bundle_dependencies(bundle_path)

            # Write marker on success
            deps_marker.write_text("installed")
            logger.info("Dependencies installed successfully")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _install_bundle_dependencies(bundle_path: Path) -> None:
    """Install bundle dependencies using uv or pip.

    Args:
        bundle_path: Path to unpacked bundle
    """
    pyproject = bundle_path / "pyproject.toml"
    requirements = bundle_path / "requirements.txt"

    # Prefer uv if available (faster)
    uv = shutil.which("uv")

    if pyproject.exists():
        logger.info(f"Installing from pyproject.toml with {'uv' if uv else 'pip'}")
        if uv:
            result = subprocess.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--index-url",
                    "https://pypi.org/simple",
                    "--python",
                    sys.executable,
                    str(bundle_path),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(f"uv install failed: {result.stderr}")
                raise RuntimeError(f"Failed to install dependencies: {result.stderr}")
        else:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--isolated",
                    "--disable-pip-version-check",
                    str(bundle_path),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(f"pip install failed: {result.stderr}")
                raise RuntimeError(f"Failed to install dependencies: {result.stderr}")

    elif requirements.exists():
        logger.info(f"Installing from requirements.txt with {'uv' if uv else 'pip'}")
        if uv:
            result = subprocess.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--index-url",
                    "https://pypi.org/simple",
                    "--python",
                    sys.executable,
                    "-r",
                    str(requirements),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(f"uv install failed: {result.stderr}")
                raise RuntimeError(f"Failed to install dependencies: {result.stderr}")
        else:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--isolated",
                    "--disable-pip-version-check",
                    "-r",
                    str(requirements),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(f"pip install failed: {result.stderr}")
                raise RuntimeError(f"Failed to install dependencies: {result.stderr}")

    else:
        logger.warning("No pyproject.toml or requirements.txt found")

    # Ensure bundle itself is on sys.path
    if str(bundle_path) not in sys.path:
        sys.path.insert(0, str(bundle_path))


def run_simulation_task(bundle_path: Path, task_json: str) -> str:
    """Run a single simulation task.

    Args:
        bundle_path: Path to unpacked bundle
        task_json: JSON-serialized SimTask

    Returns:
        JSON-serialized SimReturn
    """
    from modelops_contracts import SimReturn, SimTask, TableArtifact

    pid = os.getpid()

    # Parse task
    task = SimTask.model_validate_json(task_json)
    param_id_short = task.params.param_id[:8]

    logger.info(f"Child process PID {pid}: Starting simulation {param_id_short}-seed{task.seed}")
    logger.info(f"Child PID {pid}: Parameters: {dict(task.params.params)}")

    # Add bundle to sys.path (ONLY the bundle, no other paths)
    sys.path.insert(0, str(bundle_path))

    try:
        # Discover wire function via entry points
        from importlib.metadata import entry_points

        eps = list(entry_points(group="modelops.wire"))

        if not eps:
            raise RuntimeError(
                f"No modelops.wire entry point found in bundle at {bundle_path}. "
                "Bundle should register: [project.entry-points.'modelops.wire'] "
                "execute = 'module.wire:wire_function'"
            )

        if len(eps) > 1:
            names = [ep.name for ep in eps]
            raise RuntimeError(f"Multiple modelops.wire entry points found: {names}")

        # Load wire function
        ep = eps[0]
        logger.info(f"Child PID {pid}: Loading wire function from {ep.name} = {ep.value}")
        wire_fn = ep.load()

        # Execute simulation (THIS PROCESS RUNS ONE TASK ONLY!)
        logger.info(f"Child PID {pid}: Executing wire function")
        result_bytes = wire_fn(
            str(task.entrypoint) if task.entrypoint else "main",
            dict(task.params.params),
            task.seed,
        )

        # Convert result to SimReturn
        outputs = {}
        for name, data in result_bytes.items():
            if not isinstance(data, bytes):
                logger.warning(f"Wire function returned non-bytes for {name}, converting")
                if isinstance(data, str):
                    data = data.encode()
                else:
                    data = json.dumps(data).encode()

            checksum = hashlib.blake2b(data, digest_size=32).hexdigest()
            outputs[name] = TableArtifact(
                size=len(data),
                inline=data,  # Cold executor always inlines
                checksum=checksum,
            )

        # Create task ID
        tid_components = f"{task.params.param_id[:16]}-{task.seed}-{','.join(sorted(outputs.keys()))}"
        tid = hashlib.blake2b(tid_components.encode(), digest_size=32).hexdigest()

        sim_return = SimReturn(task_id=tid, outputs=outputs)

        logger.info(f"Child PID {pid}: Simulation complete, exiting")

        # Return JSON to stdout (parent reads this)
        return sim_return.model_dump_json()

    except Exception as e:
        logger.exception(f"Child PID {pid}: Simulation failed")

        # Create error return
        tid_components = f"{task.params.param_id[:16]}-{task.seed}-error"
        tid = hashlib.blake2b(tid_components.encode(), digest_size=32).hexdigest()

        error_data = json.dumps(
            {
                "error": str(e),
                "type": type(e).__name__,
                "params": dict(task.params.params),
                "seed": task.seed,
                "pid": pid,
            }
        ).encode()

        checksum = hashlib.blake2b(error_data, digest_size=32).hexdigest()

        sim_return = SimReturn(
            task_id=tid,
            outputs={
                "error": TableArtifact(
                    size=len(error_data),
                    inline=error_data,
                    checksum=checksum,
                )
            },
        )

        return sim_return.model_dump_json()


def run_aggregation_task(bundle_path: Path, task_json: str) -> str:
    """Run a single aggregation task.

    Args:
        bundle_path: Path to unpacked bundle
        task_json: JSON-serialized AggregationTask

    Returns:
        JSON-serialized AggregationReturn
    """
    from modelops_contracts.simulation import AggregationReturn, AggregationTask

    pid = os.getpid()

    # Parse aggregation task
    task = AggregationTask.model_validate_json(task_json)

    logger.info(f"Child process PID {pid}: Starting aggregation")
    logger.info(f"Child PID {pid}: Target: {task.target_entrypoint}")
    logger.info(f"Child PID {pid}: Num sim returns: {len(task.sim_returns)}")

    # Add bundle to sys.path
    sys.path.insert(0, str(bundle_path))

    try:
        # Parse target entrypoint
        if ":" not in task.target_entrypoint:
            raise ValueError(f"Invalid target entrypoint format: {task.target_entrypoint}")

        module_path, target_name = task.target_entrypoint.rsplit(":", 1)

        logger.info(f"Child PID {pid}: Importing {module_path}")

        # Import target module
        import importlib

        target_module = importlib.import_module(module_path)

        # Get target function/class
        if not hasattr(target_module, target_name):
            raise AttributeError(
                f"Module '{module_path}' has no attribute '{target_name}'. "
                f"Available: {dir(target_module)}"
            )

        target_callable = getattr(target_module, target_name)

        logger.info(f"Child PID {pid}: Calling target function")

        # Call target function with sim returns
        # Target function signature: (sim_returns: list[SimReturn]) -> AggregationReturn
        agg_return = target_callable(task.sim_returns)

        if not isinstance(agg_return, AggregationReturn):
            raise TypeError(
                f"Target function returned {type(agg_return).__name__}, "
                f"expected AggregationReturn"
            )

        logger.info(f"Child PID {pid}: Aggregation complete, loss={agg_return.loss}")

        return agg_return.model_dump_json()

    except Exception as e:
        logger.exception(f"Child PID {pid}: Aggregation failed")

        # Return error aggregation
        agg_return = AggregationReturn(
            aggregation_id=task.aggregation_id(),
            loss=None,
            n_replicates=len(task.sim_returns),
            diagnostics={
                "error": str(e),
                "type": type(e).__name__,
                "pid": pid,
            },
            outputs={},
        )

        return agg_return.model_dump_json()


def main():
    """Main entrypoint for cold subprocess runner."""
    parser = argparse.ArgumentParser(description="Cold subprocess runner for ModelOps")
    parser.add_argument(
        "--bundle-path",
        required=True,
        type=Path,
        help="Path to unpacked bundle",
    )
    parser.add_argument(
        "--aggregation",
        action="store_true",
        help="Run aggregation task instead of simulation",
    )

    args = parser.parse_args()

    pid = os.getpid()
    logger.info(f"Cold runner started: PID {pid}, bundle={args.bundle_path}")

    # Install dependencies before running any tasks
    try:
        ensure_dependencies_installed(args.bundle_path)
    except Exception as e:
        logger.exception(f"Failed to install dependencies: {e}")
        sys.exit(1)

    # Read task JSON from stdin
    task_json = sys.stdin.read()

    if not task_json:
        logger.error("No task JSON provided on stdin")
        sys.exit(1)

    try:
        if args.aggregation:
            result_json = run_aggregation_task(args.bundle_path, task_json)
        else:
            result_json = run_simulation_task(args.bundle_path, task_json)

        # Write result to stdout (parent reads this)
        print(result_json)

        # Exit cleanly (process dies, all state gone!)
        logger.info(f"Cold runner PID {pid} exiting")
        sys.exit(0)

    except Exception as e:
        logger.exception(f"Fatal error in cold runner PID {pid}")

        # Write error JSON to stdout
        error_result = {
            "_fatal_error": {
                "type": type(e).__name__,
                "message": str(e),
                "pid": pid,
            }
        }
        print(json.dumps(error_result))
        sys.exit(1)


if __name__ == "__main__":
    main()
