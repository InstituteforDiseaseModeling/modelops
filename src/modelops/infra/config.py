"""Configuration models for infrastructure provisioning."""

from dataclasses import dataclass
from typing import Dict, Any, Optional
from ..versions import DASK_IMAGE


@dataclass
class WorkspaceConfig:
    """Configuration for a Dask workspace.
    
    This contains all the parameters needed to provision a Dask cluster
    on Kubernetes, including resource requirements and scaling settings.
    """
    name: str
    namespace: str
    min_workers: int = 1
    max_workers: int = 10  # For future HPA support
    worker_memory: str = "512Mi"
    worker_cpu: str = "0.5"
    scheduler_memory: str = "512Mi"
    scheduler_cpu: str = "0.5"
    image: str = DASK_IMAGE
    
    @classmethod
    def from_cli_args(cls, 
                      name: str,
                      provider_defaults: Optional[Dict[str, Any]] = None,
                      min_workers: Optional[int] = None,
                      max_workers: Optional[int] = None,
                      **overrides) -> 'WorkspaceConfig':
        """Create config from CLI arguments with provider defaults.
        
        Priority order:
        1. Explicit CLI arguments (if provided)
        2. Provider defaults (if available)
        3. Class defaults
        
        Args:
            name: Workspace name
            provider_defaults: Provider-specific resource defaults
            min_workers: Override minimum number of worker pods
            max_workers: Override maximum number of worker pods
            **overrides: Additional overrides (future: memory, cpu)
            
        Returns:
            WorkspaceConfig instance with appropriate defaults
        """
        # Start with provider defaults or empty dict
        config_args = provider_defaults.copy() if provider_defaults else {}
        
        # Apply explicit CLI overrides (these take precedence)
        if min_workers is not None:
            config_args['min_workers'] = min_workers
        if max_workers is not None:
            config_args['max_workers'] = max_workers
            
        # Apply any additional overrides
        config_args.update(overrides)
        
        # Always set name and namespace (unique per workspace)
        config_args['name'] = name
        config_args.setdefault('namespace', f"modelops-{name}")
        
        return cls(**config_args)
    
    def validate(self) -> None:
        """Validate configuration values.
        
        Raises:
            ValueError: If configuration is invalid
        """
        if self.min_workers < 1:
            raise ValueError("min_workers must be at least 1")
        
        if self.max_workers < self.min_workers:
            raise ValueError("max_workers must be >= min_workers")
        
        if not self.name:
            raise ValueError("Workspace name is required")
        
        # Validate Kubernetes resource format
        for resource, value in [
            ("worker_memory", self.worker_memory),
            ("scheduler_memory", self.scheduler_memory)
        ]:
            if not value.endswith(("Mi", "Gi")):
                raise ValueError(f"{resource} must end with Mi or Gi (e.g., '4Gi')")