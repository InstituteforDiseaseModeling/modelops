"""SimulationService implementations for distributed and local execution."""

from modelops_contracts import SimulationService, SimReturn, FutureLike
from typing import Any, List
import importlib
import logging
import warnings
from contextlib import redirect_stderr
import io
from .ipc import to_ipc_tables, from_ipc_tables, validate_sim_return

# Logger for capturing Dask warnings
dask_logger = logging.getLogger("modelops.dask.warnings")


class LocalSimulationService:
    """Local execution for testing without Dask.
    
    This implementation runs simulations in-process, useful for:
    - Development and testing
    - Small-scale experiments
    - Environments without Kubernetes
    """
    
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
        # MVP: ignore bundle_ref, assume code is already installed
        module_name, func_name = fn_ref.split(":")
        mod = importlib.import_module(module_name)
        func = getattr(mod, func_name)
        
        # Call simulation and convert to IPC format
        result = func(params, seed)
        return validate_sim_return(result)
    
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
    
    def __init__(self, scheduler_address: str, silence_warnings: bool = True):
        """Initialize connection to Dask cluster.
        
        Args:
            scheduler_address: Address of Dask scheduler (e.g., "tcp://localhost:8786")
            silence_warnings: Whether to suppress version mismatch warnings (default: True).
                            Warnings are still logged to 'modelops.dask.warnings' logger.
        """
        from dask.distributed import Client
        
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


def _worker_run_sim(fn_ref: str, params: dict, seed: int, bundle_ref: str) -> SimReturn:
    """Function that runs on Dask workers.
    
    This function is serialized and sent to workers for execution.
    
    Args:
        fn_ref: Function reference as "module:function"
        params: Parameter dictionary
        seed: Random seed
        bundle_ref: Bundle reference (MVP: ignored, assumes pre-installed)
        
    Returns:
        Simulation result as SimReturn (dict of named tables as IPC bytes)
    """
    # TODO: In future, handle bundle loading here
    # For MVP, assume simulation code is pre-installed on workers
    
    from .ipc import validate_sim_return
    
    module_name, func_name = fn_ref.split(":")
    mod = importlib.import_module(module_name)
    func = getattr(mod, func_name)
    
    # Call the simulation function
    result = func(params, seed)
    
    # Convert to IPC format per contract
    return validate_sim_return(result)