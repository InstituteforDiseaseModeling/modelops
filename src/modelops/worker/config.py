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
    bundle_insecure: bool = False  # Use HTTP instead of HTTPS (for local dev)
    
    # Storage configuration
    upload_to_azure: bool = False  # Enable automatic upload to Azure blob storage
    azure_storage_account: Optional[str] = None
    azure_container: str = "results"
    azure_connection_string: Optional[str] = None  # Optional explicit connection string
    
    # Execution environment
    executor_type: str = "isolated_warm"  # "isolated_warm", "direct"
    venvs_dir: str = "/tmp/modelops/venvs"
    storage_dir: str = "/tmp/modelops/provenance"  # Provenance storage location
    max_warm_processes: int = 128
    mem_limit_bytes: Optional[int] = None
    inline_artifact_max_bytes: int = 64_000  # Artifacts smaller than this are inlined
    
    # Process pool configuration
    force_fresh_venv: bool = False  # Never reuse venvs (for debugging)
    validate_deps_on_reuse: bool = True  # Check deps haven't changed
    max_process_reuse_count: int = 1000  # Restart after N uses (future)
    process_ttl_seconds: int = 3600  # Max age before restart (future)
    
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
        config.bundle_insecure = os.environ.get("MODELOPS_BUNDLE_INSECURE", "false").lower() == "true"
        
        # Storage configuration
        config.upload_to_azure = os.environ.get("MODELOPS_UPLOAD_TO_AZURE", "false").lower() == "true"
        config.azure_storage_account = os.environ.get("MODELOPS_AZURE_STORAGE_ACCOUNT", config.azure_storage_account)
        config.azure_container = os.environ.get("MODELOPS_AZURE_CONTAINER", config.azure_container)
        config.azure_connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", config.azure_connection_string)
        
        # Execution environment
        config.executor_type = os.environ.get("MODELOPS_EXECUTOR_TYPE", config.executor_type)
        config.venvs_dir = os.environ.get("MODELOPS_VENVS_DIR", config.venvs_dir)
        config.storage_dir = os.environ.get("MODELOPS_STORAGE_DIR", config.storage_dir)
        config.max_warm_processes = int(os.environ.get("MODELOPS_MAX_WARM_PROCESSES", config.max_warm_processes))
        config.inline_artifact_max_bytes = int(os.environ.get("MODELOPS_INLINE_ARTIFACT_MAX_BYTES", config.inline_artifact_max_bytes))
        
        mem_limit = os.environ.get("MODELOPS_MEM_LIMIT_BYTES")
        if mem_limit:
            config.mem_limit_bytes = int(mem_limit)
        
        # Process pool configuration
        config.force_fresh_venv = os.environ.get("MODELOPS_FORCE_FRESH_VENV", "false").lower() == "true"
        config.validate_deps_on_reuse = os.environ.get("MODELOPS_VALIDATE_DEPS", "true").lower() == "true"
        config.max_process_reuse_count = int(os.environ.get("MODELOPS_MAX_PROCESS_REUSE", config.max_process_reuse_count))
        config.process_ttl_seconds = int(os.environ.get("MODELOPS_PROCESS_TTL", config.process_ttl_seconds))
        
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
        
        # Storage validation
        if self.upload_to_azure:
            # Need either connection string OR storage account
            if not self.azure_connection_string and not self.azure_storage_account:
                raise ValueError(
                    "When upload_to_azure is enabled, must provide either:\n"
                    "  - AZURE_STORAGE_CONNECTION_STRING environment variable\n"
                    "  - MODELOPS_AZURE_STORAGE_ACCOUNT environment variable"
                )
        
        # Executor type validation
        if self.executor_type not in ["isolated_warm", "direct"]:
            raise ValueError(f"Invalid executor_type: {self.executor_type}")