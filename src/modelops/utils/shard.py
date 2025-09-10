"""Utility for sharding paths for filesystem storage.

This module provides utilities for converting digests/hashes into
sharded filesystem paths to avoid too many files in a single directory.

TODO: audit / need?
"""

from modelops_contracts.errors import ContractViolationError


def shard(digest: str, depth: int = 2, width: int = 2) -> str:
    """Convert digest to sharded filesystem path.
    
    Examples:
        shard("abcdef123456...") -> "ab/cd/abcdef123456..."
        shard("abcdef123456...", depth=3, width=2) -> "ab/cd/ef/abcdef123456..."
    
    Args:
        digest: Hex digest string
        depth: Number of shard levels
        width: Characters per shard level
    
    Returns:
        Sharded path string
    """
    if len(digest) < depth * width:
        raise ContractViolationError(
            f"Digest too short for sharding: need at least {depth * width} chars, got {len(digest)}"
        )
    
    parts = []
    for i in range(depth):
        start = i * width
        parts.append(digest[start:start + width])
    parts.append(digest)
    
    return "/".join(parts)


__all__ = ["shard"]
