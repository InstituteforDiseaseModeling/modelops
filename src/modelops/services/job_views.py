"""Write job results to Parquet views for post-job analysis.

This module provides functionality to write AggregationReturn objects
directly to Parquet files, bypassing the need for filesystem scanning
or Dask worker submission. Supports multiple targets with separate Parquet
files per target.
"""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modelops_contracts import AggregationReturn, SimJob, TargetSpec

logger = logging.getLogger(__name__)


# TODO/FIXME: make output_dir use ProvenanceSchema
def write_job_view(
    job: SimJob,
    results: list[AggregationReturn] | dict[str, list[AggregationReturn]],
    output_dir: Path = Path("/tmp/modelops/provenance/token/v1/views/jobs"),
    prov_store: Any | None = None,
    raw_sim_returns: dict[str, list[Any]] | None = None,
) -> Path:
    """Write job results to Parquet for post-job analysis.

    Takes AggregationReturn objects from job execution and writes them
    to structured Parquet datasets for querying. Supports multiple targets
    with separate Parquet files per target.

    Args:
        job: The SimJob that was executed
        results: Either a list of AggregationReturn objects (single target)
                or a dict mapping target names to lists of AggregationReturn
        output_dir: Base directory for job views
        prov_store: Optional ProvenanceStore for Azure uploads

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

    # Normalize results to dict format
    if isinstance(results, list):
        # Single target or no target - use "default" as key
        results_by_target = {"default": results}
    else:
        results_by_target = results

    # Process each target separately
    target_summaries = {}
    blob_urls = {}
    base_url: str | None = None

    for target_name, target_results in results_by_target.items():
        logger.info(f"Processing target '{target_name}' with {len(target_results)} results...")

        # Extract data for Parquet
        rows = []
        available_count = 0
        failed_count = 0

        # Get task groups once for mapping
        task_groups = list(job.get_task_groups().items())

        for i, result in enumerate(target_results):
            if not isinstance(result, AggregationReturn):
                # Log more details about the error
                error_details = ""
                if hasattr(result, "message"):
                    error_details = f" Error: {result.message}"
                elif isinstance(result, Exception):
                    error_details = f" Error: {result}"
                if hasattr(result, "data") and result.data:
                    error_details += f" Data: {result.data}"
                logger.error(
                    f"Expected AggregationReturn but got {type(result).__name__} at index {i}.{error_details}"
                )
                failed_count += 1
                continue

            # Get param_id from corresponding task group
            if i < len(task_groups):
                param_id, tasks = task_groups[i]
                first_task = tasks[0]
            else:
                logger.warning(f"No task group for result {i}")
                continue

            # Extract seed information from tasks
            seeds = [task.seed for task in tasks]

            row = {
                "job_id": job.job_id,
                "param_id": param_id,
                "bundle_ref": job.bundle_ref,
                "entrypoint": first_task.entrypoint,
                "loss": float(result.loss) if result.loss is not None else None,
                "n_replicates": result.n_replicates,
                "seeds": seeds,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            # Add parameters as columns
            for key, value in first_task.params.params.items():
                row[f"param_{key}"] = value

            # Track availability
            if result.loss is not None:
                available_count += 1
            else:
                failed_count += 1

            rows.append(row)

        # Write Parquet file for this target
        if rows:
            # Create target-specific directory
            target_dir = job_dir / "targets" / target_name
            target_dir.mkdir(parents=True, exist_ok=True)

            # Convert to Arrow table
            column_names = list(rows[0].keys())
            column_data = {col: [row[col] for row in rows] for col in column_names}
            table = pa.table(column_data)

            # Write Parquet
            parquet_path = target_dir / "data.parquet"
            pq.write_table(table, parquet_path, compression="snappy")

            logger.info(f"Wrote {len(rows)} rows to {parquet_path}")

            # Calculate mean loss for summary
            losses = [r["loss"] for r in rows if r["loss"] is not None]
            mean_loss = sum(losses) / len(losses) if losses else None
        else:
            logger.warning(f"No valid results for target '{target_name}'")
            mean_loss = None

        # Store summary for manifest
        target_summaries[target_name] = {
            "rows": len(rows),
            "available": available_count,
            "failed": failed_count,
            "mean_loss": mean_loss,
        }

    # Write manifest
    manifest = {
        "job_id": job.job_id,
        "bundle_ref": job.bundle_ref,
        "created_at": datetime.now(UTC).isoformat(),
        "targets": target_summaries,
        "dataset_uri": str(job_dir),
        "schema_version": "1.0.0",
        "target_spec": _serialize_target_spec(job.target_spec) if job.target_spec else None,
    }

    # Prepare Azure blob URLs if ProvenanceStore is available
    if prov_store and hasattr(prov_store, "supports_remote_uploads") and prov_store.supports_remote_uploads():
        try:
            # Extract storage account name from connection string
            backend_info = (
                prov_store.get_remote_backend_info()
                if hasattr(prov_store, "get_remote_backend_info")
                else None
            )
            connection_string = backend_info.get("connection_string") if backend_info else None
            account_name = _extract_account_name(connection_string or "")
            if account_name:
                # Build blob URLs that will be valid after upload
                remote_prefix = f"views/jobs/{job.job_id}"
                base_url = f"https://{account_name}.blob.core.windows.net/results/{remote_prefix}"
                manifest["blob_url"] = f"{base_url}/manifest.json"

                # Add blob URLs for each target
                for target_name in target_summaries:
                    target_url = f"{base_url}/targets/{target_name}/data.parquet"
                    target_summaries[target_name]["blob_url"] = target_url
                    blob_urls[target_name] = target_url
        except Exception as e:
            logger.error(f"Failed to prepare Azure URLs: {e}")

    # Write manifest (with blob URLs if available)
    manifest_path = job_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    # Write model outputs if available
    if raw_sim_returns:
        try:
            model_outputs_dir = job_dir / "model_outputs"
            model_outputs_dir.mkdir(parents=True, exist_ok=True)
            _write_model_outputs(model_outputs_dir, raw_sim_returns, job)
            logger.info("Wrote model outputs to Parquet")
        except Exception as e:
            logger.error(f"Failed to write model outputs: {e}")
            # Continue without model outputs

    # Upload to Azure if ProvenanceStore is available
    if prov_store and hasattr(prov_store, "supports_remote_uploads") and prov_store.supports_remote_uploads():
        try:
            logger.info("Uploading job views to Azure...")

            # Upload the entire job directory (including manifest)
            remote_prefix = f"views/jobs/{job.job_id}"
            prov_store.upload_directory(job_dir, remote_prefix)

            if blob_urls:
                logger.info(f"Job views uploaded to: {base_url}")
                for target_name, url in blob_urls.items():
                    logger.info(f"  Target '{target_name}': {url}")
        except Exception as e:
            logger.error(f"Failed to upload to Azure: {e}")
            # Continue without upload

    logger.info(f"Job view written to {job_dir}")
    logger.info(f"  Total targets: {len(target_summaries)}")
    for target_name, summary in target_summaries.items():
        logger.info(
            f"  Target '{target_name}': {summary['rows']} rows, mean loss: {summary['mean_loss']:.2f}"
            if summary["mean_loss"]
            else f"  Target '{target_name}': {summary['rows']} rows"
        )

    return job_dir


def write_replicates_view(
    job: SimJob,
    results_by_target: dict[str, list[AggregationReturn]],
    output_dir: Path = Path("/tmp/modelops/provenance/token/v1/views/jobs"),
    prov_store: Any | None = None,
) -> Path | None:
    """Write per-replicate results to separate Parquet file.

    Extracts per-replicate losses from AggregationReturn.diagnostics and
    writes them to a replicates.parquet file for detailed analysis.

    Args:
        job: The SimJob that was executed
        results_by_target: Dict mapping target names to lists of AggregationReturn
        output_dir: Base directory for job views
        prov_store: Optional ProvenanceStore for Azure uploads

    Returns:
        Path to replicates file, or None if no per-replicate data available
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        logger.error("pyarrow not installed. Cannot write replicates view.")
        return None

    # Get task groups for seed information
    task_groups = list(job.get_task_groups().items())

    # Collect per-replicate rows
    rows = []
    has_per_replicate_data = False

    for target_name, target_results in results_by_target.items():
        for i, result in enumerate(target_results):
            if not isinstance(result, AggregationReturn):
                continue

            # Check if diagnostics contains per-replicate losses
            per_rep_losses = result.diagnostics.get("per_replicate_losses")
            if not per_rep_losses:
                continue

            has_per_replicate_data = True

            # Get corresponding task group
            if i >= len(task_groups):
                logger.warning(f"No task group for result {i}")
                continue

            param_id, tasks = task_groups[i]

            # Create one row per replicate
            for rep_idx, (task, loss) in enumerate(zip(tasks, per_rep_losses)):
                rows.append({
                    "job_id": job.job_id,
                    "param_id": param_id,
                    "target_name": target_name,
                    "replicate_idx": rep_idx,
                    "seed": task.seed,
                    "loss": float(loss),
                })

    if not has_per_replicate_data:
        logger.info("No per-replicate loss data found in diagnostics")
        return None

    # Create job directory
    job_dir = output_dir / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Write replicates Parquet
    table = pa.table({col: [row[col] for row in rows] for col in rows[0].keys()})
    replicates_path = job_dir / "replicates.parquet"
    pq.write_table(table, replicates_path, compression="snappy")

    logger.info(f"Wrote {len(rows)} per-replicate results to {replicates_path}")

    # Upload to Azure if available
    if prov_store and hasattr(prov_store, "_azure_backend") and prov_store._azure_backend:
        try:
            logger.info("Uploading replicates view to Azure...")
            remote_prefix = f"views/jobs/{job.job_id}"
            # Upload just the replicates file
            from pathlib import Path as P
            temp_dir = P(replicates_path).parent
            prov_store._azure_backend.upload_file(
                str(replicates_path),
                f"{remote_prefix}/replicates.parquet"
            )
            logger.info(f"Replicates view uploaded to Azure: {remote_prefix}/replicates.parquet")
        except Exception as e:
            logger.error(f"Failed to upload replicates view to Azure: {e}")

    return replicates_path


