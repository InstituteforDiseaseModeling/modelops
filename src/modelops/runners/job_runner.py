#!/usr/bin/env python
"""Universal job runner that processes jobs from blob storage.

This script runs inside a Kubernetes Job pod, downloads the job
specification from blob storage, and executes it based on job type.
Handles both SimJob (batch simulation) and CalibrationJob (adaptive).
"""

import json
import logging
import math
import os
import sys
from typing import Any

from azure.storage.blob import BlobServiceClient
from dask.distributed import Client
from modelops_contracts import (
    CalibrationJob,
    Job,
    SimJob,
    SimTask,
    TargetSpec,
    UniqueParameterSet,
)
from modelops_contracts.adaptive import AdaptiveAlgorithm

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class MissingEnvironmentVariableError(Exception):
    """Raised when a required environment variable is missing."""

    pass


def _get_required_env(name: str, description: str) -> str:
    """Get a required environment variable with helpful error message.

    Args:
        name: Environment variable name
        description: Human-readable description of what this variable is for

    Returns:
        Environment variable value

    Raises:
        MissingEnvironmentVariableError: If variable is not set
    """
    value = os.environ.get(name)
    if not value:
        raise MissingEnvironmentVariableError(
            f"Required environment variable {name} is not set.\n"
            f"Purpose: {description}\n"
            f"This variable should be set by the Kubernetes Job configuration."
        )
    return value


def load_job_from_blob() -> Job:
    """Download and deserialize job from blob storage.

    Returns:
        Deserialized Job object (SimJob or CalibrationJob)

    Raises:
        MissingEnvironmentVariableError: If required environment variables are missing
        Exception: If download or deserialization fails
    """
    # Get configuration from environment with validation
    blob_key = _get_required_env("JOB_BLOB_KEY", "Blob storage key for the job specification")
    conn_str = _get_required_env(
        "AZURE_STORAGE_CONNECTION_STRING",
        "Azure Storage connection string for accessing job blobs",
    )

    logger.info(f"Downloading job from blob: {blob_key}")

    # Download from blob storage
    blob_service = BlobServiceClient.from_connection_string(conn_str)
    container_client = blob_service.get_container_client("jobs")
    blob_client = container_client.get_blob_client(blob_key)

    job_json = blob_client.download_blob().readall().decode("utf-8")
    job_data = json.loads(job_json)

    # Deserialize based on job_type
    return deserialize_job(job_data)


def deserialize_job(data: dict[str, Any]) -> Job:
    """Deserialize job from JSON data.

    Args:
        data: JSON data dictionary

    Returns:
        Job object (SimJob or CalibrationJob)

    Raises:
        ValueError: If job_type is unknown
    """
    job_type = data["job_type"]

    match job_type:
        case "simulation":
            # Reconstruct SimJob - now with flat task list
            tasks = []
            # Support both old format (batches) and new format (flat tasks)
            if "batches" in data:
                # Old format with batches - flatten into tasks
                for batch_data in data["batches"]:
                    for task_data in batch_data["tasks"]:
                        task = SimTask(
                            bundle_ref=task_data["bundle_ref"],
                            entrypoint=task_data["entrypoint"],
                            params=UniqueParameterSet(
                                param_id=task_data["params"]["param_id"],
                                params=task_data["params"]["values"],
                            ),
                            seed=task_data["seed"],
                            outputs=task_data.get("outputs"),
                        )
                        tasks.append(task)
            elif "tasks" in data:
                # New format with flat task list
                for task_data in data["tasks"]:
                    task = SimTask(
                        bundle_ref=task_data["bundle_ref"],
                        entrypoint=task_data["entrypoint"],
                        params=UniqueParameterSet(
                            param_id=task_data["params"]["param_id"],
                            params=task_data["params"]["values"],
                        ),
                        seed=task_data["seed"],
                        outputs=task_data.get("outputs"),
                    )
                    tasks.append(task)

            # Deserialize target_spec if present (same as CalibrationJob)
            target_spec = None
            if "target_spec" in data:
                target_spec = TargetSpec(
                    data=data["target_spec"]["data"],
                    loss_function=data["target_spec"]["loss_function"],
                    weights=data["target_spec"].get("weights"),
                    metadata=data["target_spec"].get("metadata", {}),
                )

            return SimJob(
                job_id=data["job_id"],
                bundle_ref=data["bundle_ref"],
                tasks=tasks,
                priority=data.get("priority", 0),
                metadata=data.get("metadata", {}),
                target_spec=target_spec,
            )

        case "calibration":
            # Reconstruct CalibrationJob
            target_spec = TargetSpec(
                data=data["target_spec"]["data"],
                loss_function=data["target_spec"]["loss_function"],
                weights=data["target_spec"].get("weights"),
                metadata=data["target_spec"].get("metadata", {}),
            )

            return CalibrationJob(
                job_id=data["job_id"],
                bundle_ref=data["bundle_ref"],
                algorithm=data["algorithm"],
                target_spec=target_spec,
                max_iterations=data["max_iterations"],
                convergence_criteria=data.get("convergence_criteria", {}),
                algorithm_config=data.get("algorithm_config", {}),
            )

        case _:
            raise ValueError(f"Unknown job type: {job_type}")


