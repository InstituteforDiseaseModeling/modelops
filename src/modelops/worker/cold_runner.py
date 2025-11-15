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
import base64
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
    from modelops_contracts import SimReturn, TableArtifact

    pid = os.getpid()

    # Parse task data (simple JSON dict, not whole SimTask object)
    task_data = json.loads(task_json)
    entrypoint = task_data["entrypoint"]
    params = task_data["params"]
    seed = task_data["seed"]

    logger.info(f"Child process PID {pid}: Starting simulation seed{seed}")
    logger.info(f"Child PID {pid}: Parameters: {params}")

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
            entrypoint,
            params,
            seed,
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

        # Create task ID (simplified - just use seed + outputs)
        tid_components = f"cold-sim-{seed}-{','.join(sorted(outputs.keys()))}"
        tid = hashlib.blake2b(tid_components.encode(), digest_size=32).hexdigest()

        sim_return = SimReturn(task_id=tid, outputs=outputs)

        logger.info(f"Child PID {pid}: Simulation complete, exiting")

        # Return JSON dict to stdout (parent reconstructs SimReturn)
        return json.dumps({
            "task_id": sim_return.task_id,
            "outputs": {
                name: {
                    "size": art.size,
                    "checksum": art.checksum,
                    "inline": base64.b64encode(art.inline).decode('ascii') if art.inline is not None else None,
                }
                for name, art in sim_return.outputs.items()
            }
        })

    except Exception as e:
        logger.exception(f"Child PID {pid}: Simulation failed")

        # Create error return
        tid_components = f"cold-sim-{seed}-error"
        tid = hashlib.blake2b(tid_components.encode(), digest_size=32).hexdigest()

        error_data = json.dumps(
            {
                "error": str(e),
                "type": type(e).__name__,
                "params": params,
                "seed": seed,
                "pid": pid,
            }
        ).encode()

        checksum = hashlib.blake2b(error_data, digest_size=32).hexdigest()

        # Return JSON dict
        return json.dumps({
            "task_id": tid,
            "outputs": {
                "error": {
                    "size": len(error_data),
                    "checksum": checksum,
                    "inline": base64.b64encode(error_data).decode('ascii'),
                }
            }
        })


def run_aggregation_task(bundle_path: Path, task_json: str) -> str:
    """Run a single aggregation task.

    Args:
        bundle_path: Path to unpacked bundle
        task_json: JSON-serialized AggregationTask

    Returns:
        JSON-serialized AggregationReturn
    """
    from modelops_contracts import SimReturn, TableArtifact
    from modelops_contracts.simulation import AggregationReturn

    pid = os.getpid()

    # Parse aggregation data (simple JSON dict)
    agg_data = json.loads(task_json)
    target_entrypoint = agg_data["target_entrypoint"]
    serialized_returns = agg_data["sim_returns"]
    target_data = agg_data.get("target_data")

    # Reconstruct SimReturn objects from serialized dicts
    sim_returns = []
    for sr_dict in serialized_returns:
        outputs = {}
        for name, art_dict in sr_dict["outputs"].items():
            outputs[name] = TableArtifact(
                size=art_dict["size"],
                checksum=art_dict["checksum"],
                inline=base64.b64decode(art_dict["inline"]) if art_dict.get("inline") is not None else None,
            )
        sim_returns.append(SimReturn(task_id=sr_dict["task_id"], outputs=outputs))

    logger.info(f"Child process PID {pid}: Starting aggregation")
    logger.info(f"Child PID {pid}: Target: {target_entrypoint}")
    logger.info(f"Child PID {pid}: Num sim returns: {len(sim_returns)}")

    # Add bundle to sys.path
    sys.path.insert(0, str(bundle_path))

    try:
        # Parse target entrypoint
        if ":" not in target_entrypoint:
            raise ValueError(f"Invalid target entrypoint format: {target_entrypoint}")

        module_path, target_name = target_entrypoint.rsplit(":", 1)

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

        # Check if this is a Calabaria-style target (decorated with @calibration_target)
        import inspect
        sig = inspect.signature(target_callable)

        if len(sig.parameters) == 0 or (
            len(sig.parameters) == 1 and "data_paths" in sig.parameters
        ):
            # Calabaria target - call with no args, returns Target object
            logger.info(f"Child PID {pid}: Detected Calabaria-style target")
            target_obj = target_callable()  # Decorator handles data_paths

            # Convert SimReturns to Calabaria SimOutputs (DataFrames)
            import polars as pl
            import io

            sim_outputs = []
            for sim_return in sim_returns:
                sim_output = {}
                for name, artifact in sim_return.outputs.items():
                    # Skip metadata (JSON, not Arrow)
                    if name == "metadata":
                        continue

                    # Decode Arrow bytes
                    if artifact.inline:
                        try:
                            df = pl.read_ipc(io.BytesIO(artifact.inline))
                            sim_output[name] = df
                        except Exception as e:
                            logger.warning(f"Failed to read Arrow data for {name}: {e}")

                sim_outputs.append(sim_output)

            # Evaluate the Target with sim_outputs
            logger.info(f"Child PID {pid}: Evaluating Calabaria target")
            target_eval = target_obj.evaluate(sim_outputs)

            # Build AggregationReturn from Calabaria TargetEvaluation
            agg_id_input = f"{target_entrypoint}:{','.join(sorted([sr.task_id for sr in sim_returns]))}"
            agg_id = hashlib.blake2b(agg_id_input.encode(), digest_size=32).hexdigest()[:16]

            agg_return = AggregationReturn(
                aggregation_id=agg_id,
                loss=float(target_eval.loss),
                diagnostics={
                    "target_type": type(target_obj).__name__,
                    "model_output": target_obj.model_output,
                    "target_name": target_eval.name if hasattr(target_eval, "name") else None,
                    "weight": target_eval.weight if hasattr(target_eval, "weight") else None,
                },
                outputs={},
                n_replicates=len(sim_returns),
            )
        else:
            # Old-style target - takes sim_returns directly
            logger.info(f"Child PID {pid}: Old-style target function")
            agg_return = target_callable(sim_returns)

            if not isinstance(agg_return, AggregationReturn):
                raise TypeError(
                    f"Target function returned {type(agg_return).__name__}, "
                    f"expected AggregationReturn"
                )

        logger.info(f"Child PID {pid}: Aggregation complete, loss={agg_return.loss}")

        # Return JSON dict (parent will reconstruct AggregationReturn)
        return json.dumps({
            "aggregation_id": agg_return.aggregation_id,
            "loss": agg_return.loss,
            "diagnostics": agg_return.diagnostics,
            "outputs": {},  # TODO: serialize outputs if needed
            "n_replicates": agg_return.n_replicates,
        })

    except Exception as e:
        logger.exception(f"Child PID {pid}: Aggregation failed")
        # Let the exception propagate - parent will see non-zero exit code
        raise


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
