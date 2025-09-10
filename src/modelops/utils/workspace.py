"""Workspace digest computation for local development.

This module provides utilities for computing deterministic workspace digests
based on git state and dependencies. These digests are used for cache invalidation
in local development mode.

TODO: audit/needed? stale? 
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Optional


def get_git_tree_sha() -> Optional[str]:
    """Get git tree SHA if in a git repository.
    
    TODO(MVP): Implement proper git tree SHA extraction
    PLACEHOLDER: Returns None for MVP
    
    Future implementation:
    - Run `git rev-parse HEAD:` to get tree SHA
    - Handle cases where not in a git repo
    - Handle uncommitted changes (maybe add dirty flag)
    
    Returns:
        Git tree SHA string, or None if not in a git repo
    """
    # TODO(MVP): Replace with real implementation
    # try:
    #     result = subprocess.run(
    #         ['git', 'rev-parse', 'HEAD:'],
    #         capture_output=True,
    #         text=True,
    #         check=True
    #     )
    #     return result.stdout.strip()
    # except subprocess.CalledProcessError:
    #     return None
    
    # PLACEHOLDER: Return None for MVP
    return None


def get_uv_lock_hash() -> Optional[str]:
    """Get hash of uv.lock file if it exists.
    
    TODO(MVP): Implement proper uv.lock hashing
    PLACEHOLDER: Returns None for MVP
    
    Future implementation:
    - Check if uv.lock exists in current directory or parent dirs
    - Compute SHA256 hash of the file contents
    - Return hex digest
    
    Returns:
        SHA256 hash of uv.lock, or None if file doesn't exist
    """
    # TODO(MVP): Replace with real implementation
    # lock_path = Path('uv.lock')
    # if lock_path.exists():
    #     content = lock_path.read_bytes()
    #     return hashlib.sha256(content).hexdigest()
    # return None
    
    # PLACEHOLDER: Return None for MVP
    return None


def compute_workspace_digest() -> str:
    """Compute deterministic digest for local workspace.
    
    TODO(MVP): Implement proper workspace digest computation
    PLACEHOLDER: Returns all-zeros for MVP
    
    Future implementation:
    - Compute git tree SHA: git rev-parse HEAD:
    - Hash uv.lock file if exists
    - Combine: sha256(f"{git_tree_sha}\\n{uv_lock_hash}")[:12]
    - Return 12-character hex digest for use in EntryPointId
    
    Returns:
        12-character hex digest representing workspace state
    """
    # TODO(MVP): Replace with real implementation
    # git_tree_sha = get_git_tree_sha()
    # uv_lock_hash = get_uv_lock_hash()
    # 
    # if git_tree_sha and uv_lock_hash:
    #     # Combine both for full workspace identity
    #     combined = f"{git_tree_sha}\n{uv_lock_hash}"
    #     full_hash = hashlib.sha256(combined.encode()).hexdigest()
    #     return full_hash[:12]  # First 12 chars for EntryPointId
    # elif git_tree_sha:
    #     # Just git if no uv.lock
    #     return hashlib.sha256(git_tree_sha.encode()).hexdigest()[:12]
    # else:
    #     # Fallback to all-zeros if no git
    #     return "000000000000"
    
    # PLACEHOLDER: All-zeros digest for MVP
    return "000000000000"


def should_disable_cache_writes(digest: str) -> bool:
    """Check if cache writes should be disabled for this digest.
    
    TODO(MVP): Implement cache write policy
    PLACEHOLDER: Always allow cache writes for now
    
    Future implementation:
    - Disable cache writes when using all-zeros digest
    - Disable when workspace is dirty (uncommitted changes)
    - Make configurable via environment variable
    
    Args:
        digest: The workspace digest to check
        
    Returns:
        True if cache writes should be disabled, False otherwise
    """
    # TODO(MVP): Disable cache writes for all-zeros digest
    # if digest == "000000000000":
    #     return True
    # 
    # # Also check for dirty workspace
    # try:
    #     result = subprocess.run(
    #         ['git', 'status', '--porcelain'],
    #         capture_output=True,
    #         text=True,
    #         check=True
    #     )
    #     if result.stdout.strip():  # Any output means dirty
    #         return True
    # except subprocess.CalledProcessError:
    #     pass
    # 
    # return False
    
    # PLACEHOLDER: Always allow cache writes for MVP
    return False


__all__ = [
    "compute_workspace_digest",
    "should_disable_cache_writes",
    "get_git_tree_sha",
    "get_uv_lock_hash",
]