def run_simulation_job(job: SimJob, client: Client) -> None:
    """Execute a simulation job using batched submission.

    Submits parameter sets in configurable batches to prevent Dask distributed
    memory accumulation and scheduler overload. Each batch's results are gathered
    and written before moving to the next, so Dask can free worker memory
    between batches.

    Without batching, all sim futures are held for the entire job duration,
    preventing Dask from freeing any SimReturn from distributed memory. When
    workers OOM-kill, completed results stored on those workers are lost and
    must be recomputed — causing progress to go backwards.

    Configure batch size via MODELOPS_JOB_BATCH_SIZE environment variable.

    Args:
        job: SimJob to execute
        client: Dask client connected to cluster
    """
    from modelops.services.dask_simulation import DaskSimulationService

    logger.info(f"Running simulation job {job.job_id}")
    logger.info(f"Total tasks: {len(job.tasks)}")

    # Create simulation service
    sim_service = DaskSimulationService(client)

    # Group tasks by parameter ID for replicate handling
    task_groups = job.get_task_groups()
    total_params = len(task_groups)
    logger.info(f"Processing {total_params} parameter sets with replicates")

    # Check if we have targets for aggregation
    target_entrypoints = []
    if job.target_spec and job.target_spec.data.get("target_entrypoints"):
        target_entrypoints = job.target_spec.data["target_entrypoints"]
        logger.info(f"Will evaluate {len(target_entrypoints)} targets: {target_entrypoints}")

    # Configurable batch size — tune based on model output size and worker memory.
    # Smaller batches reduce peak distributed memory and blast radius from worker
    # death, but add small gaps between batches for gathering.
    batch_size = int(os.environ.get("MODELOPS_JOB_BATCH_SIZE", "200"))
    task_groups_list = list(task_groups.items())
    n_batches = math.ceil(total_params / batch_size)
    logger.info(
        f"Batch size: {batch_size} (set MODELOPS_JOB_BATCH_SIZE to change), "
        f"{n_batches} batch(es)"
    )

    from modelops_contracts import ReplicateSet

    # Accumulate results across batches (small — just loss + diagnostics per param)
    all_results_by_target: dict[str, list] = {}
    all_default_results: list = []
    # Accumulate model outputs across batches for Parquet writing.
    # Each SimReturn output is small (Arrow IPC bytes). For models with very large
    # outputs, this could be replaced with per-batch Parquet file writing.
    all_raw_sim_returns: dict[str, list] = {}
    completed_params = 0
    oom_kill_count = 0

    for batch_idx in range(n_batches):
        batch_start = batch_idx * batch_size
        batch_end = min(batch_start + batch_size, total_params)
        batch = task_groups_list[batch_start:batch_end]
        batch_len = len(batch)

        logger.info(
            f"--- Batch {batch_idx + 1}/{n_batches}: "
            f"params {batch_start + 1}-{batch_end} of {total_params} ---"
        )

        # Submit this batch's simulations and aggregations
        sim_futures_batch: dict[str, list] = {}
        agg_futures_batch: list[tuple[str, str | None, Any]] = []

        for param_id, replicate_tasks in batch:
            base_task = replicate_tasks[0]
            replicate_set = ReplicateSet(
                base_task=base_task,
                n_replicates=len(replicate_tasks),
                seed_offset=0,
            )

            sim_futures = sim_service.submit_replicates(replicate_set)
            sim_futures_batch[param_id] = sim_futures
            logger.info(
                f"  Submitted {len(replicate_tasks)} replicate(s) for param {param_id[:8]}"
            )

            if target_entrypoints:
                for target in target_entrypoints:
                    agg_future = sim_service.submit_aggregation(
                        sim_futures,
                        target,
                        bundle_ref=base_task.bundle_ref,
                        param_id=param_id,
                    )
                    agg_futures_batch.append((param_id, target, agg_future))
                    logger.info(f"    Evaluating target {target} on param {param_id[:8]}")
            else:
                def gather_sims(*sims):
                    return list(sims)

                from modelops.services.dask_simulation import DaskFutureAdapter

                gathered_future = sim_service.client.submit(
                    gather_sims,
                    *[f.wrapped for f in sim_futures],
                    pure=False,
                )
                agg_futures_batch.append(
                    (param_id, None, DaskFutureAdapter(gathered_future))
                )

        # Gather aggregation results for this batch
        batch_results = sim_service.gather([f for *_, f in agg_futures_batch])

        # Check for OOM errors in results
        for result in batch_results:
            if isinstance(result, Exception):
                error_str = str(result)
                if "OOM" in error_str or "exit code 137" in error_str:
                    oom_kill_count += 1

        # Accumulate aggregation results by target
        for (param_id, target, _), result in zip(agg_futures_batch, batch_results):
            if target:
                target_name = target.split("/")[-1] if "/" in target else target
                all_results_by_target.setdefault(target_name, []).append(result)
            else:
                if isinstance(result, list):
                    all_default_results.extend(result)
                else:
                    all_default_results.append(result)

        # Gather raw SimReturns for model outputs (instant — sims already completed)
        if target_entrypoints:
            for param_id, sim_futs in sim_futures_batch.items():
                sim_returns = sim_service.gather(sim_futs)
                all_raw_sim_returns[param_id] = sim_returns

        completed_params += batch_len
        logger.info(
            f"--- Batch {batch_idx + 1}/{n_batches} complete: "
            f"{completed_params}/{total_params} params gathered ---"
        )

        # Release batch references — Dask can now free all SimReturns and
        # AggregationReturns from distributed worker memory for this batch.
        sim_futures_batch.clear()
        agg_futures_batch.clear()

    # Report OOM detections
    if oom_kill_count > 0:
        logger.warning(
            f"OOM kills detected: {oom_kill_count} task(s) failed with out-of-memory errors. "
            f"Current MODELOPS_JOB_BATCH_SIZE={batch_size}. "
            f"Consider reducing to MODELOPS_JOB_BATCH_SIZE={max(10, batch_size // 2)} "
            f"or increasing worker memory."
        )

    logger.info(f"All batches complete: {completed_params} parameter sets processed")

    # Log results summary
    if target_entrypoints:
        for target in target_entrypoints:
            target_name = target.split("/")[-1] if "/" in target else target
            target_results = all_results_by_target.get(target_name, [])
            logger.info(f"Results for target {target_name}: {len(target_results)}")
            for i, result in enumerate(target_results[:3]):
                if hasattr(result, "loss"):
                    logger.info(f"  Param set {i} loss for {target_name}: {result.loss}")
    else:
        logger.info(f"Collected {len(all_default_results)} raw simulation results (no targets)")

    # Write Parquet views for post-job analysis
    if target_entrypoints and all_results_by_target:
        try:
            from pathlib import Path

            from modelops.services.job_views import write_job_view, write_replicates_view
            from modelops.services.provenance_store import ProvenanceStore

            logger.info("Writing job results to Parquet views...")

            prov_store = None
            conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
            if conn_str:
                try:
                    prov_store = ProvenanceStore(
                        storage_dir=Path("/tmp/modelops/provenance"),
                        azure_backend={
                            "container": "results",
                            "connection_string": conn_str,
                        },
                    )
                    logger.info("ProvenanceStore initialized with Azure backend")
                except Exception as e:
                    logger.warning(f"Could not initialize ProvenanceStore with Azure: {e}")
                    prov_store = None

            view_path = write_job_view(
                job,
                all_results_by_target,
                prov_store=prov_store,
                raw_sim_returns=all_raw_sim_returns if all_raw_sim_returns else None,
            )
            logger.info(f"Job view written to: {view_path}")

            try:
                replicates_path = write_replicates_view(
                    job, all_results_by_target, prov_store=prov_store
                )
                if replicates_path:
                    logger.info(f"Per-replicate view written to: {replicates_path}")
            except Exception as e:
                logger.warning(f"Could not write per-replicate view: {e}")
        except ImportError as e:
            logger.warning(f"Could not write job views (missing dependency): {e}")
        except Exception as e:
            logger.error(f"Failed to write job views: {e}")
    elif not target_entrypoints:
        logger.warning("Skipping view generation: no targets specified")

    if not target_entrypoints and job.target_spec:
        logger.info("Evaluating targets on client side...")
        try:
            all_results = all_default_results
            trial_results = evaluate_results(all_results, job.target_spec)
            logger.info(f"Target evaluation complete: {len(trial_results)} trials evaluated")
            for i, tr in enumerate(trial_results[:3]):
                if hasattr(tr, "loss"):
                    logger.info(f"  Trial {i} loss: {tr.loss}")
        except NotImplementedError:
            logger.warning("Target evaluation not yet implemented")
        except Exception as e:
            logger.error(f"Target evaluation failed: {e}")

    logger.info(f"Job {job.job_id} completed successfully")


