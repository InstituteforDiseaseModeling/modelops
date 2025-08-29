"""Registry for infrastructure providers."""

from typing import Dict, Type, List
from .base import WorkspaceProvider
from .orbstack import OrbStackProvider
from .azure import AzureProvider


class ProviderRegistry:
    """Registry of available infrastructure providers.
    
    This centralizes provider discovery and instantiation,
    making it easy to add new providers without changing CLI code.
    """
    
    # Map of provider names to their implementation classes
    _providers: Dict[str, Type[WorkspaceProvider]] = {
        "orbstack": OrbStackProvider,
        "azure": AzureProvider,
    }
    
    @classmethod
    def get(cls, name: str) -> WorkspaceProvider:
        """Get a configured provider instance by name.
        
        This loads the provider configuration from the standard location
        (~/.modelops/providers/{name}.yaml) and returns an initialized instance.
        
        Args:
            name: Provider name (e.g., "orbstack", "azure")
            
        Returns:
            Configured provider instance
            
        Raises:
            ValueError: If provider name is unknown
            ValueError: If provider config file doesn't exist
        """
        if name not in cls._providers:
            available = ", ".join(cls._providers.keys())
            raise ValueError(
                f"Unknown provider '{name}'. Available providers: {available}"
            )
        
        # Get the provider class
        provider_class = cls._providers[name]
        
        # Load configuration from file
        # This will raise if config file doesn't exist
        return provider_class.from_config_file(name)
    
    @classmethod
    def list_available(cls) -> List[str]:
        """List all available provider types.
        
        Returns:
            List of provider names that can be used
        """
        return list(cls._providers.keys())
    
    @classmethod
    def register(cls, name: str, provider_class: Type[WorkspaceProvider]) -> None:
        """Register a new provider type.
        
        This allows extending the registry with custom providers
        without modifying this file.
        
        Args:
            name: Provider name
            provider_class: Provider implementation class
        """
        cls._providers[name] = provider_class
    
    @classmethod
    def is_available(cls, name: str) -> bool:
        """Check if a provider is available.
        
        Args:
            name: Provider name to check
            
        Returns:
            True if provider is registered
        """
        return name in cls._providers