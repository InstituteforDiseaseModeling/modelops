"""Unified provenance-based storage for simulation results.

This module provides a single storage system that replaces both the
SimulationCache and CAS (Content-Addressed Storage). It uses input-addressed
storage (hash of inputs) rather than content-addressed storage.
"""

import json
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import asdict, dataclass

from modelops_contracts import (
    SimTask,
    SimReturn,
    TableArtifact,
    ErrorInfo
)
from modelops_contracts.simulation import AggregationTask, AggregationReturn

from .provenance_schema import ProvenanceSchema, DEFAULT_SCHEMA
from .storage_utils import atomic_write


logger = logging.getLogger(__name__)


@dataclass
class StoredResult:
    """Result stored with metadata."""
    metadata: Dict[str, Any]  # SimTask/AggTask metadata
    result: Any  # SimReturn or AggregationReturn


class ProvenanceStore:
    """
    Unified storage for all simulation and aggregation results.

    Uses provenance-based (input-addressed) storage to enable
    efficient caching and invalidation. For MVP, always stores
    as blobs on disk and returns inline in memory.
    """

    def __init__(
        self,
        storage_dir: Path,
        schema: ProvenanceSchema = DEFAULT_SCHEMA
    ):
        """Initialize provenance store.

        Args:
            storage_dir: Root directory for storage
            schema: Schema for path generation
        """
        self.storage_dir = Path(storage_dir)
        self.schema = schema
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Initialized ProvenanceStore with schema '{schema.name}' at {storage_dir}")

    def _write_json_atomic(self, path: Path, data: Dict) -> None:
        """Write JSON atomically to avoid corruption.

        Args:
            path: Target file path
            data: Dictionary to serialize as JSON
        """
        content = json.dumps(data, indent=2).encode('utf-8')
        atomic_write(path, content)

    def get_sim(self, task: SimTask) -> Optional[SimReturn]:
        """Retrieve simulation result if it exists.

        Args:
            task: Simulation task specification

        Returns:
            SimReturn if found, None otherwise
        """
        # Generate storage path
        path_context = self._sim_path_context(task)
        result_dir = self.storage_dir / self.schema.sim_path(**path_context)

        if not result_dir.exists():
            return None

        try:
            # Load metadata
            metadata_file = result_dir / "metadata.json"
            if not metadata_file.exists():
                logger.warning(f"Missing metadata.json in {result_dir}")
                return None

            with open(metadata_file, "r") as f:
                metadata = json.load(f)

            # Load result
            result_file = result_dir / "result.json"
            if not result_file.exists():
                logger.warning(f"Missing result.json in {result_dir}")
                return None

            with open(result_file, "r") as f:
                result_data = json.load(f)

            # Reconstruct SimReturn with TableArtifacts
            outputs = {}
            for name, artifact_data in result_data.get("outputs", {}).items():
                # For MVP, always store as blob and load inline
                artifact_file = result_dir / f"artifact_{name}.arrow"
                if artifact_file.exists():
                    with open(artifact_file, "rb") as f:
                        inline_data = f.read()
                    outputs[name] = TableArtifact(
                        size=len(inline_data),
                        inline=inline_data,
                        checksum=artifact_data["checksum"]
                    )
                else:
                    logger.warning(f"Missing artifact file: {artifact_file}")

            # Reconstruct error info if present
            error = None
            error_details = None
            if "error" in result_data:
                error = ErrorInfo(
                    error_type=result_data["error"]["error_type"],
                    message=result_data["error"]["message"],
                    retryable=result_data["error"]["retryable"]
                )

                # Load error details if present
                if "error_details" in result_data:
                    error_file = result_dir / "error_details.arrow"
                    if error_file.exists():
                        with open(error_file, "rb") as f:
                            error_data = f.read()
                        error_details = TableArtifact(
                            size=len(error_data),
                            inline=error_data,
                            checksum=result_data["error_details"]["checksum"]
                        )

            return SimReturn(
                task_id=result_data["task_id"],
                outputs=outputs,
                error=error,
                error_details=error_details,
                cached=True
            )

        except Exception as e:
            logger.error(f"Failed to load simulation result from {result_dir}: {e}")
            return None

    def put_sim(self, task: SimTask, result: SimReturn) -> str:
        """Store simulation result.

        Args:
            task: Simulation task specification
            result: Simulation result to store

        Returns:
            Storage path for the result
        """
        # Generate storage path
        path_context = self._sim_path_context(task)
        result_dir = self.storage_dir / self.schema.sim_path(**path_context)
        result_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Store metadata
            metadata = {
                "bundle_ref": task.bundle_ref,
                "entrypoint": str(task.entrypoint),
                "params": dict(task.params.params),
                "seed": task.seed,
                "outputs": task.outputs,
                "param_id": task.params.param_id,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
            self._write_json_atomic(result_dir / "metadata.json", metadata)

            # No longer storing manifest - removed from SimTask

            # Store result metadata
            result_data = {
                "task_id": result.task_id,
                "outputs": {}
            }

            # Store error info if present
            if result.error:
                result_data["error"] = {
                    "error_type": result.error.error_type,
                    "message": result.error.message,
                    "retryable": result.error.retryable
                }

                # Store error details if present
                if result.error_details:
                    error_file = result_dir / "error_details.arrow"
                    if result.error_details.inline:
                        atomic_write(error_file, result.error_details.inline)
                    result_data["error_details"] = {
                        "size": result.error_details.size,
                        "checksum": result.error_details.checksum
                    }

            # Store artifacts as separate blob files
            for name, artifact in result.outputs.items():
                # For MVP, always store as blob
                if artifact.inline:
                    artifact_file = result_dir / f"artifact_{name}.arrow"
                    atomic_write(artifact_file, artifact.inline)

                # Store artifact metadata
                result_data["outputs"][name] = {
                    "size": artifact.size,
                    "checksum": artifact.checksum
                }

            self._write_json_atomic(result_dir / "result.json", result_data)

            logger.debug(f"Stored simulation result at {result_dir}")
            return str(result_dir)

        except Exception as e:
            logger.error(f"Failed to store simulation result: {e}")
            raise

    def get_agg(self, task: AggregationTask) -> Optional[AggregationReturn]:
        """Retrieve aggregation result if it exists.

        Args:
            task: Aggregation task specification

        Returns:
            AggregationReturn if found, None otherwise
        """
        # Generate storage path
        path_context = self._agg_path_context(task)
        result_dir = self.storage_dir / self.schema.agg_path(**path_context)

        if not result_dir.exists():
            return None

        try:
            # Load result
            result_file = result_dir / "result.json"
            if not result_file.exists():
                return None

            with open(result_file, "r") as f:
                result_data = json.load(f)

            return AggregationReturn(
                aggregation_id=result_data["aggregation_id"],
                loss=result_data["loss"],
                diagnostics=result_data.get("diagnostics", {}),
                outputs={},  # Could reconstruct if needed
                n_replicates=result_data["n_replicates"]
            )

        except Exception as e:
            logger.error(f"Failed to load aggregation result: {e}")
            return None

    def put_agg(self, task: AggregationTask, result: AggregationReturn) -> str:
        """Store aggregation result.

        Args:
            task: Aggregation task specification
            result: Aggregation result to store

        Returns:
            Storage path for the result
        """
        # Generate storage path
        path_context = self._agg_path_context(task)
        result_dir = self.storage_dir / self.schema.agg_path(**path_context)
        result_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Store metadata with param_id
            metadata = {
                "bundle_ref": task.bundle_ref,
                "target_entrypoint": str(task.target_entrypoint),
                "n_sim_returns": len(task.sim_returns),
                "param_id": self._extract_param_id_from_task(task),
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
            self._write_json_atomic(result_dir / "metadata.json", metadata)

            # Store result with input provenance tracking
            result_data = {
                "aggregation_id": result.aggregation_id,
                "loss": result.loss,
                "diagnostics": result.diagnostics,
                "n_replicates": result.n_replicates,
                "outputs": {},
                "inputs": [  # Track what we aggregated
                    {
                        "type": "sim",
                        "task_id": sr.task_id
                    }
                    for sr in task.sim_returns
                ]
            }

            # Store any aggregated outputs as artifacts (future enhancement)
            for name, artifact in result.outputs.items():
                if artifact.inline:
                    artifact_file = result_dir / f"artifact_{name}.arrow"
                    atomic_write(artifact_file, artifact.inline)

                    result_data["outputs"][name] = {
                        "size": artifact.size,
                        "checksum": artifact.checksum,
                        "content_type": "application/vnd.apache.arrow.file"
                    }

            self._write_json_atomic(result_dir / "result.json", result_data)

            logger.debug(f"Stored aggregation result at {result_dir}")
            return str(result_dir)

        except Exception as e:
            logger.error(f"Failed to store aggregation result: {e}")
            raise

    def list_results(
        self,
        result_type: str = "sim",
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """List stored results with metadata.

        Args:
            result_type: "sim" or "agg"
            limit: Maximum number of results

        Returns:
            List of result metadata dicts
        """
        results = []
        search_dir = self.storage_dir / self.schema.name / f"v{self.schema.version}" / f"{result_type}s"

        if not search_dir.exists():
            return results

        # Walk directory tree
        for result_dir in search_dir.rglob("metadata.json"):
            if len(results) >= limit:
                break

            try:
                with open(result_dir, "r") as f:
                    metadata = json.load(f)
                    metadata["path"] = str(result_dir.parent)
                    results.append(metadata)
            except Exception as e:
                logger.warning(f"Failed to read metadata from {result_dir}: {e}")

        return results

    def _sim_path_context(self, task: SimTask) -> Dict[str, Any]:
        """Generate path context for simulation task."""
        context = {
            "bundle_digest": hashlib.blake2b(
                task.bundle_ref.encode(), digest_size=32
            ).hexdigest(),
            "param_id": task.params.param_id,
            "seed": task.seed
        }

        # For token invalidation, would need model_digest from bundle
        # For now, use bundle_digest as fallback
        if "model_digest" in self.schema.sim_path_template:
            context["model_digest"] = hashlib.blake2b(
                task.bundle_ref.encode(), digest_size=32
            ).hexdigest()

        return context

    def _extract_param_id_from_task(self, task: AggregationTask) -> Optional[str]:
        """Extract param_id from aggregation task's sim_returns.

        Since all sim_returns in a replicate set share the same param_id,
        we can extract it from the first one. The task_id format includes
        the param_id in its first component.

        Args:
            task: AggregationTask with sim_returns

        Returns:
            param_id if extractable, None otherwise
        """
        if task.sim_returns and len(task.sim_returns) > 0:
            # task_id is generated as hash of: f"{param_id[:16]}-{seed_str}-{output_names}"
            # Since it's a hash, we can't directly extract param_id
            # But we can use the first 16 chars as a proxy identifier
            # Better approach: If we had access to the original SimTask, we'd have param_id directly
            first_task_id = task.sim_returns[0].task_id
            # Use first 16 chars of task_id as param identifier
            return first_task_id[:16]
        return None

    def _agg_path_context(self, task: AggregationTask) -> Dict[str, Any]:
        """Generate path context for aggregation task."""
        context = {
            "bundle_digest": hashlib.blake2b(
                task.bundle_ref.encode(), digest_size=32
            ).hexdigest(),
            "target": str(task.target_entrypoint).replace("/", "_"),
            "aggregation_id": task.aggregation_id()
        }

        # For token invalidation, would need model_digest from bundle
        # For now, use bundle_digest as fallback
        if "model_digest" in self.schema.agg_path_template:
            context["model_digest"] = hashlib.blake2b(
                task.bundle_ref.encode(), digest_size=32
            ).hexdigest()

        return context

    def clear_schema(self, schema_name: Optional[str] = None):
        """Clear all data for a schema (for testing/debugging).

        Args:
            schema_name: Schema to clear, or current schema if None
        """
        target_schema = schema_name or self.schema.name
        schema_dir = self.storage_dir / target_schema

        if schema_dir.exists():
            import shutil
            shutil.rmtree(schema_dir)
            logger.info(f"Cleared schema '{target_schema}' data")
        else:
            logger.info(f"Schema '{target_schema}' has no data to clear")