def run_calibration_job(job: CalibrationJob, client: Client) -> None:
    """Execute a calibration job.

    Runs ask/tell loop using adaptive algorithm and simulation service.

    Args:
        job: CalibrationJob to execute
        client: Dask client connected to cluster
    """
    from modelops.services.dask_simulation import DaskSimulationService

    logger.info(f"Running calibration job {job.job_id}")
    logger.info(f"Algorithm: {job.algorithm}")
    logger.info(f"Max iterations: {job.max_iterations}")

    # Check if we should use the new calibration wire
    try:
        from pathlib import Path

        from modelops_calabaria.calibration.wire import calibration_wire

        from modelops.services.provenance_store import ProvenanceStore

        # Create simulation service
        sim_service = DaskSimulationService(client)

        # Initialize ProvenanceStore with Azure backend (same pattern as simulation jobs)
        prov_store = None
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn_str:
            try:
                prov_store = ProvenanceStore(
                    storage_dir=Path("/tmp/modelops/provenance"),
                    azure_backend={
                        "container": "results",
                        "connection_string": conn_str,
                    },
                )
                logger.info(
                    "ProvenanceStore initialized with Azure backend for calibration results"
                )
            except Exception as e:
                logger.warning(f"Could not initialize ProvenanceStore with Azure: {e}")
                prov_store = None

        # Use the calibration wire function with ProvenanceStore
        calibration_wire(job, sim_service, prov_store=prov_store)
        return
    except ImportError:
        logger.warning(
            "modelops-calabaria calibration module not available. Using basic implementation."
        )

    # Fallback to basic implementation if calibration wire not available
    # Create simulation service
    sim_service = DaskSimulationService(client)

    # Initialize algorithm based on type
    algo = create_adaptive_algorithm(job.algorithm, job.algorithm_config)

    # Run ask/tell loop
    iteration = 0
    while not algo.finished() and iteration < job.max_iterations:
        iteration += 1
        logger.info(f"Iteration {iteration}/{job.max_iterations}")

        # Ask for parameters
        param_sets = algo.ask(n=16)  # Batch size could be configurable
        if not param_sets:
            logger.info("No more parameters to evaluate")
            break

        # Submit simulations
        futures = []
        for params in param_sets:
            # Create task for these parameters
            task = SimTask(
                bundle_ref=job.bundle_ref,
                entrypoint="models.main/baseline",  # Should be in job config
                params=params,
                seed=iteration * 1000 + len(futures),  # Simple seed generation
            )
            future = sim_service.submit(task)
            futures.append(future)

        # Gather results
        sim_results = sim_service.gather(futures)

        # Evaluate against targets
        trial_results = evaluate_results(sim_results, job.target_spec)

        # Tell algorithm
        algo.tell(trial_results)

        # Check convergence
        if check_convergence(trial_results, job.convergence_criteria):
            logger.info("Convergence criteria met")
            break

    logger.info(f"Calibration job {job.job_id} completed after {iteration} iterations")


