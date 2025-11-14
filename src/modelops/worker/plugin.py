"""ModelOps Dask WorkerPlugin implementation.

This is the composition root for ModelOps - all dependency injection
from outside ports (modelops-bundle,

"""

from pathlib import Path

from dask.distributed import WorkerPlugin
from modelops_contracts.ports import BundleRepository, ExecutionEnvironment

from .config import RuntimeConfig


class ModelOpsWorkerPlugin(WorkerPlugin):
    """Dask WorkerPlugin for ModelOps simulation execution.

    This plugin is registered with the Dask client and installed on all workers.
    It sets up the execution environment once per worker and manages lifecycle.

    No singletons, no globals, no LRU tricks needed!
    """

    def __init__(self):
        """Initialize plugin.

        Workers create their own RuntimeConfig from environment variables.
        This ensures workers read THEIR env vars, not the runner's.
        """
        # No config stored - workers read their own environment in setup()
        pass

    def setup(self, worker):
        """Setup hook called when plugin is installed on worker.

        This is where ALL wiring happens. Called ONCE per worker.

        Args:
            worker: The Dask worker instance
        """
        # Workers ALWAYS read from their own environment
        # This ensures MODELOPS_EXECUTOR_TYPE is read from the worker pod, not the runner
        config = RuntimeConfig.from_env()
        config.validate()

        # Storage directory for provenance store
        storage_dir = Path(config.storage_dir or "/tmp/modelops/provenance")

        # Create bundle repository
        bundle_repo = self._make_bundle_repository(config)

        # Create execution environment with its dependencies
        exec_env = self._make_execution_environment(config, bundle_repo, storage_dir)

        # Create the core domain executor with single dependency
        from modelops.core.executor import SimulationExecutor

        executor = SimulationExecutor(exec_env)

        # Attach to worker for task access
        # This is the ONLY place we store runtime state
        worker.modelops_runtime = executor

        # Also store exec_env for clean shutdown
        worker.modelops_exec_env = exec_env

        print(f"ModelOps runtime initialized on worker {worker.id}")
        print(f"  Executor: {config.executor_type}")
        print(f"  Bundle source: {config.bundle_source}")
        if config.executor_type == "cold":
            print(f"  Fresh venv per task: {config.force_fresh_venv}")

    def teardown(self, worker):
        """Teardown hook called when worker is shutting down.

        Clean shutdown of all resources.

        Args:
            worker: The Dask worker instance
        """
        print(f"Shutting down ModelOps runtime on worker {worker.id}")

        # Clean shutdown via the runtime (which delegates to exec_env)
        if hasattr(worker, "modelops_runtime"):
            try:
                worker.modelops_runtime.shutdown()
            finally:
                delattr(worker, "modelops_runtime")

    def _make_bundle_repository(self, config: RuntimeConfig) -> BundleRepository:
        """Instantiate the appropriate bundle repository adapter.

        NO implicit defaults - bundle references must be explicit for reproducibility.

        Args:
            config: Runtime configuration

        Returns:
            BundleRepository implementation based on config
        """
        if config.bundle_source == "oci":
            # Use entry point discovery for OCI bundle repositories
            from importlib.metadata import entry_points

            # STRICT: registry must be specified
            if not config.bundle_registry:
                raise ValueError("bundle_registry must be specified for OCI source")

            # Discover ModelOps bundle repository via entry points
            eps = entry_points(group="modelops.bundle_repos")
            bundle_plugin = None
            for ep in eps:
                if ep.name == "modelops_bundle":
                    bundle_plugin = ep
                    break

            if not bundle_plugin:
                raise ValueError(
                    "No ModelOps bundle repository plugin found. "
                    "Ensure modelops-bundle is installed with the 'modelops_bundle' entry point."
                )

            # Load and instantiate the ModelOps bundle repository
            repo_class = bundle_plugin.load()
            return repo_class(
                registry_ref=config.bundle_registry,  # e.g., "ghcr.io/org/models"
                cache_dir=str(
                    Path(config.bundles_cache_dir)
                ),  # Convert Path to str for compatibility
                cache_structure="digest_short",  # Use short digest for cache dirs
                default_tag="latest",  # Default tag if not specified in ref
                insecure=config.bundle_insecure if hasattr(config, "bundle_insecure") else False,
            )
        elif config.bundle_source == "file":
            # Simple filesystem for local development
            from modelops.adapters.bundle.file_repo import FileBundleRepository

            if not config.bundles_dir:
                raise ValueError("bundles_dir must be specified for file source")

            return FileBundleRepository(
                bundles_dir=config.bundles_dir, cache_dir=config.bundles_cache_dir
            )
        else:
            raise ValueError(f"Unknown bundle source: {config.bundle_source}")

    def _make_execution_environment(
        self, config: RuntimeConfig, bundle_repo: BundleRepository, storage_dir: Path
    ) -> ExecutionEnvironment:
        """Instantiate the appropriate execution environment.

        Args:
            config: Runtime configuration
            bundle_repo: Bundle repository for fetching code
            storage_dir: Directory for provenance-based storage

        Returns:
            ExecutionEnvironment implementation based on config
        """
        # Create Azure backend configuration if uploads are enabled
        azure_backend = None
        if config.upload_to_azure:
            azure_config = {
                "container": config.azure_container,
                "connection_string": config.azure_connection_string,
            }
            # Only set azure_backend if upload is enabled
            azure_backend = azure_config

        if config.executor_type == "isolated_warm":
            from modelops.adapters.exec_env.isolated_warm import IsolatedWarmExecEnv

            return IsolatedWarmExecEnv(
                bundle_repo=bundle_repo,
                venvs_dir=Path(config.venvs_dir),
                storage_dir=storage_dir,
                mem_limit_bytes=config.mem_limit_bytes,
                max_warm_processes=config.max_warm_processes,
                force_fresh_venv=config.force_fresh_venv,
                azure_backend=azure_backend,
            )
        elif config.executor_type == "direct":
            # Simple in-process execution for testing
            from modelops.adapters.exec_env.direct import DirectExecEnv

            return DirectExecEnv(
                bundle_repo=bundle_repo,
                storage_dir=storage_dir,
                azure_backend=azure_backend,
            )
        elif config.executor_type == "cold":
            # Cold execution - fresh process per task for maximum isolation
            from modelops.adapters.exec_env.cold import ColdExecEnv

            return ColdExecEnv(
                bundle_repo=bundle_repo,
                venvs_dir=Path(config.venvs_dir),
                storage_dir=storage_dir,
                force_fresh_venv=config.force_fresh_venv,
                azure_backend=azure_backend,
            )
        else:
            raise ValueError(f"Unknown executor type: {config.executor_type}")
