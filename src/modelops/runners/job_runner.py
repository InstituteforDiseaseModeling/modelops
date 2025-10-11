#!/usr/bin/env python
"""Universal job runner that processes jobs from blob storage.

This script runs inside a Kubernetes Job pod, downloads the job
specification from blob storage, and executes it based on job type.
Handles both SimJob (batch simulation) and CalibrationJob (adaptive).
"""

import json
import logging
import os
import sys
from typing import Dict, Any

from azure.storage.blob import BlobServiceClient
from dask.distributed import Client

from modelops_contracts import (
    Job,
    SimJob,
    CalibrationJob,
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


def load_job_from_blob() -> Job:
    """Download and deserialize job from blob storage.

    Returns:
        Deserialized Job object (SimJob or CalibrationJob)

    Raises:
        Exception: If download or deserialization fails
    """
    # Get configuration from environment
    blob_key = os.environ["JOB_BLOB_KEY"]
    conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]

    logger.info(f"Downloading job from blob: {blob_key}")

    # Download from blob storage
    blob_service = BlobServiceClient.from_connection_string(conn_str)
    container_client = blob_service.get_container_client("jobs")
    blob_client = container_client.get_blob_client(blob_key)

    job_json = blob_client.download_blob().readall().decode("utf-8")
    job_data = json.loads(job_json)

    # Deserialize based on job_type
    return deserialize_job(job_data)


def deserialize_job(data: Dict[str, Any]) -> Job:
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
    """Execute a simulation job.

    Processes all tasks using DaskSimulationService.

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
    logger.info(f"Processing {len(task_groups)} parameter sets with replicates")

    # Check if we have targets for aggregation
    target_entrypoint = None
    if job.target_spec and job.target_spec.data.get("target_entrypoints"):
        # Use first target for now (could extend to multiple targets)
        target_entrypoint = job.target_spec.data["target_entrypoints"][0]
        logger.info(f"Will aggregate replicates using target: {target_entrypoint}")

    # Submit replicate sets with aggregation if targets are present
    from modelops_contracts import ReplicateSet

    futures = []
    for param_id, replicate_tasks in task_groups.items():
        if len(replicate_tasks) > 1:
            # Multiple replicates - use ReplicateSet for grouped submission
            base_task = replicate_tasks[0]
            replicate_set = ReplicateSet(
                base_task=base_task,
                n_replicates=len(replicate_tasks),
                seed_offset=0  # Seeds already set in tasks
            )
            # Submit with or without target-based aggregation
            future = sim_service.submit_replicate_set(replicate_set, target_entrypoint)
            futures.append(future)
            if target_entrypoint:
                logger.info(f"  Submitted {len(replicate_tasks)} replicates for param {param_id[:8]} with target aggregation")
            else:
                logger.info(f"  Submitted {len(replicate_tasks)} replicates for param {param_id[:8]} as group")
        else:
            # Single task - no grouping needed
            task = replicate_tasks[0]
            future = sim_service.submit(task)
            futures.append(future)
            logger.info(f"  Submitted single task for param {param_id[:8]}")

    # Gather results
    results = sim_service.gather(futures)
    logger.info(f"Job complete: {len(results)} results")

    # If we used aggregation, results are already AggregationReturn objects with loss
    if target_entrypoint:
        logger.info("Target evaluation completed during aggregation")
        # Note: Storage happens on worker side in isolated_warm.py
        for i, result in enumerate(results[:3]):  # Log first 3
            if hasattr(result, 'loss'):
                logger.info(f"  Param set {i} loss: {result.loss}")
    elif job.target_spec:
        # Fallback: evaluate targets on client side if not done on worker
        logger.info("Evaluating targets on client side...")
        try:
            trial_results = evaluate_results(results, job.target_spec)
            logger.info(f"Target evaluation complete: {len(trial_results)} trials evaluated")
            for i, tr in enumerate(trial_results[:3]):
                if hasattr(tr, 'loss'):
                    logger.info(f"  Trial {i} loss: {tr.loss}")
        except NotImplementedError:
            logger.warning("Target evaluation not yet implemented")
        except Exception as e:
            logger.error(f"Target evaluation failed: {e}")

    # TODO: Upload results to blob storage
    # For now, just log success
    for i, result in enumerate(results[:3]):  # Log first 3
        if hasattr(result, "outputs"):
            logger.info(f"  Task {i}: {list(result.outputs.keys())}")

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


def create_adaptive_algorithm(
    algorithm: str, config: Dict[str, Any]
) -> AdaptiveAlgorithm:
    """Create adaptive algorithm instance.

    Args:
        algorithm: Algorithm name ("optuna", etc.)
        config: Algorithm configuration

    Returns:
        AdaptiveAlgorithm implementation

    Raises:
        ValueError: If algorithm is unknown
    """
    # This would import the actual algorithm implementations
    # For now, raise NotImplementedError
    raise NotImplementedError(f"Algorithm {algorithm} not yet implemented")


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


def check_convergence(trial_results, criteria: Dict[str, float]) -> bool:
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
        scheduler_addr = os.environ.get(
            "DASK_SCHEDULER_ADDRESS", "tcp://dask-scheduler:8786"
        )
        logger.info(f"Connecting to Dask scheduler at {scheduler_addr}")

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
