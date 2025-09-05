"""Storage backend protocol for cloud-agnostic storage."""

from typing import Protocol, runtime_checkable
import json


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol for storage backends.
    
    Defines the contract that any storage implementation must follow.
    This enables swapping backends without changing client code.
    
    Implementations include:
    - AzureBlobBackend: Azure Blob Storage
    - LocalFileBackend: Local filesystem (for development)
    - Future: S3Backend, GCSBackend
    """
    
    def exists(self, key: str) -> bool:
        """Check if key exists in storage.
        
        Args:
            key: Storage key (e.g., "cache/param_id/seed_42")
            
        Returns:
            True if key exists, False otherwise
        """
        ...
    
    def load(self, key: str) -> bytes:
        """Load binary data from storage.
        
        Args:
            key: Storage key
            
        Returns:
            Binary data
            
        Raises:
            KeyError: If key doesn't exist
        """
        ...
    
    def save(self, key: str, data: bytes) -> None:
        """Save binary data to storage.
        
        Args:
            key: Storage key
            data: Binary data to save
        """
        ...
    
    def delete(self, key: str) -> None:
        """Delete key from storage.
        
        Args:
            key: Storage key to delete
            
        Raises:
            KeyError: If key doesn't exist
        """
        ...
    
    def list_keys(self, prefix: str) -> list[str]:
        """List all keys with given prefix.
        
        Args:
            prefix: Key prefix to filter by (e.g., "cache/param_id/")
            
        Returns:
            List of keys matching the prefix
        """
        ...
    
    # Optional convenience methods with default implementations
    def save_json(self, key: str, data: dict) -> None:
        """Save JSON data to storage.
        
        Args:
            key: Storage key
            data: Dictionary to save as JSON
        """
        json_bytes = json.dumps(data, indent=2).encode('utf-8')
        self.save(key, json_bytes)
    
    def load_json(self, key: str) -> dict:
        """Load JSON data from storage.
        
        Args:
            key: Storage key
            
        Returns:
            Parsed JSON dictionary
        """
        data = self.load(key)
        return json.loads(data.decode('utf-8'))