"""Runtime configuration for ModelOps workers."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class RuntimeConfig:
    """Configuration for ModelOps runtime on workers.
    
    This configuration is loaded from environment variables on each worker.
    It controls how the worker sets up its execution environment.
    """
    
    # Bundle configuration
    bundle_source: str = "oci"  # "oci" or "file"
    bundle_registry: Optional[str] = None  # e.g., "ghcr.io/org/models"
    bundles_cache_dir: str = "/tmp/modelops/bundles"
    bundles_dir: Optional[str] = None  # For file source
    
    # CAS configuration  
    cas_backend: str = "memory"  # "memory", "azure"
    cas_bucket: Optional[str] = None
    cas_prefix: str = "modelops/artifacts"
    cas_region: Optional[str] = None
    azure_storage_account: Optional[str] = None
    
    # Execution environment
    executor_type: str = "isolated_warm"  # "isolated_warm", "direct"
    venvs_dir: str = "/tmp/modelops/venvs"
    max_warm_processes: int = 128
    mem_limit_bytes: Optional[int] = None
    inline_artifact_max_bytes: int = 64_000  # Artifacts smaller than this are inlined
    
    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        """Load configuration from environment variables.
        
        Environment variables are prefixed with MODELOPS_.
        For example:
        - MODELOPS_BUNDLE_SOURCE -> bundle_source
        - MODELOPS_BUNDLE_REGISTRY -> bundle_registry
        - MODELOPS_CAS_BACKEND -> cas_backend
        """
        config = cls()
        
        # Bundle configuration
        config.bundle_source = os.environ.get("MODELOPS_BUNDLE_SOURCE", config.bundle_source)
        config.bundle_registry = os.environ.get("MODELOPS_BUNDLE_REGISTRY", config.bundle_registry)
        config.bundles_cache_dir = os.environ.get("MODELOPS_BUNDLES_CACHE_DIR", config.bundles_cache_dir)
        config.bundles_dir = os.environ.get("MODELOPS_BUNDLES_DIR", config.bundles_dir)
        
        # CAS configuration
        config.cas_backend = os.environ.get("MODELOPS_CAS_BACKEND", config.cas_backend)
        config.cas_bucket = os.environ.get("MODELOPS_CAS_BUCKET", config.cas_bucket)
        config.cas_prefix = os.environ.get("MODELOPS_CAS_PREFIX", config.cas_prefix)
        config.cas_region = os.environ.get("MODELOPS_CAS_REGION", config.cas_region)
        config.azure_storage_account = os.environ.get("MODELOPS_AZURE_STORAGE_ACCOUNT", config.azure_storage_account)
        
        # Execution environment
        config.executor_type = os.environ.get("MODELOPS_EXECUTOR_TYPE", config.executor_type)
        config.venvs_dir = os.environ.get("MODELOPS_VENVS_DIR", config.venvs_dir)
        config.max_warm_processes = int(os.environ.get("MODELOPS_MAX_WARM_PROCESSES", config.max_warm_processes))
        config.inline_artifact_max_bytes = int(os.environ.get("MODELOPS_INLINE_ARTIFACT_MAX_BYTES", config.inline_artifact_max_bytes))
        
        mem_limit = os.environ.get("MODELOPS_MEM_LIMIT_BYTES")
        if mem_limit:
            config.mem_limit_bytes = int(mem_limit)
        
        return config
    
    def validate(self) -> None:
        """Validate configuration.
        
        Raises:
            ValueError: If configuration is invalid
        """
        # Bundle source validation
        if self.bundle_source == "oci" and not self.bundle_registry:
            raise ValueError("bundle_registry must be specified for OCI source")
        
        if self.bundle_source == "file" and not self.bundles_dir:
            raise ValueError("bundles_dir must be specified for file source")
        
        if self.bundle_source not in ["oci", "file"]:
            raise ValueError(f"Invalid bundle_source: {self.bundle_source}")
        
        # CAS backend validation
        if self.cas_backend == "azure":
            if not self.cas_bucket:
                raise ValueError("cas_bucket must be specified for Azure backend")
            if not self.azure_storage_account:
                raise ValueError("azure_storage_account must be specified for Azure backend")
        
        if self.cas_backend not in ["memory", "azure"]:
            raise ValueError(f"Invalid cas_backend: {self.cas_backend}")
        
        # Executor type validation
        if self.executor_type not in ["isolated_warm", "direct"]:
            raise ValueError(f"Invalid executor_type: {self.executor_type}")