"""Base provider interface for workspace infrastructure."""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from pathlib import Path
import yaml


class WorkspaceProvider(ABC):
    """Abstract provider for workspace infrastructure.
    
    Providers handle cloud-specific resources like storage, networking,
    and Kubernetes access. Each provider loads its configuration from
    a YAML file and handles its own authentication.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize provider with configuration.
        
        Args:
            config: Provider configuration dictionary
        """
        self.config = config
        self.provider_type = config.get('provider', 'unknown')
    
    @classmethod
    def from_config_file(cls, name: str) -> 'WorkspaceProvider':
        """Load provider from configuration file.
        
        Config files are stored in ~/.modelops/providers/{name}.yaml
        
        Args:
            name: Provider name (e.g., "azure", "local")
            
        Returns:
            Configured provider instance
            
        Raises:
            ValueError: If config file not found or invalid
        """
        config_dir = Path.home() / ".modelops" / "providers"
        config_path = config_dir / f"{name}.yaml"
        
        if not config_path.exists():
            # Provide helpful error message
            config_dir.mkdir(parents=True, exist_ok=True)
            example_path = config_dir / f"{name}.yaml.example"
            
            # Create example config
            example_config = cls._get_example_config(name)
            with open(example_path, 'w') as f:
                yaml.dump(example_config, f, default_flow_style=False)
            
            raise ValueError(
                f"No config found for provider '{name}'.\n"
                f"Create {config_path} based on {example_path}\n"
                f"Or run: mops provider init {name}"
            )
        
        # Load and validate config
        with open(config_path) as f:
            config = yaml.safe_load(f)
        
        # Validate provider type matches
        if config.get('provider') != name:
            raise ValueError(
                f"Config file provider type '{config.get('provider')}' "
                f"doesn't match requested provider '{name}'"
            )
        
        return cls(config)
    
    @staticmethod
    def _get_example_config(provider_type: str) -> Dict[str, Any]:
        """Get example configuration for a provider type.
        
        Args:
            provider_type: Provider type (azure, orbstack)
            
        Returns:
            Example configuration dictionary
        """
        examples = {
            "azure": {
                "kind": "Provider",
                "provider": "azure",
                "spec": {
                    "subscription_id": "YOUR_SUBSCRIPTION_ID",
                    "resource_group": "modelops-rg",
                    "location": "eastus",
                    "aks_cluster": "modelops-aks"
                },
                "auth": {
                    "method": "cli",  # or "service_principal", "managed_identity"
                    # Note: Don't put secrets here! Use environment variables
                }
            },
            "orbstack": {
                "kind": "Provider",
                "provider": "orbstack",
                "spec": {
                    "context": "orbstack",
                    "storage": {
                        "type": "emptydir"
                    }
                }
            }
        }
        
        return examples.get(provider_type, {
            "kind": "Provider",
            "provider": provider_type,
            "spec": {},
            "auth": {}
        })
    
    @abstractmethod
    def validate(self) -> None:
        """Validate provider configuration and credentials.
        
        Should check that:
        - Required configuration fields are present
        - Credentials are available and valid
        - Cloud resources (if any) are accessible
        
        Raises:
            ValueError: If configuration is invalid
            RuntimeError: If credentials are invalid or missing
        """
        pass
    
    @abstractmethod
    def get_k8s_provider(self) -> Optional[Any]:
        """Get Pulumi Kubernetes provider for this cloud.
        
        Returns:
            Pulumi K8s provider configured for this cloud,
            or None for local providers
        """
        pass
    
    @abstractmethod
    def setup_storage(self) -> Dict[str, Any]:
        """Setup cloud storage for artifacts.
        
        Creates necessary storage resources (buckets, containers, etc.)
        and returns connection information.
        
        Returns:
            Dictionary with storage configuration:
            - connection_string or credentials
            - bucket/container names
            - any other provider-specific info
        """
        pass
    
    @abstractmethod
    def get_storage_secret_data(self) -> Dict[str, str]:
        """Get secret data for storage access.
        
        Returns data to be stored in a Kubernetes secret for
        pod access to storage.
        
        Returns:
            Dictionary of secret key -> value pairs
        """
        pass
    
    def get_resource_defaults(self) -> Dict[str, Any]:
        """Get provider-specific resource defaults.
        
        Override this in subclasses to provide appropriate defaults
        for the target environment (local vs cloud).
        
        Returns:
            Dictionary with optional keys:
            - min_workers: Minimum number of worker pods
            - max_workers: Maximum number of worker pods
            - worker_memory: Worker memory (e.g., "512Mi", "4Gi")
            - worker_cpu: Worker CPU (e.g., "0.5", "2")
            - scheduler_memory: Scheduler memory
            - scheduler_cpu: Scheduler CPU
        """
        return {}
    
    def get_labels(self) -> Dict[str, str]:
        """Get standard labels for resources.
        
        Returns:
            Dictionary of labels to apply to all resources
        """
        return {
            "app.kubernetes.io/managed-by": "modelops",
            "modelops.io/provider": self.provider_type
        }
    
    def __repr__(self) -> str:
        """String representation of provider."""
        return f"{self.__class__.__name__}(provider={self.provider_type})"