"""Cold execution environment - fresh process per task.

Provides maximum isolation by spawning a brand new subprocess for each
simulation task. The subprocess exits after completing one task, ensuring
no C++ static state or Python module globals persist across tasks.

This is the diagnostic mode for debugging state leakage issues in
pybind11 extensions or other native code. It's significantly slower than
warm executors but guarantees complete isolation.
"""

import base64
import hashlib
import json
import logging
import os
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from modelops_contracts import ErrorInfo, SimReturn, SimTask, TableArtifact
from modelops_contracts.ports import BundleRepository, ExecutionEnvironment
from modelops_contracts.simulation import AggregationReturn, AggregationTask

logger = logging.getLogger(__name__)


class ColdExecEnv(ExecutionEnvironment):
    """Cold execution environment - spawns fresh process per task.

    Each simulation runs in a completely fresh Python process that:
    - Has no warm pool or cached modules
    - Loads the bundle and executes exactly one task
    - Exits immediately after task completion
    - Never shares C++ static state with other tasks

    This provides maximum isolation at the cost of performance. Use this
    for debugging state leakage or when correctness is more important
    than speed.

    Performance characteristics:
    - ~500-1000ms overhead per task (vs ~50ms for warm)
    - No venv caching across tasks (optional)
    - Process spawn + Python interpreter startup per task
    """

    def __init__(
        self,
        bundle_repo: BundleRepository,
        venvs_dir: Path,
        storage_dir: Path,
        force_fresh_venv: bool = False,
        timeout_seconds: int = 3600,
        azure_backend: dict[str, Any] | None = None,
    ):
        """Initialize cold execution environment.

        Args:
            bundle_repo: Repository for fetching bundles
            venvs_dir: Directory for virtual environments (can cache venvs)
            storage_dir: Directory for provenance-based storage
            force_fresh_venv: If True, create fresh venv per task (slowest)
            timeout_seconds: Max execution time per task
            azure_backend: Azure backend configuration (not yet supported in cold mode)
        """
        self.bundle_repo = bundle_repo
        self.venvs_dir = venvs_dir
        self.storage_dir = storage_dir
        self.force_fresh_venv = force_fresh_venv
        self.timeout_seconds = timeout_seconds

        if azure_backend:
            logger.warning("ColdExecEnv does not yet support Azure backend (provenance only local)")

        # Create storage dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.venvs_dir.mkdir(parents=True, exist_ok=True)

    def run(self, task: SimTask) -> SimReturn:
        """Execute simulation task in fresh subprocess.

        Spawns a new Python process, runs the task, and returns results.
        The subprocess exits after one task, ensuring no state persists.

        Args:
            task: Simulation task to execute

        Returns:
            SimReturn with status and artifacts
        """
        parent_pid = os.getpid()
        param_id_short = task.params.param_id[:8]

        logger.info(
            f"Cold exec (parent PID {parent_pid}): "
            f"Running task {param_id_short}-seed{task.seed}"
        )

        try:
            # 1. Resolve bundle
            digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)

            # 2. Get or create venv (can cache venvs, but not processes!)
            venv_path = self._get_or_create_venv(digest, bundle_path)
            python_exe = venv_path / "bin" / "python"

            if not python_exe.exists():
                raise RuntimeError(f"Python executable not found: {python_exe}")

            # 3. Serialize task fields (not whole object - follow warm executor pattern)
            task_data = {
                "entrypoint": str(task.entrypoint) if task.entrypoint else "main",
                "params": dict(task.params.params),
                "seed": task.seed,
            }
            task_json = json.dumps(task_data)

            # 4. Prepare clean environment for subprocess
            env = self._prepare_subprocess_env()

            # 5. Spawn fresh subprocess (exits after one task!)
            result = subprocess.run(
                [
                    str(python_exe),
                    "-u",  # Unbuffered output
                    "-m",
                    "modelops.worker.cold_runner",
                    "--bundle-path",
                    str(bundle_path),
                ],
                input=task_json,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                check=False,
            )

            child_pid = result.returncode  # We'll log actual PID from subprocess output

            # 6. Parse result
            if result.returncode != 0:
                logger.error(
                    f"Cold subprocess failed (exit {result.returncode}): {result.stderr[:500]}"
                )
                return self._create_error_return(task, result.stderr, result.returncode)

            # 7. Parse SimReturn from stdout (JSON dict format)
            try:
                result_dict = json.loads(result.stdout)
                # Reconstruct SimReturn from dict
                outputs = {}
                for name, art_dict in result_dict["outputs"].items():
                    outputs[name] = TableArtifact(
                        size=art_dict["size"],
                        checksum=art_dict["checksum"],
                        inline=base64.b64decode(art_dict["inline"]) if art_dict["inline"] else None,
                    )
                sim_return = SimReturn(
                    task_id=result_dict["task_id"],
                    outputs=outputs,
                )
                logger.debug(
                    f"Cold exec success: {param_id_short}-seed{task.seed} "
                    f"(check stderr for child PID)"
                )
                return sim_return
            except Exception as e:
                logger.error(f"Failed to parse SimReturn from subprocess: {e}")
                logger.error(f"Subprocess stdout: {result.stdout[:500]}")
                return self._create_error_return(
                    task, f"Failed to parse result: {e}\nStdout: {result.stdout[:200]}", 1
                )

        except subprocess.TimeoutExpired as e:
            logger.error(f"Task {param_id_short}-seed{task.seed} timed out after {self.timeout_seconds}s")
            return self._create_error_return(task, f"Timeout after {self.timeout_seconds}s", 124)
        except Exception as e:
            logger.exception(f"Cold execution failed for task {param_id_short}-seed{task.seed}")
            return self._create_error_return(task, str(e), 1)

    def run_aggregation(self, task: AggregationTask) -> AggregationReturn:
        """Execute aggregation task in fresh subprocess.

        Args:
            task: AggregationTask with target and sim results

        Returns:
            AggregationReturn with loss and diagnostics
        """
        parent_pid = os.getpid()
        logger.info(f"Cold exec (parent PID {parent_pid}): Running aggregation")

        try:
            # 1. Resolve bundle
            digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)

            # 2. Get or create venv
            venv_path = self._get_or_create_venv(digest, bundle_path)
            python_exe = venv_path / "bin" / "python"

            # 3. Serialize aggregation task (follow warm executor pattern)
            # Don't serialize whole AggregationTask - break it into simple fields
            serialized_returns = self._serialize_sim_returns(task.sim_returns)
            agg_data = {
                "target_entrypoint": str(task.target_entrypoint),
                "sim_returns": serialized_returns,
                "target_data": task.target_data,
            }
            task_json = json.dumps(agg_data)

            # 4. Prepare environment
            env = self._prepare_subprocess_env()

            # 5. Spawn fresh subprocess for aggregation
            result = subprocess.run(
                [
                    str(python_exe),
                    "-u",
                    "-m",
                    "modelops.worker.cold_runner",
                    "--bundle-path",
                    str(bundle_path),
                    "--aggregation",  # Flag to indicate aggregation mode
                ],
                input=task_json,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                check=False,
            )

            if result.returncode != 0:
                logger.error(f"Cold aggregation subprocess failed: {result.stderr[:500]}")
                # Raise exception instead of returning invalid AggregationReturn
                raise RuntimeError(
                    f"Aggregation subprocess failed (exit {result.returncode}): {result.stderr[:1000]}"
                )

            # 6. Parse AggregationReturn from JSON dict
            try:
                result_dict = json.loads(result.stdout)
                agg_return = AggregationReturn(
                    aggregation_id=result_dict["aggregation_id"],
                    loss=result_dict["loss"],
                    diagnostics=result_dict["diagnostics"],
                    outputs=result_dict.get("outputs", {}),
                    n_replicates=result_dict["n_replicates"],
                )
                logger.debug("Cold aggregation success")
                return agg_return
            except Exception as e:
                logger.error(f"Failed to parse AggregationReturn: {e}")
                logger.error(f"Subprocess stdout: {result.stdout[:500]}")
                raise RuntimeError(f"Failed to parse AggregationReturn: {e}\\nStdout: {result.stdout[:200]}") from e

        except subprocess.TimeoutExpired:
            logger.error(f"Aggregation timed out after {self.timeout_seconds}s")
            raise RuntimeError(f"Aggregation timed out after {self.timeout_seconds}s")
        except Exception as e:
            logger.exception("Cold aggregation failed")
            raise

    def _serialize_sim_returns(self, sim_returns: list[SimReturn]) -> list[dict]:
        """Serialize SimReturns for subprocess communication.

        Args:
            sim_returns: List of SimReturns with inline data

        Returns:
            List of serialized SimReturn dicts
        """
        serialized_returns = []
        for sr in sim_returns:
            sr_dict = {"task_id": sr.task_id, "outputs": {}}

            for name, artifact in sr.outputs.items():
                # For cold executor, always use inline data
                if not artifact.inline:
                    raise ValueError(f"Artifact {name} missing inline data for aggregation")

                sr_dict["outputs"][name] = {
                    "size": artifact.size,
                    "checksum": artifact.checksum,
                    "inline": base64.b64encode(artifact.inline).decode("ascii"),
                }

            serialized_returns.append(sr_dict)

        return serialized_returns

    def _get_or_create_venv(self, digest: str, bundle_path: Path) -> Path:
        """Get or create venv for bundle.

        Note: We cache venvs for speed, but NEVER reuse processes!
        Each task still gets a fresh Python interpreter startup.

        Args:
            digest: Bundle digest
            bundle_path: Path to bundle

        Returns:
            Path to venv
        """
        if self.force_fresh_venv:
            # Create unique venv per task (slowest but most isolated)
            import uuid

            venv_name = f"{digest[:16]}-{uuid.uuid4().hex[:8]}"
        else:
            # Reuse venv for same digest (faster, still process-isolated)
            venv_name = f"{digest[:16]}-py{sys.version_info.major}.{sys.version_info.minor}"

        venv_path = self.venvs_dir / venv_name

        if venv_path.exists() and not self.force_fresh_venv:
            logger.debug(f"Reusing venv: {venv_path}")
            return venv_path

        # Create fresh venv (inline - TODO: extract to venv_manager module)
        logger.info(f"Creating fresh venv: {venv_path}")
        try:
            subprocess.run(
                ["uv", "venv", str(venv_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            logger.info(f"Created venv: {venv_path}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create venv: {e.stderr}")
            raise RuntimeError(f"Failed to create venv: {e.stderr}") from e
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Venv creation timed out after 120s")

        # Note: Dependencies will be installed by cold_runner.py when it starts
        # (similar to subprocess_runner.py pattern)

        return venv_path

    def _prepare_subprocess_env(self) -> dict[str, str]:
        """Prepare clean environment for subprocess.

        Strips most inherited env vars to prevent leakage, keeping only
        what's necessary for bundle execution.

        Returns:
            Clean environment dict
        """
        # Start with minimal safe env
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "PYTHONNOUSERSITE": "1",  # Prevent user site-packages
            "PYTHONHASHSEED": "0",  # Deterministic hashing
        }

        # Pass through essential ModelOps config
        for key in [
            "MODELOPS_BUNDLE_REGISTRY",
            "MODELOPS_BUNDLES_CACHE_DIR",
            "AZURE_STORAGE_CONNECTION_STRING",
            "AZURE_STORAGE_ACCOUNT",
        ]:
            if key in os.environ:
                env[key] = os.environ[key]

        return env

    def _create_error_return(self, task: SimTask, error_msg: str, exit_code: int) -> SimReturn:
        """Create SimReturn for error case.

        Args:
            task: Original task
            error_msg: Error message
            exit_code: Subprocess exit code

        Returns:
            SimReturn with error artifact
        """
        param_id = task.params.param_id
        tid_components = f"{param_id[:16]}-{task.seed}-error"
        tid = hashlib.blake2b(tid_components.encode(), digest_size=32).hexdigest()

        error_data = json.dumps(
            {
                "error": error_msg,
                "exit_code": exit_code,
                "params": dict(task.params.params),
                "seed": task.seed,
                "executor": "cold",
            }
        ).encode()

        checksum = hashlib.blake2b(error_data, digest_size=32).hexdigest()

        return SimReturn(
            task_id=tid,
            outputs={
                "error": TableArtifact(
                    size=len(error_data),
                    inline=error_data,
                    checksum=checksum,
                )
            },
        )

    def health_check(self) -> dict[str, Any]:
        """Check health of execution environment."""
        return {
            "type": "cold",
            "status": "healthy",
            "venvs_dir": str(self.venvs_dir),
            "force_fresh_venv": self.force_fresh_venv,
            "timeout_seconds": self.timeout_seconds,
        }

    def shutdown(self):
        """Shutdown the execution environment.

        No cleanup needed - we don't keep any processes alive.
        """
        logger.info("Shutting down ColdExecEnv (no processes to clean up)")
