"""SimulationService implementations for distributed and local execution."""

from modelops_contracts import SimulationService, SimReturn, FutureLike
from typing import Any, List
import importlib


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
        
        # For local execution, directly return the result
        return func(params, seed)
    
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
    
    def __init__(self, scheduler_address: str):
        """Initialize connection to Dask cluster.
        
        Args:
            scheduler_address: Address of Dask scheduler (e.g., "tcp://localhost:8786")
        """
        from dask.distributed import Client
        self.client = Client(scheduler_address)
    
    @classmethod
    def from_workspace(cls, workspace_name: str = "default") -> 'DaskSimulationService':
        """Create from a provisioned ModelOps workspace.
        
        Args:
            workspace_name: Name of the workspace to connect to
            
        Returns:
            DaskSimulationService connected to the workspace
            
        Raises:
            ValueError: If workspace not found
        """
        from ..state.manager import StateManager
        state = StateManager()
        workspace = state.get_workspace(workspace_name)
        
        if not workspace:
            raise ValueError(f"Workspace '{workspace_name}' not found. Run 'mops workspace up' first.")
        
        scheduler_address = workspace.get("scheduler_address")
        if not scheduler_address:
            raise ValueError(f"Workspace '{workspace_name}' has no scheduler address")
            
        return cls(scheduler_address)
    
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
        Simulation result as SimReturn (dict of named tables)
    """
    # TODO: In future, handle bundle loading here
    # For MVP, assume simulation code is pre-installed on workers
    
    module_name, func_name = fn_ref.split(":")
    mod = importlib.import_module(module_name)
    func = getattr(mod, func_name)
    
    # Call the simulation function
    result = func(params, seed)
    
    # Ensure result conforms to SimReturn type
    if not isinstance(result, dict):
        raise TypeError(f"Simulation must return dict, got {type(result)}")
    
    return result