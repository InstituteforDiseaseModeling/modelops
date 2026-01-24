"""Dask-based simulation service implementation.

IMPORTANT: Aggregation Deadlock Prevention
===========================================
This module implements critical deadlock prevention for aggregation tasks that depend
on large numbers of simulation tasks (e.g., 200 replicates per parameter set).

The Deadlock Pattern:
1. Each aggregation task depends on 200 simulation futures
2. With limited worker threads (e.g., 8 threads), aggregation tasks waiting for
   dependencies can consume all available threads
3. This prevents simulation tasks from running, creating a circular dependency

Solution Implemented (Oct 2025):
1. Direct dependency passing: Aggregation tasks receive simulation results as *args
   instead of calling gather() inside workers (commit d2d5f8)
2. Resource constraints: Aggregation tasks use resources={'aggregation': 1} to run
   only on workers configured with aggregation resources
3. Increased worker processes: Scale from 2 to 4 processes per pod for more threads

Without these measures, jobs freeze at 18/20 aggregations with 3998/4000 simulations
completed - a consistent pattern indicating thread starvation.
"""

import logging

from dask.distributed import Client, get_worker, wait
from dask.distributed import Future as DaskFuture
from modelops_contracts import SimReturn, SimTask
from modelops_contracts.ports import Future, SimulationService
from modelops_contracts.simulation import (
    AggregationReturn,
    AggregationTask,
    ReplicateSet,
)

from ..worker.config import RuntimeConfig
from ..worker.plugin import ModelOpsWorkerPlugin

logger = logging.getLogger(__name__)


class TaskKeys:
    """Dask task key generation following hyphenated convention.

    Dask groups tasks by the substring before the first hyphen in the key.
    For example: 'sim-abc123-4' groups as 'sim', 'agg-def456' groups as 'agg'.
    Using underscores causes each task to be its own group in the dashboard,
    messing up the colors in task streams.

    IMPORTANT: Do NOT truncate param_id to avoid key collisions!
    With only 8 characters, collisions are likely with many parameter sets.
    """

    @staticmethod
    def sim_key(param_id: str, replicate_idx: int) -> str:
        """Generate simulation task key: sim-{param_id}-{idx}"""
        # Use full param_id to avoid collisions
        return f"sim-{param_id}-{replicate_idx}"

    @staticmethod
    def agg_key(param_id: str) -> str:
        """Generate aggregation task key: agg-{param_id}"""
        # Use full param_id to avoid collisions
        return f"agg-{param_id}"

    @staticmethod
    def single_sim_key(seed: int, bundle_ref: str) -> str:
        """Generate single simulation key: sim-{seed}-{bundle[:12]}"""
        # Bundle ref truncation is less risky as it's for display
        return f"sim-{seed}-{bundle_ref[:12]}"


def _worker_run_task(task: SimTask) -> SimReturn:
    """Execute task on worker using plugin-initialized runtime.

    This function runs on the Dask worker and uses the ModelOps runtime
    that was initialized by the WorkerPlugin.

    Args:
        task: Simulation task to execute

    Returns:
        Simulation result
    """
    import logging
    import time

    logger = logging.getLogger(__name__)
    worker = get_worker()

    if not hasattr(worker, "modelops_runtime"):
        raise RuntimeError(
            "ModelOps runtime not initialized. "
            "Ensure ModelOpsWorkerPlugin is registered with the client."
        )

    start = time.perf_counter()
    result = worker.modelops_runtime.execute(task)
    duration_ms = (time.perf_counter() - start) * 1000

    # Diagnostic: sim task timing
    logger.info(
        f"SIM_TIMING: param_id={task.params.param_id[:8]} "
        f"seed={task.seed} duration_ms={duration_ms:.1f} "
        f"worker={worker.address}"
    )

    return result


def _worker_run_aggregation(task: AggregationTask) -> AggregationReturn:
    """Execute aggregation on worker using plugin-initialized runtime.

    This runs ON THE WORKER, using the ModelOps runtime that was
    initialized by the WorkerPlugin. It enables worker-side aggregation
    to avoid transferring all replicate data to the client.
    """
    worker = get_worker()

    if not hasattr(worker, "modelops_exec_env"):
        raise RuntimeError(
            "ModelOps execution environment not initialized. "
            "Ensure ModelOpsWorkerPlugin is registered."
        )

    # Use the IsolatedWarmExecEnv's run_aggregation method
    # TODO: why go around the modelops_runtime?
    return worker.modelops_exec_env.run_aggregation(task)