def create_adaptive_algorithm(algorithm: str, config: dict[str, Any]) -> AdaptiveAlgorithm:
    """Create adaptive algorithm instance.

    Args:
        algorithm: Algorithm name ("optuna", etc.)
        config: Algorithm configuration

    Returns:
        AdaptiveAlgorithm implementation

    Raises:
        ValueError: If algorithm is unknown
    """
    # Import calibration module from modelops-calabaria
    try:
        from modelops_calabaria.calibration import create_algorithm_adapter
        from modelops_calabaria.calibration.factory import parse_parameter_specs
    except ImportError as e:
        raise ImportError(
            "modelops-calabaria not installed. Please install it to use calibration features."
        ) from e

    # Parse parameter specs if provided
    parameter_specs = {}
    if "parameter_specs" in config:
        parameter_specs = parse_parameter_specs(config["parameter_specs"])

    # Create and return adapter
    return create_algorithm_adapter(
        algorithm_type=algorithm,
        parameter_specs=parameter_specs,
        config=config,
    )


def evaluate_results(sim_results, target_spec: TargetSpec):
    """Evaluate simulation results against targets.

    Args:
        sim_results: List of SimReturn objects
        target_spec: Target specification

    Returns:
        List of TrialResult objects
    """
    # This would implement actual evaluation logic
    # For now, raise NotImplementedError
    raise NotImplementedError("Result evaluation not yet implemented")


