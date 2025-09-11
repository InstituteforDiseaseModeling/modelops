"""SimulationService implementations for distributed and local execution."""

from modelops_contracts import (
    SimulationService, SimReturn, FutureLike, UniqueParameterSet,
    AggregatorFunction, Scalar, make_param_id, SimTask, EntryPointId
)
from typing import Any, List, Optional, Union, Dict, Tuple
import logging
import os
import numpy as np
from .utils import resolve_function, import_function
from .cache import SimulationCache
# from .aggregation import AggregationService  # TODO: Update for new SimTask interface
from ..runtime.runners import SimulationRunner, DirectRunner, get_runner

# Logger for capturing Dask warnings
dask_logger = logging.getLogger("modelops.dask.warnings")
logger = logging.getLogger(__name__)


class BaseSimulationService(SimulationService):
    """Base implementation with shared logic for all simulation services.
    
    Provides replication methods, batch submission, and caching support
    that can be reused by all concrete implementations.
    """
    
    def __init__(self, cache: Optional[SimulationCache] = None):
        """Initialize base service.
        
        Args:
            cache: Optional SimulationCache for result deduplication
        """
        self.cache = cache
        # self.aggregation_service = aggregation_service or AggregationService()  # TODO: Update
        if cache:
            logger.info(f"Initialized with cache: {cache.config.storage_prefix}")
    
    
    def submit_batch(self, tasks: List[SimTask]) -> List[FutureLike]:
        """Submit multiple simulation tasks.
        
        Each task has its own UniqueParameterSet for tracking with param_id.
        Seeds can be derived deterministically or specified per task.
        
        Args:
            tasks: List of SimTask specifications
            
        Returns:
            List of futures, one per task
        """
        futures = []
        for task in tasks:
            # Check cache first if available
            if self.cache and self.cache.exists(task):
                logger.debug(f"Cache hit for task_id={task.task_id()[:8]}...")
                cached_result = self.cache.get(task)
                futures.append(self._make_cached_future(cached_result))
            else:
                # Submit actual computation
                future = self.submit(task)
                
                # Store task with future for cache storage later (if possible)
                if hasattr(future, '__dict__'):
                    future._task = task
                
                futures.append(future)
        
        return futures
    
    def submit_replicates(self, base_task: SimTask, n_replicates: int) -> List[FutureLike]:
        """Submit multiple replicates of the same task.
        
        Implementations should derive replicate seeds deterministically
        from the base task's seed to ensure reproducibility.
        
        Args:
            base_task: Base SimTask to replicate
            n_replicates: Number of replicates to run
            
        Returns:
            List of futures, one per replicate
        """
        # Use SeedSequence for statistically independent seeds
        # TODO: we might want to handle CRN, etc here more carefully.
        ss = np.random.SeedSequence(base_task.seed)
        child_seeds = ss.spawn(n_replicates)
        
        futures = []
        for child_ss in child_seeds:
            seed_val = int(child_ss.generate_state(1)[0])
            
            # Create replicate task with new seed
            replicate_task = SimTask(
                bundle_ref=base_task.bundle_ref,
                entrypoint=base_task.entrypoint,
                params=base_task.params,
                seed=seed_val,
                outputs=base_task.outputs
            )
            
            if self.cache and self.cache.exists(replicate_task):
                logger.debug(f"Cache hit for seed={seed_val}")
                cached_result = self.cache.get(replicate_task)
                futures.append(self._make_cached_future(cached_result))
            else:
                future = self.submit(replicate_task)
                
                # Store task with future for cache storage later
                if hasattr(future, '__dict__'):
                    future._task = replicate_task
                
                futures.append(future)
        
        return futures
    
    def gather_and_aggregate(self, futures: List[FutureLike],
                             aggregator: Union[str, AggregatorFunction]) -> SimReturn:
        """Gather and aggregate with support for both string refs and callables.
        
        Execution strategy depends on aggregator type:
        - String refs ("module:function"): Aggregation runs ON workers,
          minimizing data transfer. Only the aggregated result is returned.
        - Callables (functions, lambdas): All results are gathered from workers
          first, then aggregation runs locally. This avoids serialization issues
          but has a performance penalty due to data transfer.
        
        For best performance with large datasets, use string references.
        """
        is_distributed, resolved = resolve_function(aggregator)
        
        if is_distributed:
            # Use worker-side aggregation with string ref
            return self._gather_and_aggregate_distributed(futures, resolved)
        else:
            # Local aggregation with callable
            results = self.gather(futures)
            
            # Cache results if futures have task metadata
            if self.cache:
                for future, result in zip(futures, results):
                    if hasattr(future, '_task') and future._task:
                        self.cache.put(future._task, result)
            
            return resolved(results)
    
    def _gather_and_aggregate_distributed(self, futures: List[FutureLike], 
                                          aggregator_ref: str) -> SimReturn:
        """Default implementation for distributed aggregation.
        
        Subclasses can override for worker-side aggregation.
        """
        results = self.gather(futures)
        aggregator_fn = import_function(aggregator_ref)
        return aggregator_fn(results)
    
    def _make_cached_future(self, result: SimReturn) -> FutureLike:
        """Create a future-like object that returns cached result.
        
        Subclasses should override for their specific future type.
        """
        return result