def _inline_bytes(sim_returns):
    """Compute total inline bytes across all SimReturn outputs (cheap, no pickle)."""
    total = 0
    n_outputs = 0
    max_single = 0
    for sr in sim_returns:
        if getattr(sr, "outputs", None):
            for art in sr.outputs.values():
                n_outputs += 1
                b = getattr(art, "inline", None)
                if b:
                    size = len(b)
                    total += size
                    max_single = max(max_single, size)
    return total, n_outputs, max_single


def _worker_run_aggregation_direct(*sim_returns, target_ep, bundle_ref, run_id=None, param_id=None):
    """Aggregate results directly without gather to avoid deadlock.

    This function fixes a critical deadlock that occurred when aggregation tasks
    called client.gather() inside a worker thread. The deadlock happened because
    aggregation tasks would occupy all worker threads while waiting for their
    dependencies (replicate tasks) to complete, but those tasks couldn't run
    because all threads were blocked. By passing futures as direct dependencies
    (*args), Dask's scheduler materializes them before calling this function,
    eliminating the deadlock while keeping aggregation on workers (not scheduler).

    Args:
        *sim_returns: Materialized SimReturn objects (Dask passes these)
        target_ep: Target entrypoint string
        bundle_ref: Bundle reference
        run_id: Optional run identifier for diagnostic correlation
        param_id: Optional param identifier for diagnostic correlation

    Returns:
        AggregationReturn with computed loss
    """
    import logging
    import time
    from modelops_contracts.simulation import AggregationTask

    logger = logging.getLogger(__name__)
    worker = get_worker()
    start = time.perf_counter()

    # Extract target suffix for logging
    target_suffix = target_ep.split('/')[-1] if target_ep else "unknown"

    # Cheap payload size accounting (no cloudpickle, just inline bytes)
    total_bytes, n_outputs, max_single = _inline_bytes(sim_returns)
    total_mb = total_bytes / (1024 * 1024)

    # sim_returns are already materialized by Dask
    agg_task = AggregationTask(
        bundle_ref=bundle_ref,
        target_entrypoint=target_ep,
        sim_returns=list(sim_returns),
    )

    result = _worker_run_aggregation(agg_task)
    duration_ms = (time.perf_counter() - start) * 1000

    # Diagnostic: aggregation timing, payload size, and locality
    logger.info(
        f"AGG_TIMING: run_id={run_id or 'N/A'} param_id={(param_id or 'N/A')[:8]} "
        f"target={target_suffix} n_sim_returns={len(sim_returns)} "
        f"n_outputs={n_outputs} total_inline_mb={total_mb:.2f} "
        f"max_single_kb={max_single/1024:.1f} duration_ms={duration_ms:.1f} "
        f"worker={worker.address}"
    )

    return result


class DaskFutureAdapter:
    """Adapt Dask Future to our Future protocol."""

    def __init__(self, dask_future: DaskFuture):
        self.wrapped = dask_future

    def result(self, timeout: float | None = None) -> SimReturn:
        return self.wrapped.result(timeout=timeout)

    def done(self) -> bool:
        return self.wrapped.done()

    def cancel(self) -> bool:
        return self.wrapped.cancel()

    def exception(self) -> Exception | None:
        return self.wrapped.exception()