def _write_model_outputs(
    output_dir: Path,
    raw_sim_returns_by_param: dict[str, list[Any]],
    job: SimJob
) -> None:
    """Write raw model outputs to Parquet files.

    Creates one Parquet file per output name (incidence, prevalence, etc.)
    with all replicates concatenated. Adds param_id, seed, replicate_idx columns.

    Args:
        output_dir: Directory to write model_outputs/*.parquet files
        raw_sim_returns_by_param: Dict mapping param_id to list of SimReturns
        job: The SimJob being executed
    """
    try:
        import polars as pl
    except ImportError:
        logger.error("polars not installed. Cannot write model outputs.")
        raise

    # Determine what outputs exist by looking at first SimReturn
    if not raw_sim_returns_by_param:
        logger.warning("No raw simulation returns to write")
        return

    first_param_id = next(iter(raw_sim_returns_by_param.keys()))
    first_sim_returns = raw_sim_returns_by_param[first_param_id]

    if not first_sim_returns:
        logger.warning(f"No SimReturns for param_id {first_param_id}")
        return

    first_sim_return = first_sim_returns[0]
    output_names = list(first_sim_return.outputs.keys())

    logger.info(f"Collecting {len(output_names)} model outputs: {output_names}")

    # For each output name, concatenate across all param_ids and replicates
    for output_name in output_names:
        all_dfs = []

        for param_id, sim_returns in raw_sim_returns_by_param.items():
            for replicate_idx, sim_return in enumerate(sim_returns):
                if output_name not in sim_return.outputs:
                    logger.warning(
                        f"Missing output {output_name} for param {param_id}, replicate {replicate_idx}"
                    )
                    continue

                # Read DataFrame from Arrow IPC (stored in TableArtifact.inline)
                artifact = sim_return.outputs[output_name]
                if not artifact.inline:
                    logger.warning(
                        f"No inline data for {output_name}, param {param_id}, replicate {replicate_idx}"
                    )
                    continue

                try:
                    df = pl.read_ipc(artifact.inline)

                    # Add metadata columns for filtering/grouping
                    df = df.with_columns([
                        pl.lit(param_id).alias("param_id"),
                        pl.lit(replicate_idx).alias("replicate_idx"),
                    ])

                    # Note: seed column should already exist from extract_outputs()
                    all_dfs.append(df)
                except Exception as e:
                    logger.error(
                        f"Failed to read {output_name} for param {param_id}, "
                        f"replicate {replicate_idx}: {e}"
                    )
                    continue

        if all_dfs:
            try:
                # Concatenate all replicates vertically
                concatenated = pl.concat(all_dfs, how="vertical")

                # Write to Parquet with compression
                output_path = output_dir / f"{output_name}.parquet"
                concatenated.write_parquet(
                    output_path,
                    compression="zstd",
                    compression_level=3
                )

                file_size = output_path.stat().st_size
                logger.info(
                    f"Wrote {output_name}.parquet: {len(concatenated):,} rows, "
                    f"{file_size:,} bytes"
                )
            except Exception as e:
                logger.error(f"Failed to write {output_name}.parquet: {e}")
        else:
            logger.warning(f"No data collected for output {output_name}")


