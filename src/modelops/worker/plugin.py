"""ModelOps Dask WorkerPlugin implementation.

This is the composition root for ModelOps - all dependency injection 
from outside ports (modelops-bundle, 

"""

from pathlib import Path
from typing import Optional

from dask.distributed import WorkerPlugin
from modelops_contracts.ports import (
    ExecutionEnvironment, BundleRepository, CAS
)

from .config import RuntimeConfig


class ModelOpsWorkerPlugin(WorkerPlugin):
    """Dask WorkerPlugin for ModelOps simulation execution.
    
    This plugin is registered with the Dask client and installed on all workers.
    It sets up the execution environment once per worker and manages lifecycle.
    
    No singletons, no globals, no LRU tricks needed!
    """
    
    def __init__(self, config: Optional[RuntimeConfig] = None):
        """Initialize plugin with optional config override.
        
        Args:
            config: Runtime configuration. If None, loaded from environment.
        """
        self.config = config
    
    def setup(self, worker):
        """Setup hook called when plugin is installed on worker.
        
        This is where ALL wiring happens. Called ONCE per worker.
        
        Args:
            worker: The Dask worker instance
        """
        # Get configuration (from env or override)
        config = self.config or RuntimeConfig.from_env()
        config.validate()
        
        # Create CAS adapter
        cas = self._make_cas(config)
        
        # Create bundle repository
        bundle_repo = self._make_bundle_repository(config)
        
        # Create execution environment with its dependencies
        exec_env = self._make_execution_environment(config, bundle_repo, cas)
        
        # Create the core domain executor with single dependency
        from modelops.core.executor import SimulationExecutor
        executor = SimulationExecutor(exec_env)
        
        # Attach to worker for task access
        # This is the ONLY place we store runtime state
        worker.modelops_runtime = executor
        
        # Also store exec_env for clean shutdown
        worker.modelops_exec_env = exec_env
        
        print(f"ModelOps runtime initialized on worker {worker.id}")
    
    def teardown(self, worker):
        """Teardown hook called when worker is shutting down.
        
        Clean shutdown of all resources.
        
        Args:
            worker: The Dask worker instance
        """
        print(f"Shutting down ModelOps runtime on worker {worker.id}")
        
        # Clean shutdown via the runtime (which delegates to exec_env)
        if hasattr(worker, 'modelops_runtime'):
            try:
                worker.modelops_runtime.shutdown()
            finally:
                delattr(worker, 'modelops_runtime')
    
    def _make_cas(self, config: RuntimeConfig) -> CAS:
        """Instantiate the appropriate CAS adapter.
        
        Args:
            config: Runtime configuration
            
        Returns:
            CAS implementation based on config
        """
        # TODO: should be an interface here, not a branch
        # or, a to_cas() method on config.
        if config.cas_backend == 'azure':
            from modelops.adapters.cas.azure_cas import AzureCAS
            return AzureCAS(
                container=config.cas_bucket,
                prefix=config.cas_prefix,
                storage_account=config.azure_storage_account
            )
        elif config.cas_backend == 'memory':
            from modelops.adapters.cas.memory_cas import MemoryCAS
            return MemoryCAS()
        else:
            raise ValueError(f"Unknown CAS backend: {config.cas_backend}")
    
    def _make_bundle_repository(self, config: RuntimeConfig) -> BundleRepository:
        """Instantiate the appropriate bundle repository adapter.
        
        NO implicit defaults - bundle references must be explicit for reproducibility.
        
        Args:
            config: Runtime configuration
            
        Returns:
            BundleRepository implementation based on config
        """
        if config.bundle_source == 'oci':
            # Use entry point discovery for OCI bundle repositories
            from importlib.metadata import entry_points
            
            # STRICT: registry must be specified
            if not config.bundle_registry:
                raise ValueError("bundle_registry must be specified for OCI source")
            
            # Discover OCI bundle repository via entry points
            eps = entry_points(group="modelops.bundle_repos")
            oci_plugin = None
            for ep in eps:
                if ep.name == "oci":
                    oci_plugin = ep
                    break
            
            if not oci_plugin:
                raise ValueError(
                    "No OCI bundle repository plugin found. "
                    "Ensure modelops-bundle is installed with the 'oci' entry point."
                )
            
            # Load and instantiate the OCI repository
            repo_class = oci_plugin.load()
            return repo_class(
                registry_ref=config.bundle_registry,  # e.g., "ghcr.io/org/models"
                cache_dir=str(Path(config.bundles_cache_dir)),  # Convert Path to str for compatibility
                cache_structure="digest_short",  # Use short digest for cache dirs
                default_tag="latest"  # Default tag if not specified in ref
            )
        elif config.bundle_source == 'file':
            # Simple filesystem for local development
            from modelops.adapters.bundle.file_repo import FileBundleRepository
            
            if not config.bundles_dir:
                raise ValueError("bundles_dir must be specified for file source")
                
            return FileBundleRepository(
                bundles_dir=config.bundles_dir,
                cache_dir=config.bundles_cache_dir
            )
        else:
            raise ValueError(f"Unknown bundle source: {config.bundle_source}")
    
    def _make_execution_environment(
        self,
        config: RuntimeConfig,
        bundle_repo: BundleRepository,
        cas: CAS
    ) -> ExecutionEnvironment:
        """Instantiate the appropriate execution environment.
        
        Args:
            config: Runtime configuration
            bundle_repo: Bundle repository for fetching code
            cas: CAS for storing outputs
            
        Returns:
            ExecutionEnvironment implementation based on config
        """
        if config.executor_type == 'isolated_warm':
            from modelops.adapters.exec_env.isolated_warm import IsolatedWarmExecEnv
            return IsolatedWarmExecEnv(
                bundle_repo=bundle_repo,
                cas=cas,
                venvs_dir=Path(config.venvs_dir),
                mem_limit_bytes=config.mem_limit_bytes,
                max_warm_processes=config.max_warm_processes,
                inline_artifact_max_bytes=config.inline_artifact_max_bytes,
                force_fresh_venv=config.force_fresh_venv
            )
        elif config.executor_type == 'direct':
            # Simple in-process execution for testing
            from modelops.adapters.exec_env.direct import DirectExecEnv
            return DirectExecEnv(
                bundle_repo=bundle_repo,
                cas=cas
            )
        else:
            raise ValueError(f"Unknown executor type: {config.executor_type}")