class LocalSimulationService(BaseSimulationService):
    """Local execution for testing without Dask.
    
    This implementation runs simulations in-process, useful for:
    - Development and testing
    - Small-scale experiments
    - Environments without Kubernetes
    """
    
    def __init__(self, runner: Optional[SimulationRunner] = None,
                 cache: Optional[SimulationCache] = None):
        """Initialize with optional runner and cache.
        
        Args:
            runner: SimulationRunner to use. Defaults to DirectRunner.
            cache: Optional SimulationCache for result deduplication.
        """
        super().__init__(cache=cache)
        self.runner = runner or DirectRunner()
    
    def submit(self, task: SimTask) -> Any:
        """Submit a simulation task for local execution.
        
        Args:
            task: SimTask specification containing all execution parameters
            
        Returns:
            The simulation result directly (not a future)
        """
        # Extract function reference from entrypoint
        # EntryPointId format: "pkg.module.Class/scenario"
        from modelops_contracts import parse_entrypoint
        import_path, scenario = parse_entrypoint(task.entrypoint)
        
        # Use runner to execute simulation with import path directly
        # Runners now handle dot notation (e.g., "pkg.module.function")
        result = self.runner.run(import_path, dict(task.params.params), task.seed, task.bundle_ref)
        
        # Store in cache if available
        if self.cache:
            self.cache.put(task, result)
        
        return result
    
    def gather(self, futures: List[Any]) -> List[SimReturn]:
        """Gather results from submitted simulations.
        
        For local execution, "futures" are just the results themselves.
        
        Args:
            futures: List of results from submit()
            
        Returns:
            The same list (no gathering needed for local)
        """
        return futures


