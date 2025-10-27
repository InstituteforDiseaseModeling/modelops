"""Result indexing for post-job analysis.

This module implements automatic indexing of simulation results after job completion,
producing query-ready Parquet files with all losses indexed by parameter and target.
"""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

from modelops_contracts import SimJob, SimTask
from modelops_contracts.utils import canonical_task_id

from .. import provenance_paths as paths
from ..job_registry import JobRegistry
from ..job_state import JobStatus
from ..provenance_store import ProvenanceStore


@dataclass
class IndexerConfig:
    """Configuration for result indexer."""

    job_id: str
    job_registry_uri: Optional[str] = None
    prov_root: str = "/tmp/modelops/provenance/token/v1"
    index_version: int = 1


class ResultIndexer:
    """Index job results into query-ready Parquet datasets."""

    def __init__(
        self,
        cfg: IndexerConfig,
        job_registry: JobRegistry,
        prov_store: ProvenanceStore,
    ):
        """Initialize the result indexer.

        Args:
            cfg: Indexer configuration
            job_registry: Registry for job metadata
            prov_store: Storage for results
        """
        self.cfg = cfg
        self.job_registry = job_registry
        self.store = prov_store

    def run(self) -> str:
        """Index all results for the configured job.

        Returns:
            Path to the created Parquet dataset

        Raises:
            ValueError: If job not found or invalid
        """
        # Get job from registry
        job_state = self.job_registry.get_job(self.cfg.job_id)
        if not job_state:
            raise ValueError(f"Job {self.cfg.job_id} not found")

        job: SimJob = job_state.job_spec
        bundle_ref = job.bundle_ref
        bundle_digest = self._extract_bundle_digest(bundle_ref)

        # Derive expected aggregations from job spec
        groups = job.get_task_groups()
        targets = self._resolve_targets(job)
        expected = self._build_expected(groups, targets, bundle_ref)

        # Build rows for all expected results
        rows = []
        for exp in expected:
            meta, res = self._read_aggregation(exp["agg_path"])
            if res is None:
                rows.append(self._row_missing(job, exp))
            else:
                rows.append(self._row_available(job, exp, meta, res))

        # Write dataset atomically
        root = paths.job_view_root(self.cfg.job_id)
        # Make paths relative by removing leading slash
        relative_root = root.lstrip('/')
        tmp_dir = f"{relative_root}/losses.parquet.__tmp__/{uuid.uuid4().hex}"
        self._write_parquet_partitioned(tmp_dir, rows)

        # Atomic rename to final location
        final_path = f"{relative_root}/losses.parquet"
        self.store.atomic_rename(tmp_dir, final_path)

        # Write metadata files
        self._write_metadata(relative_root, job, rows, bundle_digest)

        return final_path

    def _extract_bundle_digest(self, bundle_ref: str) -> str:
        """Extract full digest from bundle reference.

        Args:
            bundle_ref: Bundle reference like "oci://reg/model@sha256:abc..."

        Returns:
            64-char hex digest
        """
        if "@" in bundle_ref:
            digest_part = bundle_ref.split("@")[-1]
        else:
            digest_part = bundle_ref

        if ":" in digest_part:
            return digest_part.split(":", 1)[1]
        return digest_part

    def _resolve_targets(self, job: SimJob) -> List[str]:
        """Extract target entrypoints from job.

        Args:
            job: SimJob specification

        Returns:
            List of target entrypoint strings
        """
        targets = []

        # Check target_spec first
        if job.target_spec and "targets" in job.target_spec.metadata:
            targets = list(job.target_spec.metadata["targets"])
        # Fall back to job metadata
        elif "targets" in job.metadata:
            targets = list(job.metadata["targets"])

        return targets

    def _build_expected(
        self,
        groups: Dict[str, List[SimTask]],
        targets: List[str],
        bundle_ref: str,
    ) -> List[Dict[str, Any]]:
        """Build expected results from job specification.

        Args:
            groups: Tasks grouped by param_id
            targets: Target entrypoints
            bundle_ref: Bundle reference

        Returns:
            List of expected aggregation specifications
        """
        expected = []
        for param_id, tasks in groups.items():
            # Generate task IDs for this parameter set
            task_ids = [canonical_task_id(task) for task in tasks]

            # Create expected aggregation for each target
            for target in targets:
                expected.append({
                    "param_id": param_id,
                    "target": target,
                    "agg_path": paths.agg_path(bundle_ref, target, task_ids),
                    "n_replicates": len(tasks),
                    "parameters": tasks[0].params.params,  # All tasks have same params
                })

        return expected

    def _read_aggregation(
        self, agg_path: str
    ) -> Tuple[Optional[Dict], Optional[Dict]]:
        """Read aggregation result and metadata.

        Args:
            agg_path: Path to aggregation directory

        Returns:
            Tuple of (metadata, result) or (None, None) if missing
        """
        try:
            result_path = os.path.join(agg_path, "result.json")
            metadata_path = os.path.join(agg_path, "metadata.json")

            result = self.store.try_read_json(result_path)
            metadata = self.store.try_read_json(metadata_path)

            if result is None or metadata is None:
                return None, None

            return metadata, result
        except Exception:
            return None, None

    def _row_missing(self, job: SimJob, exp: Dict[str, Any]) -> Dict[str, Any]:
        """Create row for missing aggregation result.

        Args:
            job: Job specification
            exp: Expected aggregation spec

        Returns:
            Row dictionary with NULL loss
        """
        return {
            "job_id": job.job_id,
            "bundle_digest": self._extract_bundle_digest(job.bundle_ref),
            "target": exp["target"],
            "param_id": exp["param_id"],
            "loss": None,
            "n_replicates": exp["n_replicates"],
            "computed_at": None,
            "status": "missing",
            "cache_hit": None,
            "parameters": dict(exp["parameters"]),
        }

    def _row_available(
        self,
        job: SimJob,
        exp: Dict[str, Any],
        metadata: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create row for available aggregation result.

        Args:
            job: Job specification
            exp: Expected aggregation spec
            metadata: Aggregation metadata
            result: Aggregation result

        Returns:
            Row dictionary with loss value
        """
        # Extract timestamp
        timestamp_str = metadata.get("timestamp") or metadata.get("computed_at")
        timestamp = None
        if timestamp_str:
            try:
                timestamp = datetime.fromisoformat(
                    timestamp_str.replace("Z", "+00:00")
                )
            except Exception:
                pass

        return {
            "job_id": job.job_id,
            "bundle_digest": self._extract_bundle_digest(job.bundle_ref),
            "target": exp["target"],
            "param_id": exp["param_id"],
            "loss": result.get("loss"),
            "n_replicates": result.get("n_replicates", exp["n_replicates"]),
            "computed_at": timestamp,
            "status": "available",
            "cache_hit": None,  # Can be added later
            "parameters": dict(exp["parameters"]),
        }

    def _write_parquet_partitioned(
        self, output_path: str, rows: List[Dict[str, Any]]
    ) -> None:
        """Write partitioned Parquet dataset.

        Args:
            output_path: Root directory for dataset (relative to storage_dir)
            rows: Result rows to write
        """
        # Define Arrow schema
        schema = pa.schema([
            pa.field("job_id", pa.string()),
            pa.field("bundle_digest", pa.string()),
            pa.field("target", pa.string()),
            pa.field("param_id", pa.string()),
            pa.field("loss", pa.float64()),
            pa.field("n_replicates", pa.int32()),
            pa.field("computed_at", pa.timestamp("us", tz="UTC")),
            pa.field("status", pa.string()),
            pa.field("cache_hit", pa.bool_()),
            pa.field("parameters", pa.map_(pa.string(), pa.float64())),
        ])

        # Group rows by target for partitioning
        by_target: Dict[str, List[Dict]] = {}
        for row in rows:
            target = row["target"]
            by_target.setdefault(target, []).append(row)

        # Write each partition
        for target, target_rows in by_target.items():
            # Create partition directory using the store's base path
            partition_path = f"{output_path}/target={target}"
            full_partition_dir = self.store.storage_dir / partition_path.lstrip('/')
            full_partition_dir.mkdir(parents=True, exist_ok=True)

            # Remove target column from rows since it's in the partition path
            rows_without_target = [
                {k: v for k, v in row.items() if k != "target"}
                for row in target_rows
            ]

            # Schema without target column (partitioned column)
            schema_without_target = pa.schema([
                field for field in schema if field.name != "target"
            ])

            # Convert to Arrow table
            table = pa.Table.from_pylist(rows_without_target, schema=schema_without_target)

            # Write Parquet file
            pq.write_table(
                table,
                full_partition_dir / "part-0.parquet",
                compression="snappy",
            )

    def _write_metadata(
        self,
        root: str,
        job: SimJob,
        rows: List[Dict[str, Any]],
        bundle_digest: str,
    ) -> None:
        """Write manifest and summary JSON files.

        Args:
            root: Root directory for job views
            job: Job specification
            rows: Result rows
            bundle_digest: Full bundle digest
        """
        # Calculate counts
        counts = self._calculate_counts(rows)

        # Write manifest
        manifest = {
            "index_version": self.cfg.index_version,
            "job_id": job.job_id,
            "bundle_digest": bundle_digest,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "row_counts": counts,
            "input_fingerprint": self._fingerprint(job),
            "dataset_uri": f"{root}/losses.parquet",
            "schema_hash": "v1",
        }
        self.store.write_json(f"{root}/manifest.json", manifest)

        # Write summary
        summary = self._generate_summary(rows)
        self.store.write_json(f"{root}/summary.json", summary)

        # Write schema
        schema = {
            "version": 1,
            "columns": [
                "job_id",
                "bundle_digest",
                "target",
                "param_id",
                "loss",
                "n_replicates",
                "computed_at",
                "status",
                "cache_hit",
                "parameters",
            ],
        }
        self.store.write_json(f"{root}/schema.json", schema)

    def _calculate_counts(self, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """Calculate row counts by status.

        Args:
            rows: Result rows

        Returns:
            Dictionary with count statistics
        """
        total = len(rows)
        available = sum(1 for r in rows if r["status"] == "available")
        failed = sum(1 for r in rows if r["status"] == "failed")
        missing = total - available - failed

        return {
            "total": total,
            "available": available,
            "missing": missing,
            "failed": failed,
        }

    def _generate_summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate summary statistics.

        Args:
            rows: Result rows

        Returns:
            Summary dictionary with per-target statistics
        """
        by_target: Dict[str, List[Dict]] = {}
        for row in rows:
            by_target.setdefault(row["target"], []).append(row)

        summary = {}
        for target, target_rows in by_target.items():
            # Get non-null losses
            losses = [r["loss"] for r in target_rows if r["loss"] is not None]

            if not losses:
                summary[target] = {
                    "count": len(target_rows),
                    "available": 0,
                    "missing": len(target_rows),
                }
            else:
                # Find best parameters
                sorted_rows = sorted(
                    [r for r in target_rows if r["loss"] is not None],
                    key=lambda x: x["loss"],
                )
                best_5 = [
                    {"param_id": r["param_id"], "loss": r["loss"]}
                    for r in sorted_rows[:5]
                ]

                summary[target] = {
                    "count": len(target_rows),
                    "available": len(losses),
                    "missing": len(target_rows) - len(losses),
                    "min_loss": min(losses),
                    "max_loss": max(losses),
                    "median_loss": sorted(losses)[len(losses) // 2],
                    "best_params": best_5,
                }

        return summary

    def _fingerprint(self, job: SimJob) -> str:
        """Generate fingerprint of job specification.

        Args:
            job: Job specification

        Returns:
            Hex fingerprint string
        """
        # Create stable representation of job spec
        groups = job.get_task_groups()
        param_ids = sorted(groups.keys())
        targets = self._resolve_targets(job)

        payload = json.dumps({
            "bundle": job.bundle_ref,
            "param_ids": param_ids,
            "targets": targets,
        }, sort_keys=True)

        return hashlib.blake2b(payload.encode(), digest_size=8).hexdigest()