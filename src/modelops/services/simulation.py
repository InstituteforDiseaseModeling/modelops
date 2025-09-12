"""SimulationService implementations for distributed and local execution."""

from modelops_contracts import (
    SimulationService, SimReturn, Future, UniqueParameterSet,
    Scalar, make_param_id, SimTask, EntryPointId
)
from typing import Any, List, Optional, Union, Dict, Tuple, Callable
import logging
import os
import numpy as np
from .utils import resolve_function, import_function
from .cache import SimulationCache
# from .aggregation import AggregationService  # TODO: Update for new SimTask interface

# Logger for capturing Dask warnings
dask_logger = logging.getLogger("modelops.dask.warnings")
logger = logging.getLogger(__name__)


class BaseSimulationService(SimulationService):
    """Base implementation with common functionality.
    
    Provides:
    - Cache integration for deduplication
    - Batch submission helpers
    - Standard logging
    """
    
    def __init__(self, cache: Optional[SimulationCache] = None):
        """Initialize with optional cache.
        
        Args:
            cache: Optional SimulationCache for result deduplication.
        """
        self.cache = cache
        # self.aggregation_service = aggregation_service or AggregationService()  # TODO: Update
    
    def submit_batch(self, tasks: List[SimTask], *, 
                     cache_policy: str = "read_write") -> List[Future[SimReturn]]:
        """Submit multiple tasks efficiently.
        
        Args:
            tasks: List of simulation tasks to submit
            cache_policy: One of:
                - "read_write": Check cache first, write results
                - "write_only": Don't check, but write results
                - "bypass": Ignore cache completely
            
        Returns:
            List of futures (or results for cached items)
        """
        futures = []
        
        for task in tasks:
            # Check cache if policy allows
            if cache_policy in ("read_write",) and self.cache:
                cached = self.cache.get(task)
                if cached is not None:
                    # Return cached result wrapped as completed future
                    from concurrent.futures import Future as ConcurrentFuture
                    future = ConcurrentFuture()
                    future.set_result(cached)
                    futures.append(future)
                    continue
            
            # Submit to execution
            future = self.submit(task)
            futures.append(future)
        
        return futures
    
    def batch_gather_with_cache(self, futures: List[Future[SimReturn]], 
                                 tasks: List[SimTask]) -> List[SimReturn]:
        """Gather results and update cache.
        
        Args:
            futures: List of futures from submit_batch
            tasks: Original tasks (for cache keys)
            
        Returns:
            List of simulation results
        """
        results = self.gather(futures)
        
        # Update cache with new results
        if self.cache:
            for task, result in zip(tasks, results):
                # Only cache successful results
                if hasattr(result, 'status') and result.status.value == "COMPLETED":
                    self.cache.put(task, result)
        
        return results
    
    def submit_replicated(self, task: SimTask, n_replicates: int, *,
                          seed_offset: int = 0) -> List[Future[SimReturn]]:
        """Submit replicated simulations with different seeds.
        
        This is a common pattern: same params, different seeds.
        
        Args:
            task: Base task to replicate
            n_replicates: Number of replicates to run
            seed_offset: Offset to add to task.seed for each replicate
            
        Returns:
            List of futures
        """
        futures = []
        
        for i in range(n_replicates):
            # Create replicate with offset seed
            replicate = SimTask(
                entrypoint=task.entrypoint,
                params=task.params,
                seed=task.seed + seed_offset + i,
                bundle_ref=task.bundle_ref
            )
            
            future = self.submit(replicate)
            futures.append(future)
        
        return futures
    
    def gather_and_aggregate(self, futures: List[Future[SimReturn]],
                             aggregator: Union[str, Callable]) -> SimReturn:
        """Gather and aggregate with support for both string refs and callables.
        
        Execution strategy depends on aggregator type:
        - String refs ("module:function"): Aggregation runs ON workers,
          avoiding data transfer of all replicates to client
        - Callable objects: Runs locally after gathering all results
        
        Args:
            futures: List of futures from replicated simulations
            aggregator: Either:
                - String reference like "numpy:mean" or "mymodule:aggregate_fn"
                - Callable that takes List[SimReturn] -> SimReturn
                
        Returns:
            Aggregated result as SimReturn
        """
        if isinstance(aggregator, str):
            # For string refs, resolve to function
            aggregator = resolve_function(aggregator)
        
        # Local aggregation (LocalSimulationService or callable aggregator)
        results = self.gather(futures)
        
        if callable(aggregator):
            return aggregator(results)
        else:
            raise ValueError(f"Invalid aggregator type: {type(aggregator)}")


class LocalSimulationService(BaseSimulationService):
    """Local in-process execution for development and testing.
    
    Executes simulations directly in the current process without
    any distributed infrastructure. Useful for:
    - Development and debugging
    - Small-scale experiments
    - Environments without Kubernetes
    """
    
    def __init__(self, cache: Optional[SimulationCache] = None):
        """Initialize with optional cache.
        
        Args:
            cache: Optional SimulationCache for result deduplication.
        """
        super().__init__(cache=cache)
        # Import here to avoid circular dependency
        from ..core.executor import SimulationExecutor
        from ..adapters.exec_env.direct import DirectExecEnv
        from ..adapters.bundle.file_repo import FileBundleRepository
        from ..adapters.cas.memory_cas import MemoryCAS
        from ..worker.config import RuntimeConfig
        
        config = RuntimeConfig.from_env()
        
        # Create bundle repository based on config
        if config.bundle_source == "file":
            bundle_repo = FileBundleRepository(
                bundles_dir=config.bundles_dir or "/tmp/modelops/bundles",
                cache_dir=config.bundles_cache_dir
            )
        else:
            raise NotImplementedError(f"LocalSimulationService doesn't support bundle_source={config.bundle_source} yet")
        
        # Create CAS (memory for local)
        cas = MemoryCAS()
        
        # Create execution environment with proper dependencies
        exec_env = DirectExecEnv(bundle_repo=bundle_repo, cas=cas)
        self.executor = SimulationExecutor(exec_env)
    
    def submit(self, task: SimTask) -> Future[SimReturn]:
        """Submit a simulation task for local execution.
        
        Args:
            task: SimTask specification containing all execution parameters
            
        Returns:
            A Future that contains the simulation result
        """
        from concurrent.futures import Future as ConcurrentFuture
        
        # Create a future and set the result immediately (synchronous execution)
        future = ConcurrentFuture()
        try:
            # Use executor to run the task
            result = self.executor.execute(task)
            
            # Store in cache if available
            if self.cache:
                self.cache.put(task, result)
            
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
        
        return future
    
    def gather(self, futures: List[Future[SimReturn]]) -> List[SimReturn]:
        """Gather results from submitted simulations.
        
        Args:
            futures: List of futures from submit()
            
        Returns:
            List of simulation results
        """
        results = []
        for future in futures:
            try:
                # For concurrent.futures.Future objects
                if hasattr(future, 'result'):
                    results.append(future.result())
                else:
                    # Fallback for any direct results (shouldn't happen)
                    results.append(future)
            except Exception as e:
                # Handle exceptions by re-raising
                raise e
        return results


# DaskSimulationService has been moved to dask_simulation.py
# Use: from modelops.services.dask_simulation import DaskSimulationService