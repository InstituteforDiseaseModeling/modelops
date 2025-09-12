"""Core domain executor for simulation tasks.

This is the application service layer - the seam between primary
adapters (Dask) and secondary adapters (ExecutionEnvironment).
"""

from modelops_contracts import SimTask, SimReturn
from modelops_contracts.ports import ExecutionEnvironment


class SimulationExecutor:
    """Application service layer for simulation execution.
    
    This is the seam between:
    - Inbound: Primary adapters (DaskSimulationService)
    - Outbound: Secondary adapters (ExecutionEnvironment)
    
    Even though thin now, this is where domain logic belongs:
    - Validation & normalization (coming soon)
    - Fingerprinting & caching (future)
    - Policy & routing decisions (future)
    - Observability & metrics (future)
    
    Infrastructure concerns stay in ExecutionEnvironment.
    The executor used to have bundle_repo and cas dependencies too,
    but those are infrastructure concerns that belong in ExecutionEnvironment.
    """
    
    def __init__(self, exec_env: ExecutionEnvironment):
        """Initialize with single dependency.
        
        Args:
            exec_env: Execution environment for running simulations
        """
        self.exec_env = exec_env
    
    def execute(self, task: SimTask) -> SimReturn:
        """Execute a simulation task.
        
        Currently just delegates to ExecutionEnvironment, but this
        is where we'll add domain logic:
        - Parameter validation
        - Result caching
        - Routing logic
        - Metrics collection
        
        Args:
            task: Simulation task to execute
            
        Returns:
            SimReturn with outputs and metadata
        """
        # Future: self._validate(task)
        # Future: if cached := self._check_cache(task): return cached
        # Future: with metrics.timer("simulation.execute"):
        
        return self.exec_env.run(task)
    
    def shutdown(self):
        """Clean shutdown of executor.
        
        Ensures all resources are properly released.
        """
        if hasattr(self.exec_env, 'shutdown'):
            self.exec_env.shutdown()