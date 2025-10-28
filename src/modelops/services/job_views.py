"""Write job results to Parquet views for post-job analysis.

This module provides functionality to write AggregationReturn objects
directly to Parquet files, bypassing the need for filesystem scanning
or Dask worker submission.
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from modelops_contracts import AggregationReturn, SimJob, TargetSpec

logger = logging.getLogger(__name__)


def write_job_view(
    job: SimJob,
    results: List[AggregationReturn],
    output_dir: Path = Path("/tmp/modelops/provenance/token/v1/views/jobs"),
) -> Path:
    """Write job results to Parquet for post-job analysis.

    Takes AggregationReturn objects from job execution and writes them
    to a structured Parquet dataset for querying.

    Args:
        job: The SimJob that was executed
        results: List of AggregationReturn objects with loss values
        output_dir: Base directory for job views

    Returns:
        Path to the created view directory
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        logger.error("pyarrow not installed. Cannot write Parquet views.")
        raise

    # Create job-specific directory
    job_dir = output_dir / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Extract data for Parquet
    rows = []
    available_count = 0
    failed_count = 0

    logger.info(f"Processing {len(results)} results...")
    for i, result in enumerate(results):
        if not isinstance(result, AggregationReturn):
            logger.warning(f"Skipping non-AggregationReturn result at index {i}: type={type(result).__name__}")
            continue
        logger.debug(f"Processing AggregationReturn {i}: {result.aggregation_id}")

        # Get param_id from corresponding task group
        # Results are ordered same as task groups
        task_groups = list(job.get_task_groups().items())
        if i < len(task_groups):
            param_id, tasks = task_groups[i]
            first_task = tasks[0]
        else:
            logger.warning(f"No task group for result {i}")
            continue

        row = {
            "job_id": job.job_id,
            "param_id": param_id,
            "bundle_ref": job.bundle_ref,
            "entrypoint": first_task.entrypoint,
            "loss": float(result.loss) if result.loss is not None else None,
            "n_replicates": result.n_replicates,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Add parameters
        for key, value in first_task.params.params.items():
            row[f"param_{key}"] = value

        # Track availability
        if result.loss is not None:
            available_count += 1
        else:
            failed_count += 1

        rows.append(row)
        logger.debug(f"Added row for param_id {param_id[:8]}")

    logger.info(f"Collected {len(rows)} rows for Parquet")

    # Write Parquet file
    if not rows:
        logger.warning("No valid results to write to Parquet")
        # Still write manifest even with no data
    else:
        # Convert to Arrow table with explicit schema
        # PyArrow needs column names when constructing from list of dicts
        if rows:
            # Get column names from first row (all rows have same structure)
            column_names = list(rows[0].keys())
            # Create lists for each column
            column_data = {col: [row[col] for row in rows] for col in column_names}
            # Create table from dict of arrays
            table = pa.table(column_data)

        # Write partitioned by param_id prefix (first 2 chars)
        # This helps with query performance
        pq.write_to_dataset(
            table,
            root_path=str(job_dir / "data"),
            partition_cols=None,  # Don't partition for now, can add later
            compression="snappy",
        )

        logger.info(f"Wrote {len(rows)} rows to Parquet at {job_dir}/data")

    # Write manifest
    manifest = {
        "job_id": job.job_id,
        "bundle_ref": job.bundle_ref,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "row_counts": {
            "total": len(results),
            "available": available_count,
            "failed": failed_count,
            "missing": 0,  # We process all results in memory
        },
        "dataset_uri": str(job_dir / "data"),
        "schema_version": "1.0.0",
        "target_spec": _serialize_target_spec(job.target_spec) if job.target_spec else None,
    }

    manifest_path = job_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    logger.info(f"Job view written to {job_dir}")
    logger.info(f"  Total results: {len(results)}")
    logger.info(f"  Available: {available_count}")
    logger.info(f"  Failed: {failed_count}")

    return job_dir


def _serialize_target_spec(spec: Optional[TargetSpec]) -> Optional[Dict[str, Any]]:
    """Serialize TargetSpec to dict for JSON storage."""
    if not spec:
        return None

    return {
        "data": spec.data,
        "loss_function": spec.loss_function,
        "weights": spec.weights,
        "metadata": spec.metadata,
    }


def read_job_view(job_id: str, base_dir: Path = Path("/tmp/modelops/provenance/token/v1/views/jobs")) -> Optional[Any]:
    """Read a job view Parquet dataset.

    Args:
        job_id: Job ID to read
        base_dir: Base directory for job views

    Returns:
        PyArrow Table or None if not found
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:
        logger.error("pyarrow not installed. Cannot read Parquet views.")
        return None

    job_dir = base_dir / job_id / "data"
    if not job_dir.exists():
        logger.warning(f"No job view found at {job_dir}")
        return None

    # Read the Parquet dataset
    table = pq.read_table(str(job_dir))
    logger.info(f"Read {table.num_rows} rows from job view {job_id}")

    return table