class DaskSimulationService(BaseSimulationService):
    """Dask distributed execution on a cluster.
    
    This implementation submits simulations to a Dask cluster for
    distributed execution across multiple workers. Includes worker-side
    aggregation for efficient reduction operations.
    """
    
    def __init__(self, scheduler_address: str, silence_warnings: bool = True,
                 runner_type: Optional[str] = None, cache: Optional[SimulationCache] = None):
        """Initialize connection to Dask cluster.
        
        Args:
            scheduler_address: Address of Dask scheduler (e.g., "tcp://localhost:8786")
            silence_warnings: Whether to suppress version mismatch warnings (default: True).
                            Warnings are still logged to 'modelops.dask.warnings' logger.
            runner_type: Type of runner to use on workers ("direct", "bundle", "cached").
                        If None, uses MODELOPS_RUNNER_TYPE env var or defaults to "direct".
            cache: Optional SimulationCache for result deduplication.
        """
        super().__init__(cache=cache)
        from dask.distributed import Client
        import warnings
        from contextlib import redirect_stderr
        import io
        import os
        
        self.runner_type = runner_type
        # Track future metadata for caching
        self._cache_meta: Dict[str, SimTask] = {}
        
        # Log which runner type will be used
        actual_runner = runner_type or os.getenv("MODELOPS_RUNNER_TYPE", "direct")
        logging.getLogger("modelops").info(f"DaskSimulationService using runner: {actual_runner}")
        
        if silence_warnings:
            # Capture warnings to log them
            stderr_buffer = io.StringIO()
            with warnings.catch_warnings(record=True) as warning_list:
                warnings.simplefilter("always")
                with redirect_stderr(stderr_buffer):
                    self.client = Client(scheduler_address)
                
                # Log any warnings that were generated
                stderr_output = stderr_buffer.getvalue()
                if stderr_output:
                    dask_logger.info(f"Dask connection warnings (suppressed):\n{stderr_output}")
                
                for w in warning_list:
                    dask_logger.warning(f"{w.category.__name__}: {w.message}")
        else:
            # Normal connection with warnings visible
            self.client = Client(scheduler_address)
    
    def submit(self, task: SimTask) -> FutureLike:
        """Submit a simulation task to Dask cluster.
        
        Args:
            task: SimTask specification containing all execution parameters
            
        Returns:
            A Dask future representing the pending computation
        """
        # Fast-path: return cached result if available
        if self.cache and self.cache.exists(task):
            cached = self.cache.get(task)
            return self._make_cached_future(cached)
        
        # Submit to cluster
        future = self.client.submit(_worker_run_sim, task, pure=False)
        
        # Register callback to cache result when complete
        if self.cache:
            self._cache_meta[future.key] = task
            future.add_done_callback(self._cache_callback)
        
        return future
    
    def _cache_callback(self, future):
        """Callback to cache results as futures complete."""
        try:
            task = self._cache_meta.pop(future.key, None)
            if not task:
                return
            result = future.result()
            # Cache the result with task
            self.cache.put(task, result)
        except Exception as e:
            logger.warning(f"Cache callback failed for {future.key}: {e}")
    
    def gather(self, futures: List[FutureLike]) -> List[SimReturn]:
        """Gather results from Dask futures.
        
        Blocks until all futures are complete and returns results
        in the same order as the input futures.
        
        Args:
            futures: List of Dask futures from submit()
            
        Returns:
            List of simulation results
        """
        return self.client.gather(futures)
    
    def _gather_and_aggregate_distributed(self, futures: List[FutureLike], 
                                          aggregator_ref: str) -> SimReturn:
        """Perform aggregation ON workers to minimize data transfer.
        
        This is the KEY performance optimization - aggregate on workers
        to avoid transferring large replicate data back to scheduler.
        """
        # Submit aggregation task that operates on futures
        aggregation_future = self.client.submit(
            _worker_aggregate,
            futures,
            aggregator_ref,
            pure=False  # Not pure since it depends on futures
        )
        
        # Gather only the aggregated result (much smaller!)
        return self.client.gather(aggregation_future)
    
    def _make_cached_future(self, result: SimReturn) -> FutureLike:
        """Create a Dask future from cached result."""
        # Use scatter to efficiently place cached data in cluster
        # This avoids task overhead and allows Dask to optimize placement
        return self.client.scatter(result, broadcast=False)
    
    def close(self):
        """Close connection to Dask cluster."""
        self.client.close()
    
    @classmethod
    def from_config(cls, config: dict) -> "DaskSimulationService":
        """Create DaskSimulationService from configuration dict.
        
        Args:
            config: Configuration dictionary with keys:
                - scheduler_address: Dask scheduler address
                - silence_warnings: Whether to suppress warnings (optional)
                - runner_type: Runner type for workers (optional)
                
        Returns:
            Configured DaskSimulationService instance
        """
        return cls(
            scheduler_address=config["scheduler_address"],
            silence_warnings=config.get("silence_warnings", True),
            runner_type=config.get("runner_type")
        )
    
    def health_check(self) -> dict:
        """Check health of the service and runner.
        
        Returns:
            Dict with health status information
        """
        try:
            # Check Dask cluster connection
            info = self.client.scheduler_info()
            n_workers = len(info.get('workers', {}))
            
            # Test runner with simple function
            test_task = SimTask(
                bundle_ref="test",
                entrypoint="builtins.str/test",
                params=UniqueParameterSet.from_dict({"object": "test"}),
                seed=0
            )
            test_future = self.client.submit(_worker_run_sim, test_task)
            test_result = self.client.gather(test_future, timeout=5)
            
            return {
                "status": "healthy",
                "scheduler": self.client.scheduler.address,
                "workers": n_workers,
                "runner_type": self.runner_type or os.getenv("MODELOPS_RUNNER_TYPE", "direct"),
                "test_run": "success" if test_result else "failed"
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "runner_type": self.runner_type or os.getenv("MODELOPS_RUNNER_TYPE", "direct")
            }


def _worker_run_sim(task: SimTask) -> SimReturn:
    """Function that runs on Dask workers.
    
    This function is serialized and sent to workers for execution.
    Uses runner type from MODELOPS_RUNNER_TYPE environment variable.
    
    Args:
        task: SimTask specification containing all execution parameters
        
    Returns:
        Simulation result as SimReturn (dict of named tables as IPC bytes)
    """
    from modelops_contracts import parse_entrypoint
    
    # Get appropriate runner based on environment configuration
    runner = get_runner()
    
    # Extract function reference from entrypoint
    import_path, scenario = parse_entrypoint(task.entrypoint)
    
    # Use import path directly - runners now handle dot notation
    return runner.run(import_path, dict(task.params.params), task.seed, task.bundle_ref)


def _worker_aggregate(futures: List[FutureLike], aggregator_ref: str) -> SimReturn:
    """Aggregate function that runs ON a Dask worker.
    
    This runs ON the worker, gathering futures locally and applying aggregation
    to avoid transferring all replicate data back to the client. This is a
    key performance optimization for large simulations.
    
    Args:
        futures: List of Dask futures to aggregate
        aggregator_ref: String reference to aggregator function (module:function)
        
    Returns:
        Aggregated SimReturn
        
    Examples:
        >>> # This function is called by DaskSimulationService._gather_and_aggregate_distributed
        >>> # It runs on a worker, not the client!
        >>> result = _worker_aggregate(futures, "numpy:mean")
    """
    from dask.distributed import get_client
    from .utils import import_function
    
    # Get client on worker to gather futures locally
    client = get_client()
    results = client.gather(futures)  # Local gather on worker!
    
    # Import and apply the aggregator
    aggregator_fn = import_function(aggregator_ref)
    return aggregator_fn(results)
