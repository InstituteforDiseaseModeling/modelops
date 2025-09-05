"""SimulationService implementations for distributed and local execution."""

from modelops_contracts import SimulationService, SimReturn, FutureLike
from typing import Any, List, Optional
import importlib
import logging
import warnings
from contextlib import redirect_stderr
import io
import os
from .ipc import to_ipc_tables, from_ipc_tables, validate_sim_return
from ..runtime.runners import SimulationRunner, DirectRunner, get_runner

# Logger for capturing Dask warnings
dask_logger = logging.getLogger("modelops.dask.warnings")


class LocalSimulationService:
    """Local execution for testing without Dask.
    
    This implementation runs simulations in-process, useful for:
    - Development and testing
    - Small-scale experiments
    - Environments without Kubernetes
    """
    
    def __init__(self, runner: Optional[SimulationRunner] = None):
        """Initialize with optional runner.
        
        Args:
            runner: SimulationRunner to use. Defaults to DirectRunner.
        """
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
        return self.runner.run(fn_ref, params, seed, bundle_ref)
    
    def gather(self, futures: List[Any]) -> List[SimReturn]:
        """Gather results from submitted simulations.
        
        For local execution, "futures" are just the results themselves.
        
        Args:
            futures: List of results from submit()
            
        Returns:
            The same list (no gathering needed for local)
        """
        return futures


class DaskSimulationService:
    """Dask distributed execution on a cluster.
    
    This implementation submits simulations to a Dask cluster for
    distributed execution across multiple workers.
    """
    
    def __init__(self, scheduler_address: str, silence_warnings: bool = True,
                 runner_type: Optional[str] = None):
        """Initialize connection to Dask cluster.
        
        Args:
            scheduler_address: Address of Dask scheduler (e.g., "tcp://localhost:8786")
            silence_warnings: Whether to suppress version mismatch warnings (default: True).
                            Warnings are still logged to 'modelops.dask.warnings' logger.
            runner_type: Type of runner to use on workers ("direct", "bundle", "cached").
                        If None, uses MODELOPS_RUNNER_TYPE env var or defaults to "direct".
        """
        from dask.distributed import Client
        
        self.runner_type = runner_type
        
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
        # Pass runner type to workers if configured
        if self.runner_type:
            # Set environment variable for workers
            worker_env = {"MODELOPS_RUNNER_TYPE": self.runner_type}
            return self.client.submit(
                _worker_run_sim, fn_ref, params, seed, bundle_ref,
                workers=None, resources=None, retries=0,
                priority=0, fifo_timeout="100ms", allow_other_workers=False,
                actor=None, actors=None, pure=None,
                key=None,
                # Pass environment to workers
                # Note: This requires Dask workers to be configured to accept env vars
                # For now, we rely on workers having MODELOPS_RUNNER_TYPE set
            )
        else:
            return self.client.submit(_worker_run_sim, fn_ref, params, seed, bundle_ref)
    
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