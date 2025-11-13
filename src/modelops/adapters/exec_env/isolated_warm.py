"""
Isolated warm execution environment using subprocess pools.

TODO: we need to clean up / refactor run() and run_aggregation().
"""

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from modelops_contracts import ErrorInfo, SimReturn, SimTask, TableArtifact
from modelops_contracts.ports import BundleRepository, ExecutionEnvironment
from modelops_contracts.simulation import AggregationReturn, AggregationTask

from ...services.provenance_schema import DEFAULT_SCHEMA, ProvenanceSchema
from ...services.provenance_store import ProvenanceStore
from ...worker.process_manager import WarmProcessManager

logger = logging.getLogger(__name__)


class IsolatedWarmExecEnv(ExecutionEnvironment):
    """Execution environment using warm isolated subprocesses.

    This environment maintains a pool of warm subprocesses, each
    isolated with its own virtual environment. Processes are reused
    for the same bundle digest to avoid repeated initialization.
    """

    def __init__(
        self,
        bundle_repo: BundleRepository,
        venvs_dir: Path,
        storage_dir: Path,
        mem_limit_bytes: int | None = None,
        max_warm_processes: int = 128,
        provenance_schema: ProvenanceSchema | None = None,
        force_fresh_venv: bool = False,
        disable_provenance_cache: bool = False,
        azure_backend: dict[str, Any] | None = None,
    ):
        """Initialize the execution environment.

        Args:
            bundle_repo: Repository for fetching bundles
            venvs_dir: Directory for virtual environments
            storage_dir: Directory for provenance-based storage
            mem_limit_bytes: Optional memory limit per process
            max_warm_processes: Maximum number of warm processes
            provenance_schema: Schema for storage paths (default: bundle invalidation)
            force_fresh_venv: Force fresh venv creation for each execution (debugging)
            disable_provenance_cache: Disable provenance cache lookups (debugging)
            azure_backend: Azure backend configuration for automatic uploads
        """
        self.bundle_repo = bundle_repo
        self.venvs_dir = venvs_dir
        self.storage_dir = storage_dir
        self.mem_limit_bytes = mem_limit_bytes
        self.disable_provenance_cache = disable_provenance_cache or os.environ.get(
            "MODELOPS_DISABLE_PROVENANCE", ""
        ).lower() in ("1", "true")

        # Create provenance store with optional Azure backend
        self.provenance = ProvenanceStore(
            storage_dir=storage_dir,
            schema=provenance_schema or DEFAULT_SCHEMA,
            azure_backend=azure_backend,
        )

        # Create process manager
        self._process_manager = WarmProcessManager(
            venvs_dir=venvs_dir,
            max_processes=max_warm_processes,
            force_fresh_venv=force_fresh_venv,
        )

    def run(self, task: SimTask) -> SimReturn:
        """Execute simulation task.

        Args:
            task: Simulation task to execute

        Returns:
            SimReturn with status and artifacts
        """
        # Check provenance store first (unless disabled)
        if not self.disable_provenance_cache:
            stored = self.provenance.get_sim(task)
            if stored:
                # Generate a task identifier for logging
                task_ident = f"{task.params.param_id[:8]}-seed{task.seed}"
                logger.debug(f"Cache hit for task {task_ident}")
                return stored

        try:
            # 1. Resolve bundle
            digest, bundle_path = self._resolve_bundle(task.bundle_ref)

            # 2. Execute in subprocess
            raw_artifacts = self._process_manager.execute_task(
                bundle_digest=digest,
                bundle_path=bundle_path,
                entrypoint=str(task.entrypoint) if task.entrypoint else "main",
                params=dict(task.params.params),
                seed=task.seed,
            )

            # 3. Create return value
            result = self._create_sim_return(task, raw_artifacts)

            # 4. Store in provenance (if cache enabled)
            if not self.disable_provenance_cache:
                self.provenance.put_sim(task, result)

            return result

        except Exception as e:
            return self._create_error_return(
                task.bundle_ref,
                str(task.entrypoint) if task.entrypoint else "main",
                dict(task.params.params),
                task.seed,
                e,
            )

    def run_aggregation(self, task: AggregationTask) -> AggregationReturn:
        """Execute aggregation task.

        Args:
            task: AggregationTask with target and sim results

        Returns:
            AggregationReturn with loss and diagnostics
        """
        # Check provenance store first (unless disabled)
        if not self.disable_provenance_cache:
            stored = self.provenance.get_agg(task)
            if stored:
                logger.debug(f"Cache hit for aggregation {task.aggregation_id()}")
                return stored

        try:
            # 1. Resolve bundle
            digest, bundle_path = self._resolve_bundle(task.bundle_ref)

            # 2. Serialize for subprocess (sim_returns already have inline data)
            serialized_returns = self._serialize_sim_returns(task.sim_returns)

            # 3. Execute aggregation
            result = self._process_manager.execute_aggregation(
                bundle_digest=digest,
                bundle_path=bundle_path,
                target_entrypoint=str(task.target_entrypoint),
                sim_returns=serialized_returns,
                target_data=task.target_data,
            )

            # 4. Handle errors and return
            if "error" in result:
                from modelops.utils.error_utils import format_aggregation_error

                raise RuntimeError(format_aggregation_error(result))

            # Build diagnostics, including per-replicate losses if available
            diagnostics = result.get("diagnostics", {})
            if "per_replicate_losses" in result:
                diagnostics["per_replicate_losses"] = result["per_replicate_losses"]

            agg_return = AggregationReturn(
                aggregation_id=task.aggregation_id(),
                loss=result["loss"],
                diagnostics=diagnostics,
                outputs={},  # Could add aggregated outputs
                n_replicates=result.get("n_replicates", len(task.sim_returns)),
            )

            # 5. Store in provenance (if enabled)
            if not self.disable_provenance_cache:
                self.provenance.put_agg(task, agg_return)

            return agg_return

        except Exception as e:
            logger.error(f"Aggregation execution failed: {e}")
            raise

    def health_check(self) -> dict[str, Any]:
        """Check health of execution environment."""
        return {
            "type": "isolated_warm",
            "active_processes": self._process_manager.active_count(),
            "venvs_dir": str(self.venvs_dir),
        }

    def shutdown(self):
        """Clean shutdown of all warm processes."""
        logger.info("Shutting down IsolatedWarmExecEnv")
        self._process_manager.shutdown_all()
        # Shutdown provenance store (flushes any pending blob uploads)
        self.provenance.shutdown()

    def _resolve_bundle(self, bundle_ref: str) -> tuple[str, Path]:
        """Resolve bundle reference to local path.

        Args:
            bundle_ref: Bundle reference to resolve

        Returns:
            Tuple of (digest, local_path)
        """
        # SimTask supports both sha256:... and repository@sha256:... formats
        # The bundle repository should handle both
        return self.bundle_repo.ensure_local(bundle_ref)

    def _create_sim_return(self, task: SimTask, raw_artifacts: dict[str, Any]) -> SimReturn:
        """Create SimReturn from task and raw subprocess artifacts.

        Args:
            task: Original simulation task
            raw_artifacts: Raw artifacts from subprocess

        Returns:
            SimReturn with status and artifacts

        Raises:
            RuntimeError: If subprocess returned an error
        """

        # Log total artifact size for debugging (without full decode)
        def estimate_b64_size(s: str) -> int:
            """Estimate decoded size from base64 without decoding."""
            n = len(s)
            # Base64 encoding increases size by ~4/3, so decoded is ~3/4
            pad = 2 if s.endswith("==") else 1 if s.endswith("=") else 0
            return (3 * (n // 4)) - pad

        total_size = sum(
            estimate_b64_size(v) if isinstance(v, str) else len(v) for v in raw_artifacts.values()
        )

        # Warn about large payloads
        if total_size > 1024 * 1024:  # 1MB threshold
            logger.warning(
                f"Large sim output: {total_size:,} bytes for task "
                f"{task.params.param_id[:8]}-seed{task.seed}"
            )
        # Check for subprocess errors
        if len(raw_artifacts) == 1 and "error" in raw_artifacts:
            error_data = base64.b64decode(raw_artifacts["error"])
            # Import json locally to avoid Python 3.13 scope issue
            import json as json_module

            error_info = json_module.loads(error_data)
            raise RuntimeError(
                f"Subprocess execution failed: {error_info.get('error', 'Unknown error')} "
                f"(type: {error_info.get('type', 'Unknown')})"
            )

        # Create simple task_id from params and seed
        # Use param_id + seed + outputs for uniqueness
        param_id = task.params.param_id
        seed_str = str(task.seed)

        # Convert raw artifacts to TableArtifacts (always inline for MVP)
        outputs = {}
        for name, data in raw_artifacts.items():
            # Data comes back as base64-encoded strings from subprocess
            decoded_data = base64.b64decode(data) if isinstance(data, str) else data

            # Check for error metadata from wire function
            if name == "metadata" and decoded_data:
                # Import json locally to avoid Python 3.13 scope issue
                import json as json_module

                try:
                    metadata = json_module.loads(decoded_data)
                    if "error" in metadata:
                        # LOUD failure - registry missing or other wire error
                        raise RuntimeError(
                            f"Wire function error: {metadata['error']}\n"
                            f"Entrypoint: {metadata.get('entrypoint', 'unknown')}\n"
                            f"Seed: {metadata.get('seed', 'unknown')}\n"
                            f"This typically means the bundle is missing required files (e.g., registry.yaml)."
                        )
                except (ValueError, UnicodeDecodeError):
                    # Note: Using ValueError to catch JSON decode errors
                    # Local import avoids Python 3.13 scope issues
                    pass  # Not JSON metadata, continue

            # Warn about empty outputs for key artifacts
            if name == "table" and len(decoded_data) == 0:
                logger.warning(
                    f"Empty table output detected for task {task.params.param_id[:8]}-seed{task.seed}"
                )

            checksum = hashlib.blake2b(decoded_data, digest_size=32).hexdigest()

            outputs[name] = TableArtifact(
                size=len(decoded_data), inline=decoded_data, checksum=checksum
            )

        # Generate task_id from components
        output_names = tuple(outputs.keys())
        tid_components = f"{param_id[:16]}-{seed_str}-{','.join(sorted(output_names))}"
        tid = hashlib.blake2b(tid_components.encode(), digest_size=32).hexdigest()

        # Final validation - ensure we have meaningful outputs
        table_artifacts = [
            art
            for name, art in outputs.items()
            if isinstance(art, TableArtifact) and name != "metadata"
        ]
        if table_artifacts and all(art.size == 0 for art in table_artifacts):
            raise RuntimeError(
                f"All outputs are empty for task {task.params.param_id[:8]}-seed{task.seed}. "
                f"Check wire function execution and model outputs. "
                f"This often indicates the model registry could not be found or loaded."
            )

        return SimReturn(task_id=tid, outputs=outputs)

    def _serialize_sim_returns(self, sim_returns: list[SimReturn]) -> list[dict]:
        """Serialize SimReturns for JSON-RPC transport.

        Args:
            sim_returns: List of SimReturns with inline data

        Returns:
            List of serialized SimReturn dicts for subprocess communication
        """
        serialized_returns = []
        for sr in sim_returns:
            sr_dict = {"task_id": sr.task_id, "outputs": {}}

            for name, artifact in sr.outputs.items():
                # For MVP, always use inline data
                if not artifact.inline:
                    raise ValueError(f"Artifact {name} missing inline data for aggregation")

                sr_dict["outputs"][name] = {
                    "size": artifact.size,
                    "checksum": artifact.checksum,
                    "inline": base64.b64encode(artifact.inline).decode("ascii"),
                }

            serialized_returns.append(sr_dict)

        return serialized_returns

    def _create_error_return(
        self,
        bundle_ref: str,
        entrypoint: str,
        params: dict,
        seed: int,
        exception: Exception,
    ) -> SimReturn:
        """Create error SimReturn from exception.

        Args:
            bundle_ref: Bundle reference
            entrypoint: EntryPoint string
            params: Task parameters
            seed: Task seed
            exception: Exception that occurred

        Returns:
            SimReturn with error information
        """
        logger.exception(f"Task execution failed for bundle {bundle_ref}")

        # Create simple task_id for error case
        from modelops_contracts import make_param_id

        param_id = make_param_id(params)
        tid_components = f"{param_id[:16]}-{seed}-error"
        tid = hashlib.blake2b(tid_components.encode(), digest_size=32).hexdigest()

        # Create error info
        error_info = ErrorInfo(
            error_type=type(exception).__name__,
            message=str(exception),
            retryable=False,  # Could be smarter about this based on error type
        )

        # Store full error details as artifact (always inline for MVP)
        # Import json locally to avoid Python 3.13 scope issue
        import json as json_module

        error_details_data = json_module.dumps(
            {
                "error": str(exception),
                "type": type(exception).__name__,
                "bundle_ref": bundle_ref,
                "entrypoint": entrypoint,
                "traceback": None,  # Could capture traceback if needed
            }
        ).encode()
        checksum = hashlib.blake2b(error_details_data, digest_size=32).hexdigest()

        error_details = TableArtifact(
            size=len(error_details_data), inline=error_details_data, checksum=checksum
        )

        return SimReturn(
            task_id=tid,
            outputs={},  # Empty outputs for error case
            error=error_info,
            error_details=error_details,
        )