def check_convergence(trial_results, criteria: dict[str, float]) -> bool:
    """Check if convergence criteria are met.

    Args:
        trial_results: Latest trial results
        criteria: Convergence criteria

    Returns:
        True if converged
    """
    # Simple implementation - would be more sophisticated
    if not criteria:
        return False

    # Check if loss is below threshold
    if "max_loss" in criteria:
        losses = [r.loss for r in trial_results if r.status == "COMPLETED"]
        if losses and min(losses) < criteria["max_loss"]:
            return True

    return False


def main():
    """Main entry point for job runner."""
    try:
        # Load job from blob
        job = load_job_from_blob()
        logger.info(f"Loaded {job.job_type} job: {job.job_id}")

        # Connect to Dask scheduler
        scheduler_addr = os.environ.get("DASK_SCHEDULER_ADDRESS", "tcp://dask-scheduler:8786")
        logger.info(f"Connecting to Dask scheduler at {scheduler_addr}")

        import dask
        dask.config.set({"distributed.scheduler.worker-saturation": 1.0})
        client = Client(scheduler_addr)
        logger.info(
            f"Connected to Dask cluster with {len(client.scheduler_info()['workers'])} workers"
        )

        # Dispatch based on job type
        match job:
            case SimJob():
                run_simulation_job(job, client)
            case CalibrationJob():
                run_calibration_job(job, client)
            case _:
                raise ValueError(f"Unknown job type: {type(job).__name__}")

        logger.info("Job execution completed successfully")

    except Exception as e:
        logger.error(f"Job execution failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
