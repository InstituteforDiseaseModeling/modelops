"""
Isolated warm execution environment using subprocess pools.

TODO: we need to clean up / refactor run() and run_aggregation().
"""

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Any, List, Tuple
from dataclasses import replace

from modelops_contracts import SimTask, SimReturn, TableArtifact, ErrorInfo, task_id, sim_root
from modelops_contracts.simulation import AggregationTask, AggregationReturn
from modelops_contracts.ports import ExecutionEnvironment, BundleRepository

from ...worker.process_manager import WarmProcessManager
from ...services.provenance_store import ProvenanceStore
from ...services.provenance_schema import ProvenanceSchema, DEFAULT_SCHEMA

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
        mem_limit_bytes: Optional[int] = None,
        max_warm_processes: int = 128,
        provenance_schema: Optional[ProvenanceSchema] = None,
        force_fresh_venv: bool = False
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
        """
        self.bundle_repo = bundle_repo
        self.venvs_dir = venvs_dir
        self.storage_dir = storage_dir
        self.mem_limit_bytes = mem_limit_bytes

        # Create provenance store
        self.provenance = ProvenanceStore(
            storage_dir=storage_dir,
            schema=provenance_schema or DEFAULT_SCHEMA
        )

        # Create process manager
        self._process_manager = WarmProcessManager(
            venvs_dir=venvs_dir,
            max_processes=max_warm_processes,
            force_fresh_venv=force_fresh_venv
        )
    
    def run(self, task: SimTask) -> SimReturn:
        """Execute simulation task.

        Args:
            task: Simulation task to execute

        Returns:
            SimReturn with status and artifacts
        """
        # Check provenance store first
        stored = self.provenance.get_sim(task)
        if stored:
            logger.debug(f"Cache hit for task {task.task_id()}")
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
                seed=task.seed
            )

            # 3. Create return value
            result = self._create_sim_return(task, raw_artifacts)

            # 4. Store in provenance
            self.provenance.put_sim(task, result)

            return result

        except Exception as e:
            return self._create_error_return(
                task.bundle_ref,
                str(task.entrypoint) if task.entrypoint else "main",
                dict(task.params.params),
                task.seed,
                e
            )
    
    def run_aggregation(self, task: AggregationTask) -> AggregationReturn:
        """Execute aggregation task.

        Args:
            task: AggregationTask with target and sim results

        Returns:
            AggregationReturn with loss and diagnostics
        """
        # Check provenance store first
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
                target_data=task.target_data
            )

            # 4. Handle errors and return
            if 'error' in result:
                error_msg = result['error']
                error_type = result.get('type', 'Unknown')
                raise RuntimeError(f"Aggregation failed in subprocess: {error_msg} (type: {error_type})")

            agg_return = AggregationReturn(
                aggregation_id=task.aggregation_id(),
                loss=result['loss'],
                diagnostics=result.get('diagnostics', {}),
                outputs={},  # Could add aggregated outputs
                n_replicates=result.get('n_replicates', len(task.sim_returns))
            )

            # 5. Store in provenance
            self.provenance.put_agg(task, agg_return)

            return agg_return

        except Exception as e:
            logger.error(f"Aggregation execution failed: {e}")
            raise
    
    def health_check(self) -> Dict[str, Any]:
        """Check health of execution environment."""
        return {
            'type': 'isolated_warm',
            'active_processes': self._process_manager.active_count(),
            'venvs_dir': str(self.venvs_dir)
        }
    
    def shutdown(self):
        """Clean shutdown of all warm processes."""
        logger.info("Shutting down IsolatedWarmExecEnv")
        self._process_manager.shutdown_all()

    def _resolve_bundle(self, bundle_ref: str) -> tuple[str, Path]:
        """Resolve bundle reference to local path.

        Args:
            bundle_ref: Bundle reference to resolve

        Returns:
            Tuple of (digest, local_path)
        """
        return self.bundle_repo.ensure_local(bundle_ref)


    def _create_sim_return(self, task: SimTask, raw_artifacts: Dict[str, Any]) -> SimReturn:
        """Create SimReturn from task and raw subprocess artifacts.

        Args:
            task: Original simulation task
            raw_artifacts: Raw artifacts from subprocess

        Returns:
            SimReturn with status and artifacts

        Raises:
            RuntimeError: If subprocess returned an error
        """
        # Check for subprocess errors
        if len(raw_artifacts) == 1 and "error" in raw_artifacts:
            error_data = base64.b64decode(raw_artifacts["error"])
            error_info = json.loads(error_data)
            raise RuntimeError(
                f"Subprocess execution failed: {error_info.get('error', 'Unknown error')} "
                f"(type: {error_info.get('type', 'Unknown')})"
            )

        # Create proper sim_root and task_id
        root = sim_root(
            bundle_ref=task.bundle_ref,
            params=dict(task.params.params),
            seed=task.seed,
            entrypoint=str(task.entrypoint) if task.entrypoint else "main"
        )

        # Convert raw artifacts to TableArtifacts (always inline for MVP)
        outputs = {}
        for name, data in raw_artifacts.items():
            # Data comes back as base64-encoded strings from subprocess
            decoded_data = base64.b64decode(data) if isinstance(data, str) else data
            checksum = hashlib.blake2b(decoded_data, digest_size=32).hexdigest()

            outputs[name] = TableArtifact(
                size=len(decoded_data),
                inline=decoded_data,
                checksum=checksum
            )

        # Determine output names for task_id
        output_names = tuple(outputs.keys())
        tid = task_id(
            sim_root=root,
            entrypoint=str(task.entrypoint) if task.entrypoint else "main",
            outputs=output_names
        )

        return SimReturn(
            task_id=tid,
            sim_root=root,
            outputs=outputs
        )


    def _serialize_sim_returns(self, sim_returns: List[SimReturn]) -> List[Dict]:
        """Serialize SimReturns for JSON-RPC transport.

        Args:
            sim_returns: List of SimReturns with inline data

        Returns:
            List of serialized SimReturn dicts for subprocess communication
        """
        serialized_returns = []
        for sr in sim_returns:
            sr_dict = {
                'task_id': sr.task_id,
                'sim_root': sr.sim_root,
                'outputs': {}
            }

            for name, artifact in sr.outputs.items():
                # For MVP, always use inline data
                if not artifact.inline:
                    raise ValueError(f"Artifact {name} missing inline data for aggregation")

                sr_dict['outputs'][name] = {
                    'size': artifact.size,
                    'checksum': artifact.checksum,
                    'inline': base64.b64encode(artifact.inline).decode('ascii')
                }

            serialized_returns.append(sr_dict)

        return serialized_returns

    def _create_error_return(self, bundle_ref: str, entrypoint: str, params: dict, seed: int, exception: Exception) -> SimReturn:
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

        # Create error structure
        root = sim_root(
            bundle_ref=bundle_ref,
            params=params,
            seed=seed,
            entrypoint=entrypoint
        )
        tid = task_id(
            sim_root=root,
            entrypoint=entrypoint,
            outputs=("error",)
        )

        # Create error info
        error_info = ErrorInfo(
            error_type=type(exception).__name__,
            message=str(exception),
            retryable=False  # Could be smarter about this based on error type
        )

        # Store full error details as artifact (always inline for MVP)
        error_details_data = json.dumps({
            "error": str(exception),
            "type": type(exception).__name__,
            "bundle_ref": bundle_ref,
            "entrypoint": entrypoint,
            "traceback": None  # Could capture traceback if needed
        }).encode()
        checksum = hashlib.blake2b(error_details_data, digest_size=32).hexdigest()

        error_details = TableArtifact(
            size=len(error_details_data),
            inline=error_details_data,
            checksum=checksum
        )

        return SimReturn(
            task_id=tid,
            sim_root=root,
            outputs={},  # Empty outputs for error case
            error=error_info,
            error_details=error_details
        )
