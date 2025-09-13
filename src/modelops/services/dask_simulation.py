"""Dask-based simulation service implementation."""

import logging
from typing import List, Optional

from dask.distributed import Client, Future as DaskFuture, get_worker
from modelops_contracts.ports import SimulationService, Future
from modelops_contracts import SimReturn, SimTask

from ..worker.plugin import ModelOpsWorkerPlugin
from ..worker.config import RuntimeConfig

logger = logging.getLogger(__name__)


def _worker_run_task(task: SimTask) -> SimReturn:
    """Execute task on worker using plugin-initialized runtime.
    
    This function runs on the Dask worker and uses the ModelOps runtime
    that was initialized by the WorkerPlugin.
    
    Args:
        task: Simulation task to execute
        
    Returns:
        Simulation result
    """
    worker = get_worker()
    
    if not hasattr(worker, 'modelops_runtime'):
        raise RuntimeError(
            "ModelOps runtime not initialized. "
            "Ensure ModelOpsWorkerPlugin is registered with the client."
        )
    
    return worker.modelops_runtime.execute(task)



class DaskFutureAdapter:
    """Adapt Dask Future to our Future protocol."""
    
    def __init__(self, dask_future: DaskFuture):
        self.wrapped = dask_future
    
    def result(self, timeout: Optional[float] = None) -> SimReturn:
        return self.wrapped.result(timeout=timeout)
    
    def done(self) -> bool:
        return self.wrapped.done()
    
    def cancel(self) -> bool:
        return self.wrapped.cancel()
    
    def exception(self) -> Optional[Exception]:
        return self.wrapped.exception()


class DaskSimulationService(SimulationService):
    """Dask-based implementation of SimulationService.
    
    This service submits simulation tasks to a Dask cluster and
    manages the WorkerPlugin lifecycle.
    """
    
    def __init__(self, 
                 client: Client,
                 config: Optional[RuntimeConfig] = None):
        """Initialize the service.
        
        Args:
            client: Dask client connected to a cluster
            config: Runtime configuration (uses env if not provided)
        """
        self.client = client
        self.config = config or RuntimeConfig.from_env()
        self._plugin_installed = False
        
        # Install the worker plugin
        self._install_plugin()
    
    def _install_plugin(self):
        """Install the ModelOps worker plugin on all workers."""
        if self._plugin_installed:
            return
        
        logger.info("Installing ModelOps worker plugin on all workers")
        
        # Create the plugin
        plugin = ModelOpsWorkerPlugin(self.config)
        
        # Register it with the cluster
        self.client.register_worker_plugin(plugin, name="modelops-runtime-v1")
        
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
            key=f"sim-{task.seed}-{task.bundle_ref[:12]}"  # For debugging
        )
        
        return DaskFutureAdapter(dask_future)
    
    def gather(self, futures: List[Future[SimReturn]]) -> List[SimReturn]:
        """Gather results from submitted tasks.
        
        Args:
            futures: List of futures from submit()
            
        Returns:
            List of simulation results in the same order as futures
        """
        # Extract Dask futures
        dask_futures = [f.wrapped for f in futures]
        
        # Gather all at once (preserves order)
        return self.client.gather(dask_futures)
    
    def submit_batch(self, tasks: List[SimTask]) -> List[Future[SimReturn]]:
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
            key=[f"sim-{t.seed}-{t.bundle_ref[:12]}" for t in tasks]
        )
        
        return [DaskFutureAdapter(f) for f in dask_futures]