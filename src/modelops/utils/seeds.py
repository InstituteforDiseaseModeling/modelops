"""Seed generation utilities for deterministic replication.

This module provides functions to generate deterministic seeds
that comply with the modelops-contracts uint64 requirement.
"""

import hashlib
from typing import List


def derive_replicate_seeds(param_id: str, n_replicates: int) -> List[int]:
    """Derive deterministic seeds for replicates from a parameter ID.
    
    Uses BLAKE2b hashing to generate seeds that are:
    - Deterministic: same param_id always produces same seeds
    - Within uint64 range as required by contracts
    - Unique for each replicate index
    
    Args:
        param_id: The unique parameter set ID
        n_replicates: Number of replicate seeds to generate
        
    Returns:
        List of seeds, each within uint64 range (0 to 2^64-1)
        
    Example:
        >>> seeds = derive_replicate_seeds("abc123", 3)
        >>> len(seeds)
        3
        >>> all(0 <= s < 2**64 for s in seeds)
        True
    """
    seeds = []
    for i in range(n_replicates):
        # Create unique string for this param_id and replicate
        seed_str = f"contracts:seed:v1|{param_id}:{i}"
        
        # Use BLAKE2b with 8-byte output for uint64 range
        seed_bytes = hashlib.blake2b(
            seed_str.encode('utf-8'), 
            digest_size=8
        ).digest()
        
        # Convert to integer within uint64 range
        seed = int.from_bytes(seed_bytes, 'little') & ((1 << 64) - 1)
        seeds.append(seed)
    
    return seeds


def derive_single_seed(param_id: str, replicate_index: int = 0) -> int:
    """Derive a single deterministic seed from parameter ID.
    
    Convenience function for when you need just one seed.
    
    Args:
        param_id: The unique parameter set ID
        replicate_index: Which replicate this is (default 0)
        
    Returns:
        Seed within uint64 range
    """
    return derive_replicate_seeds(param_id, replicate_index + 1)[replicate_index]