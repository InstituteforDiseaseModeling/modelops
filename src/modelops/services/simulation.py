"""SimulationService implementations for distributed and local execution."""

from modelops_contracts import (
    SimulationService, SimReturn, FutureLike, UniqueParameterSet,
    AggregatorFunction, Scalar, make_param_id
)
from typing import Any, List, Optional, Union, Dict, Tuple
import logging
import numpy as np
from .utils import resolve_function, import_function
from .cache import SimulationCache
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
        if cache:
            logger.info(f"Initialized with cache: {cache.config.storage_prefix}")
    
    def submit_batch(self, fn_ref: str, param_sets: List[UniqueParameterSet], 
                     seed: int, *, bundle_ref: str) -> List[FutureLike]:
        """Submit batch with deterministic seed derivation.
        
        Uses numpy.random.SeedSequence for statistically independent seeds.
        """
        # Use SeedSequence for independent seeds
        ss = np.random.SeedSequence(seed)
        seeds = ss.spawn(len(param_sets))
        
        futures = []
        for param_set, child_ss in zip(param_sets, seeds):
            # Extract single seed value from sequence
            seed_val = int(child_ss.generate_state(1)[0])
            params_dict = dict(param_set.params)
            
            # Check cache first if available
            if self.cache and self.cache.exists(params_dict, seed_val):
                logger.debug(f"Cache hit for param_id={param_set.param_id[:8]}...")
                cached_result = self.cache.get(params_dict, seed_val)
                # Deserialize the cached result
                if isinstance(cached_result, bytes):
                    cached_result = pickle.loads(cached_result)
                futures.append(self._make_cached_future(cached_result))
            else:
                # Submit actual computation
                future = self.submit(fn_ref, params_dict, seed_val, bundle_ref=bundle_ref)
                
                # Store param_id with future for cache storage later (if possible)
                if hasattr(future, '__dict__'):
                    future._param_id = param_set.param_id
                    future._params = params_dict
                    future._seed = seed_val
                
                futures.append(future)
        
        return futures
    
    def submit_replicates(self, fn_ref: str, params: Dict[str, Scalar],
                          seed: int, *, bundle_ref: str, 
                          n_replicates: int) -> List[FutureLike]:
        """Submit replicates with statistically independent seeds.
        
        Uses numpy.random.SeedSequence to generate high-quality independent seeds.
        """
        ss = np.random.SeedSequence(seed)
        child_seeds = ss.spawn(n_replicates)
        
        futures = []
        for child_ss in child_seeds:
            seed_val = int(child_ss.generate_state(1)[0])
            
            if self.cache and self.cache.exists(params, seed_val):
                logger.debug(f"Cache hit for seed={seed_val}")
                cached_result = self.cache.get(params, seed_val)
                # get() now returns proper SimReturn, no deserialization needed
                futures.append(self._make_cached_future(cached_result))
            else:
                future = self.submit(fn_ref, params, seed_val, bundle_ref=bundle_ref)
                
                # Store params and seed with future for cache storage later
                if hasattr(future, '__dict__'):
                    future._params = params
                    future._seed = seed_val
                
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
            
            # Cache results if futures have params/seed metadata
            if self.cache:
                for future, result in zip(futures, results):
                    if (hasattr(future, '_params') and hasattr(future, '_seed') 
                        and future._params and future._seed):
                        self.cache.put(future._params, future._seed, result)
            
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
    
    def submit(self, fn_ref: str, params: dict, seed: int, *, bundle_ref: str) -> Any:
        """Submit a simulation for local execution.
        
        Args:
            fn_ref: Function reference as "module:function"
            params: Parameter dictionary with scalar values
            seed: Random seed for reproducibility
            bundle_ref: Bundle reference (ignored in MVP, assumes code is installed)
            
        Returns:
            The simulation result directly (not a future)
        """
        # Use runner to execute simulation
        result = self.runner.run(fn_ref, params, seed, bundle_ref)
        
        # Store in cache if available
        if self.cache:
            self.cache.put(params, seed, result)
        
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
        self._cache_meta: Dict[str, Tuple[Dict[str, Scalar], int]] = {}
        
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
    
    def submit(self, fn_ref: str, params: dict, seed: int, *, bundle_ref: str) -> FutureLike:
        """Submit a simulation to Dask cluster.
        
        Args:
            fn_ref: Function reference as "module:function"
            params: Parameter dictionary with scalar values
            seed: Random seed for reproducibility
            bundle_ref: Bundle reference for code/data dependencies
            
        Returns:
            A Dask future representing the pending computation
        """
        # Fast-path: return cached result if available
        if self.cache and self.cache.exists(params, seed):
            cached = self.cache.get(params, seed)
            return self._make_cached_future(cached)
        
        # Submit to cluster
        future = self.client.submit(_worker_run_sim, fn_ref, params, seed, bundle_ref, pure=False)
        
        # Register callback to cache result when complete
        if self.cache:
            self._cache_meta[future.key] = (dict(params), int(seed))
            future.add_done_callback(self._cache_callback)
        
        return future
    
    def _cache_callback(self, future):
        """Callback to cache results as futures complete."""
        try:
            meta = self._cache_meta.pop(future.key, None)
            if not meta:
                return
            params, seed = meta
            result = future.result()
            # Cache the result
            self.cache.put(params, seed, result)
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
            test_future = self.client.submit(
                _worker_run_sim,
                "builtins:str",  # Simple built-in function
                {"object": "test"},
                seed=0,
                bundle_ref=""
            )
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


def _worker_run_sim(fn_ref: str, params: dict, seed: int, bundle_ref: str) -> SimReturn:
    """Function that runs on Dask workers.
    
    This function is serialized and sent to workers for execution.
    Uses runner type from MODELOPS_RUNNER_TYPE environment variable.
    
    Args:
        fn_ref: Function reference as "module:function"
        params: Parameter dictionary
        seed: Random seed
        bundle_ref: Bundle reference for code/data dependencies
        
    Returns:
        Simulation result as SimReturn (dict of named tables as IPC bytes)
    """
    # Get appropriate runner based on environment configuration
    runner = get_runner()
    
    # Execute using the runner
    return runner.run(fn_ref, params, seed, bundle_ref)


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