"""Cold debug execution environment - shells out to ultra_cold_runner.py.

This adapter provides maximum isolation for debugging state leakage issues:
- Fresh Python process per task
- Fresh venv per task (or reused via REUSE=1)
- No shared state between tasks
- Complete C++ extension reload

Use for debugging non-deterministic behavior, state leakage, or shared memory issues.
Performance: ~500-1000ms per task vs ~50ms for warm executor.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from modelops_contracts import ErrorInfo, SimReturn, SimTask, TableArtifact
from modelops_contracts.ports import BundleRepository, ExecutionEnvironment
from modelops_contracts.simulation import AggregationReturn, AggregationTask

logger = logging.getLogger(__name__)

# Path to ultra_cold_runner.py
# __file__ is .../modelops/adapters/exec_env/cold_debug.py
# Runner is at .../modelops/tools/ultra_cold_runner.py
RUNNER_PATH = Path(__file__).parents[2] / "tools" / "ultra_cold_runner.py"


class ColdDebugExecEnv(ExecutionEnvironment):
    """Cold debug executor - spawns ultra_cold_runner.py per task.

    Each task runs in complete isolation:
    - Fresh subprocess
    - Fresh or reused venv (based on REUSE env var)
    - No state sharing between tasks

    Args:
        bundle_repo: Repository for fetching bundles
        runner_path: Path to ultra_cold_runner.py (default: auto-detect)
        timeout_seconds: Max execution time per task
        env: Additional environment variables for subprocess
    """

    def __init__(
        self,
        bundle_repo: BundleRepository,
        runner_path: Path | None = None,
        timeout_seconds: int = 600,
        env: dict[str, str] | None = None,
    ):
        self.bundle_repo = bundle_repo
        self.runner_path = Path(runner_path or RUNNER_PATH)
        self.timeout_seconds = timeout_seconds

        # Build environment for subprocess
        self.base_env = os.environ.copy()
        if env:
            self.base_env.update(env)

        if not self.runner_path.exists():
            raise RuntimeError(f"Runner script not found: {self.runner_path}")

        logger.info(f"ColdDebugExecEnv initialized (runner={self.runner_path})")

    def run(self, task: SimTask) -> SimReturn:
        """Execute simulation task in fresh subprocess.

        Args:
            task: Simulation task to execute

        Returns:
            SimReturn with results or error
        """
        digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)

        # Serialize task to JSON
        payload = json.dumps({
            "entrypoint": str(task.entrypoint) if task.entrypoint else "main",
            "params": dict(task.params.params),
            "seed": task.seed,
        })

        logger.info(f"Running simulation via cold runner (seed={task.seed})")

        # Invoke runner
        result_dict = self._invoke_runner(bundle_path, payload, aggregation=False)

        # Handle fatal errors
        if "_fatal_error" in result_dict:
            return self._mk_error_return(task, result_dict["_fatal_error"])

        # Reconstruct SimReturn
        outputs = {}
        for name, meta in result_dict["outputs"].items():
            # Decode base64 inline data
            inline_b64 = meta.get("inline")
            if isinstance(inline_b64, str):
                inline_bytes = base64.b64decode(inline_b64)
            else:
                inline_bytes = inline_b64 or b""

            outputs[name] = TableArtifact(
                size=len(inline_bytes),
                inline=inline_bytes,
                checksum=meta["checksum"],
            )

        # Generate task_id (same algorithm as runner for consistency)
        tid_input = f"{task.params.param_id[:16]}-{task.seed}-{','.join(sorted(outputs.keys()))}".encode()
        task_id = hashlib.blake2b(tid_input, digest_size=32).hexdigest()

        return SimReturn(task_id=task_id, outputs=outputs)

    def run_aggregation(self, task: AggregationTask) -> AggregationReturn:
        """Execute aggregation task in fresh subprocess.

        Args:
            task: Aggregation task to execute

        Returns:
            AggregationReturn with loss and diagnostics

        Raises:
            RuntimeError: If aggregation fails
        """
        digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)

        # Serialize aggregation task to JSON
        payload = json.dumps({
            "target_entrypoint": str(task.target_entrypoint),
            "sim_returns": [
                {
                    "task_id": sr.task_id,
                    "outputs": {
                        name: {
                            "size": art.size,
                            "checksum": art.checksum,
                            "inline": base64.b64encode(art.inline).decode("ascii") if art.inline else None,
                        }
                        for name, art in sr.outputs.items()
                    }
                }
                for sr in task.sim_returns
            ],
            "target_data": task.target_data,
        })

        logger.info(f"Running aggregation via cold runner (target={task.target_entrypoint})")

        # Invoke runner
        result_dict = self._invoke_runner(bundle_path, payload, aggregation=True)

        # Handle fatal errors
        if "_fatal_error" in result_dict:
            raise RuntimeError(f"Aggregation failed: {json.dumps(result_dict['_fatal_error'])}")

        # Reconstruct AggregationReturn
        return AggregationReturn(
            aggregation_id=task.aggregation_id(),
            loss=float(result_dict["loss"]),
            diagnostics=result_dict.get("diagnostics", {}),
            outputs={},
            n_replicates=result_dict.get("n_replicates", len(task.sim_returns)),
        )

    def health_check(self) -> dict[str, Any]:
        """Check health of execution environment."""
        return {
            "type": "cold_debug",
            "status": "healthy",
            "runner_path": str(self.runner_path),
            "runner_exists": self.runner_path.exists(),
            "timeout_seconds": self.timeout_seconds,
        }

    def shutdown(self):
        """Shutdown the execution environment.

        No cleanup needed - we don't keep any processes alive.
        """
        logger.info("Shutting down ColdDebugExecEnv (no processes to clean up)")

    # ---- Internal helpers ----

    def _invoke_runner(
        self,
        bundle_path: Path,
        payload: str,
        aggregation: bool
    ) -> dict[str, Any]:
        """Invoke ultra_cold_runner.py subprocess.

        Args:
            bundle_path: Path to unpacked bundle
            payload: JSON-serialized task data
            aggregation: True for aggregation, False for simulation

        Returns:
            Result dict from runner (or _fatal_error dict)
        """
        args = [
            sys.executable,
            "-u",  # Unbuffered
            str(self.runner_path),
            "--bundle-path",
            str(bundle_path),
        ]
        if aggregation:
            args.append("--aggregation")

        logger.debug(f"Invoking: {' '.join(args)}")

        try:
            result = subprocess.run(
                args,
                input=payload,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=self.base_env,
                cwd=str(bundle_path),
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            logger.error(f"Runner timed out after {self.timeout_seconds}s")
            return {
                "_fatal_error": {
                    "code": 124,
                    "error": f"Timeout after {self.timeout_seconds}s",
                    "stdout": e.stdout[:500] if e.stdout else "",
                    "stderr": e.stderr[:500] if e.stderr else "",
                }
            }

        # Forward child logs to stderr
        if result.stderr:
            sys.stderr.write(result.stderr)

        # Parse stdout as JSON
        try:
            out = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse runner output: {e}")
            return {
                "_fatal_error": {
                    "code": result.returncode,
                    "error": "Invalid JSON output",
                    "stdout": result.stdout[:500],
                    "stderr": result.stderr[:500],
                }
            }

        # If process failed but didn't return _fatal_error, construct one
        if result.returncode != 0 and "_fatal_error" not in out:
            out = {
                "_fatal_error": {
                    "code": result.returncode,
                    "stderr": result.stderr[:500],
                    "stdout": result.stdout[:500],
                }
            }

        return out

    def _mk_error_return(self, task: SimTask, err: dict[str, Any]) -> SimReturn:
        """Create SimReturn for error case.

        Args:
            task: Original task
            err: Error dict from runner

        Returns:
            SimReturn with error information
        """
        param_id = task.params.param_id
        tid_input = f"{param_id[:16]}-{task.seed}-error".encode()
        task_id = hashlib.blake2b(tid_input, digest_size=32).hexdigest()

        # Pack error details as inline artifact
        error_data = json.dumps(err).encode()

        return SimReturn(
            task_id=task_id,
            outputs={},
            error=ErrorInfo(
                error_type="ColdDebugError",
                message=json.dumps(err),
                retryable=False,
            ),
            error_details=TableArtifact(
                size=len(error_data),
                inline=error_data,
                checksum=hashlib.blake2b(error_data, digest_size=32).hexdigest(),
            ),
        )