class DaskSimulationService(SimulationService):
    """Dask-based implementation of SimulationService.

    This service submits simulation tasks to a Dask cluster and
    manages the WorkerPlugin lifecycle.
    """

    def __init__(self, client: Client):
        """Initialize the service.

        Args:
            client: Dask client connected to a cluster

        Note:
            Workers create their own RuntimeConfig from environment variables.
            This ensures workers read THEIR env vars, not the runner's.
        """
        self.client = client
        self._plugin_installed = False

        # Install the worker plugin
        self._install_plugin()

    def _install_plugin(self):
        """Install the ModelOps worker plugin on all workers."""
        if self._plugin_installed:
            return

        logger.info("Installing ModelOps worker plugin on all workers")

        # Create the plugin (workers will read their own environment)
        plugin = ModelOpsWorkerPlugin()

        # Register it with the cluster
        # Use the current API - register_plugin() handles all plugin types
        # register_worker_plugin() is deprecated since 2023.9.2
        self.client.register_plugin(plugin, name="modelops-runtime-v1")

        self._plugin_installed = True
        logger.info("Worker plugin installed successfully")

    def submit(self, task: SimTask) -> Future[SimReturn]:
        """Submit a simulation task to the cluster.

        Args:
            task: Simulation task to execute

        Returns:
            Future for the result
        """
        # Submit to Dask - the task will be executed by the worker plugin
        dask_future = self.client.submit(
            _worker_run_task,
            task,
            pure=False,  # Tasks have unique IDs
            key=TaskKeys.single_sim_key(task.seed, task.bundle_ref),  # For debugging
        )

        return DaskFutureAdapter(dask_future)

    def gather(self, futures: list[Future[SimReturn]]) -> list[SimReturn | Exception]:
        """Gather results from submitted tasks.

        Waits for all futures in parallel, then collects results.
        Failed tasks return their Exception object instead of raising,
        allowing callers to handle partial failures gracefully.

        Args:
            futures: List of futures from submit()

        Returns:
            List of results in same order as input futures.
            Failed futures return Exception objects as values.
        """
        dask_futures = [f.wrapped for f in futures]

        # Wait for ALL futures to complete (parallel wait)
        # This is critical for performance - don't call .result() sequentially!
        wait(dask_futures)

        # All futures are now done - collect results (non-blocking)
        results: list[SimReturn | Exception] = []
        for f in dask_futures:
            if getattr(f, "status", None) == "error":
                results.append(f.exception())
            else:
                try:
                    results.append(f.result())
                except Exception as e:
                    results.append(e)
        return results

    def submit_batch(self, tasks: list[SimTask]) -> list[Future[SimReturn]]:
        """Submit multiple tasks efficiently.

        Args:
            tasks: List of simulation tasks

        Returns:
            List of futures, one per task
        """
        dask_futures = self.client.map(
            _worker_run_task,
            tasks,
            pure=False,
            key=[TaskKeys.single_sim_key(t.seed, t.bundle_ref) for t in tasks],
        )

        return [DaskFutureAdapter(f) for f in dask_futures]

    def submit_replicate_set(
        self, replicate_set: ReplicateSet, target_entrypoint: str | None = None
    ) -> Future[AggregationReturn | list[SimReturn]]:
        """Submit a replicate set with optional worker-side aggregation.

        DEPRECATED: Use submit_replicates() + submit_aggregation() instead for
        multi-target workflows. This method causes Dask inline artifact size errors
        when the same replicate set is submitted multiple times for different targets.

        This is the KEY method for grouped execution:
        1. Submits all replicates as individual tasks
        2. If target_entrypoint provided, aggregates ON WORKER
        3. Returns single Future with aggregated result

        Args:
            replicate_set: Set of replicates to run
            target_entrypoint: Optional target for aggregation

        Returns:
            Future containing AggregationReturn (or List[SimReturn] if no target)
        """
        # Submit individual replicates
        tasks = replicate_set.tasks()

        # Generate proper Dask keys for dashboard grouping
        param_id = replicate_set.base_task.params.param_id
        keys = [TaskKeys.sim_key(param_id, i) for i in range(replicate_set.n_replicates)]

        # Use map for efficient batch submission
        replicate_futures = self.client.map(
            _worker_run_task,
            tasks,
            pure=False,
            key=keys,  # Explicit keys for tracking
        )

        # TODO: we should support iteration over many targets?
        # or should the Targets abstraction encapsulate that?
        if target_entrypoint:
            # Submit aggregation that runs ON WORKER
            # This is the magic - no data comes back to client!

            # NEW: Use direct dependency handling to avoid deadlock
            # Pass futures as dependencies - Dask will materialize them
            param_id = replicate_set.base_task.params.param_id

            # Check if any worker has aggregation resources
            # This prevents deadlock in tests/local clusters without resources
            submit_kwargs = {"pure": False}
            try:
                # Check scheduler info for worker resources
                info = self.client.scheduler_info()
                has_aggregation_resource = any(
                    "aggregation" in worker.get("resources", {})
                    for worker in info.get("workers", {}).values()
                )
                if has_aggregation_resource:
                    submit_kwargs["resources"] = {"aggregation": 1}
                    logger.debug("Using aggregation resource constraint")
            except Exception:
                # If we can't check, don't apply constraint
                logger.debug("Could not check for aggregation resources")

            agg_future = self.client.submit(
                _worker_run_aggregation_direct,
                *replicate_futures,  # Unpack futures as args - Dask handles dependencies
                target_ep=target_entrypoint,
                bundle_ref=replicate_set.base_task.bundle_ref,
                key=TaskKeys.agg_key(param_id),
                **submit_kwargs,
            )

            return DaskFutureAdapter(agg_future)

        else:
            # No aggregation, return list of SimReturns
            # Package as a single future for consistent interface
            def gather_results(futures):
                from dask.distributed import get_client

                client = get_client()
                return client.gather(futures)

            results_future = self.client.submit(gather_results, replicate_futures, pure=False)
            return DaskFutureAdapter(results_future)

    def submit_replicates(
        self, replicate_set: ReplicateSet, run_id: str | None = None
    ) -> list[Future[SimReturn]]:
        """Submit replicates without aggregation, returning individual simulation futures.

        This method is designed for multi-target workflows where the same simulation
        results need to be evaluated against multiple targets. By submitting replicates
        once and reusing the futures, we avoid redundant computation and Dask's inline
        artifact size limits when the same replicate set would otherwise be submitted
        multiple times.

        Args:
            replicate_set: Set of replicates to run
            run_id: Unique identifier for this submission to prevent key collisions.
                    If not provided, one will be generated.

        Returns:
            List of futures, one per replicate
        """
        import uuid

        if run_id is None:
            run_id = uuid.uuid4().hex[:10]

        tasks = replicate_set.tasks()
        param_id = replicate_set.base_task.params.param_id
        # Include run_id in keys to prevent collisions across concurrent submissions
        keys = [f"sim-{run_id}-{param_id}-{i}" for i in range(replicate_set.n_replicates)]

        # Submit all replicates as individual tasks
        replicate_futures = self.client.map(
            _worker_run_task,
            tasks,
            pure=False,
            key=keys,
        )

        return [DaskFutureAdapter(f) for f in replicate_futures]

    def submit_aggregation(
        self,
        sim_futures: list[Future[SimReturn]],
        target_entrypoint: str,
        bundle_ref: str,
        param_id: str,
        run_id: str | None = None,
    ) -> Future[AggregationReturn]:
        """Submit aggregation task for given simulation results and target.

        This method enables evaluating multiple targets on the same simulation results
        without re-running simulations. It uses Dask's scatter with broadcast to share
        simulation results across workers efficiently and avoid inline serialization limits.

        Args:
            sim_futures: List of simulation result futures to aggregate
            target_entrypoint: Target entrypoint to evaluate
            bundle_ref: Bundle reference for the aggregation task
            param_id: Parameter set ID for task naming
            run_id: Unique identifier for this submission to prevent key collisions.
                    Should match the run_id used in submit_replicates().

        Returns:
            Future containing aggregated result with target loss
        """
        import uuid

        if run_id is None:
            run_id = uuid.uuid4().hex[:10]

        # Unwrap DaskFutureAdapter to get raw Dask futures
        dask_futures = [f.wrapped for f in sim_futures]

        # Pass futures directly as dependencies - Dask will materialize them
        # before calling _worker_run_aggregation_direct. No need to scatter
        # since futures are already references to distributed data.

        # NOTE: We intentionally do NOT apply the aggregation resource constraint here.
        # With direct dependency passing (*dask_futures), Dask materializes sim results
        # BEFORE calling the aggregation function, so deadlock risk is eliminated.
        # Omitting the constraint lets Dask's scheduler pick workers with better locality
        # (workers that already hold the simulation data), avoiding costly data transfers.
        submit_kwargs = {"pure": False}

        # Include run_id in key to prevent collisions across concurrent submissions
        target_suffix = target_entrypoint.split('/')[-1]
        agg_key = f"agg-{run_id}-{param_id}-{target_suffix}"

        # Submit aggregation with futures as dependencies
        # Dask will materialize them before calling the function
        agg_future = self.client.submit(
            _worker_run_aggregation_direct,
            *dask_futures,
            target_ep=target_entrypoint,
            bundle_ref=bundle_ref,
            run_id=run_id,
            param_id=param_id,
            key=agg_key,
            **submit_kwargs,
        )

        return DaskFutureAdapter(agg_future)

    def submit_batch_with_aggregation(
        self, replicate_sets: list[ReplicateSet], target_entrypoint: str
    ) -> list[Future[AggregationReturn]]:
        """Submit multiple replicate sets with aggregation.

        This enables efficient batch submission of multiple parameter sets,
        each with their own replicates and aggregation.

        Args:
            replicate_sets: List of replicate sets
            target_entrypoint: Target for aggregation

        Returns:
            List of futures, one per replicate set
        """
        return [self.submit_replicate_set(rs, target_entrypoint) for rs in replicate_sets]
