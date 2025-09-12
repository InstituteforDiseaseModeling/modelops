"""In-memory CAS implementation for testing and development.

This is perfect for MVP - no external dependencies!
"""

import hashlib
from typing import Dict

from modelops_contracts.ports import CAS


class MemoryCAS:
    """Simple in-memory content-addressable storage.
    
    Stores data in a dictionary keyed by content hash.
    Perfect for testing and local development.
    
    Note: Data is lost when process exits!
    """
    
    def __init__(self):
        """Initialize empty storage."""
        self._storage: Dict[str, bytes] = {}
    
    def put(self, data: bytes, checksum_hex: str) -> str:
        """Store data in CAS.
        
        Args:
            data: Raw bytes to store
            checksum_hex: Expected SHA256 hex digest for verification
            
        Returns:
            Reference string for retrieving the data (the checksum)
            
        Raises:
            ValueError: If data doesn't match expected checksum
        """
        # Compute actual checksum
        actual = hashlib.sha256(data).hexdigest()
        
        # Verify it matches expected
        if actual != checksum_hex:
            raise ValueError(
                f"Checksum mismatch: expected {checksum_hex}, got {actual}"
            )
        
        # Store by checksum
        self._storage[checksum_hex] = data
        
        # Return the checksum as the reference
        return checksum_hex
    
    def get(self, ref: str) -> bytes:
        """Retrieve data from CAS.
        
        Args:
            ref: Reference returned from put() (the checksum)
            
        Returns:
            Raw bytes data
            
        Raises:
            KeyError: If reference not found
        """
        if ref not in self._storage:
            raise KeyError(f"CAS reference not found: {ref}")
        
        return self._storage[ref]
    
    def exists(self, ref: str) -> bool:
        """Check if a reference exists in CAS.
        
        Args:
            ref: Reference to check
            
        Returns:
            True if exists, False otherwise
        """
        return ref in self._storage
    
    def size(self) -> int:
        """Get number of objects in storage.
        
        Returns:
            Number of stored objects
        """
        return len(self._storage)
    
    def total_bytes(self) -> int:
        """Get total size of stored data.
        
        Returns:
            Total bytes stored
        """
        return sum(len(data) for data in self._storage.values())
    
    def clear(self) -> None:
        """Clear all stored data.
        
        Useful for testing.
        """
        self._storage.clear()