def _serialize_target_spec(spec: TargetSpec | None) -> dict[str, Any] | None:
    """Serialize TargetSpec to dict for JSON storage."""
    if not spec:
        return None

    return {
        "data": spec.data,
        "loss_function": spec.loss_function,
        "weights": spec.weights,
        "metadata": spec.metadata,
    }


def _extract_account_name(connection_string: str) -> str | None:
    """Extract storage account name from Azure connection string.

    Connection string format:
    DefaultEndpointsProtocol=https;AccountName=XXX;AccountKey=YYY;EndpointSuffix=core.windows.net
    """
    match = re.search(r"AccountName=([^;]+)", connection_string)
    if match:
        return match.group(1)
    return None


def read_job_view(
    job_id: str,
    target_name: str = "default",
    base_dir: Path = Path("/tmp/modelops/provenance/token/v1/views/jobs"),
) -> Any | None:
    """Read a job view Parquet dataset for a specific target.

    Args:
        job_id: Job ID to read
        target_name: Target name (default: "default")
        base_dir: Base directory for job views

    Returns:
        PyArrow Table or None if not found
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:
        logger.error("pyarrow not installed. Cannot read Parquet views.")
        return None

    # Check for target-specific path first
    target_path = base_dir / job_id / "targets" / target_name / "data.parquet"
    if target_path.exists():
        table = pq.read_table(str(target_path))
        logger.info(f"Read {table.num_rows} rows from job view {job_id} target '{target_name}'")
        return table

    # Fallback to old single-file location
    legacy_path = base_dir / job_id / "data"
    if legacy_path.exists():
        table = pq.read_table(str(legacy_path))
        logger.info(f"Read {table.num_rows} rows from legacy job view {job_id}")
        return table

    logger.warning(f"No job view found for {job_id} target '{target_name}'")
    return None
