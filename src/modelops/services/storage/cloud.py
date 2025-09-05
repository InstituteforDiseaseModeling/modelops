"""Cloud-agnostic base class for blob storage backends."""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
import json


class CloudBlobBackend(ABC):
    """Base cloud storage backend with provider-agnostic interface.
    
    Subclasses implement provider-specific methods prefixed with
    _<provider>_ (e.g., _azure_*, _aws_*, _gcp_*).
    
    This design allows for clean separation of cloud-specific logic
    while maintaining a common interface.
    """
    
    def __init__(self, container: str = "cache", 
                 config: Optional[Dict[str, Any]] = None):
        """Initialize cloud backend.
        
        Args:
            container: Container/bucket name
            config: Provider-specific configuration
        """
        self.container = container
        self.config = config or {}
        self.provider = self._detect_provider()
        self._initialize_client()
    
    @abstractmethod
    def _detect_provider(self) -> str:
        """Detect cloud provider from environment.
        
        Returns:
            Provider name (e.g., "azure", "aws", "gcp")
            
        Raises:
            ValueError: If provider cannot be detected
        """
        ...
    
    @abstractmethod
    def _initialize_client(self) -> None:
        """Initialize provider-specific client.
        
        This should set up the client connection and ensure
        the container/bucket exists.
        """
        ...
    
    # Common interface methods delegate to provider-specific implementations
    
    def exists(self, key: str) -> bool:
        """Check if blob exists."""
        method = getattr(self, f"_{self.provider}_exists")
        return method(key)
    
    def load(self, key: str) -> bytes:
        """Load blob data."""
        method = getattr(self, f"_{self.provider}_load")
        return method(key)
    
    def save(self, key: str, data: bytes) -> None:
        """Save data to blob."""
        method = getattr(self, f"_{self.provider}_save")
        return method(key, data)
    
    def delete(self, key: str) -> None:
        """Delete blob."""
        method = getattr(self, f"_{self.provider}_delete")
        return method(key)
    
    def list_keys(self, prefix: str) -> list[str]:
        """List all keys with prefix."""
        method = getattr(self, f"_{self.provider}_list_keys")
        return method(prefix)
    
    # Convenience methods
    
    def save_json(self, key: str, data: dict) -> None:
        """Save JSON data to blob."""
        json_bytes = json.dumps(data, indent=2).encode('utf-8')
        self.save(key, json_bytes)
    
    def load_json(self, key: str) -> dict:
        """Load JSON data from blob."""
        return json.loads(self.load(key).decode('utf-8'))
    
    def ensure_container(self) -> None:
        """Ensure the container/bucket exists.
        
        Override this in subclasses to handle container creation.
        """
        pass