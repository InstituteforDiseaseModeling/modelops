"""File-based bundle repository for local development."""

import fcntl
import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Tuple

from modelops_contracts.ports import BundleRepository

logger = logging.getLogger(__name__)


class FileBundleRepository:
    """Bundle repository using local filesystem.
    
    This adapter is for local development where bundles are
    already available on the filesystem (e.g., mounted volumes).
    """
    
    def __init__(self, bundles_dir: str, cache_dir: str):
        """Initialize the repository.
        
        Args:
            bundles_dir: Directory containing bundle directories
            cache_dir: Directory for cached/copied bundles
        """
        self.bundles_dir = Path(bundles_dir)
        self.cache_dir = Path(cache_dir)
        
        if not self.bundles_dir.exists():
            raise ValueError(f"Bundles directory does not exist: {bundles_dir}")
        
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def ensure_local(self, bundle_ref: str) -> Tuple[str, Path]:
        """Ensure bundle is available locally and return its digest and path.
        
        For file-based bundles, the reference format is:
        - "path/to/bundle" - relative path within bundles_dir
        - "/absolute/path/to/bundle" - absolute path (must exist)
        
        Args:
            bundle_ref: Bundle reference (path to bundle directory)
            
        Returns:
            Tuple of (digest, local_path) where digest is sha256 hex string
            
        Raises:
            ValueError: If bundle_ref is invalid or bundle not found
        """
        if not bundle_ref:
            raise ValueError("Bundle reference cannot be empty")
        
        # Handle special development bundle ref
        if bundle_ref == "local://dev":
            # Use current working directory for development
            source_path = Path(os.getcwd())
        elif bundle_ref.startswith("file://"):
            # Strip file:// prefix if present
            bundle_ref = bundle_ref[7:]
            source_path = Path(bundle_ref) if bundle_ref.startswith("/") else self.bundles_dir / bundle_ref
        elif bundle_ref.startswith("local://"):
            # Strip local:// prefix and treat as relative to bundles_dir
            bundle_ref = bundle_ref[8:]
            source_path = self.bundles_dir / bundle_ref
        elif bundle_ref.startswith("/"):
            # Absolute path
            source_path = Path(bundle_ref)
        else:
            # Relative to bundles_dir
            source_path = self.bundles_dir / bundle_ref
        
        # Check if path exists
        if not source_path.exists():
            raise ValueError(
                f"Bundle not found: {bundle_ref} "
                f"(resolved to {source_path})"
            )
        
        if not source_path.is_dir():
            raise ValueError(f"Bundle path is not a directory: {source_path}")
        
        # For local://dev, use working directory directly without caching
        if bundle_ref == "local://dev":
            # Use a fixed digest for dev bundle
            digest = "dev" + "0" * 60  # Make it 64 chars
            return digest, source_path
        
        # Compute digest of the source bundle
        digest = self._compute_digest(source_path)
        
        # Check if already cached
        cache_path = self.cache_dir / digest
        if cache_path.exists():
            logger.info(f"Bundle {digest[:12]} already cached at {cache_path}")
            return digest, cache_path
        
        # Use file locking to prevent concurrent caching
        lock_file = self.cache_dir / f".{digest}.lock"
        lock_file.touch()  # Ensure lock file exists
        
        with open(lock_file, 'r+') as lock:
            # Acquire exclusive lock
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                # Double-check after acquiring lock (another process may have cached it)
                if cache_path.exists():
                    logger.info(f"Bundle {digest[:12]} cached by another process")
                    return digest, cache_path
                
                # Copy bundle to cache atomically
                logger.info(f"Caching bundle from {source_path} to {cache_path}")
                
                # Use temp directory in same filesystem for atomic move
                temp_dir = tempfile.mkdtemp(dir=self.cache_dir, prefix=f".tmp_{digest[:8]}_")
                temp_path = Path(temp_dir) / "bundle"
                
                try:
                    shutil.copytree(source_path, temp_path)
                    # Atomic rename (on same filesystem)
                    os.replace(temp_path, cache_path)
                    # Clean up temp dir
                    os.rmdir(temp_dir)
                    return digest, cache_path
                except Exception as e:
                    # Clean up on failure
                    if Path(temp_dir).exists():
                        shutil.rmtree(temp_dir)
                    if cache_path.exists():
                        shutil.rmtree(cache_path)
                    raise ValueError(f"Failed to cache bundle: {e}")
            finally:
                # Release lock
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    
    def _compute_digest(self, path: Path) -> str:
        """Compute SHA256 digest of a directory.
        
        Args:
            path: Directory path
            
        Returns:
            SHA256 hex digest string
        """
        hasher = hashlib.sha256()
        
        # Sort files for deterministic ordering
        for file_path in sorted(path.rglob("*")):
            if file_path.is_file():
                # Include relative path in hash for structure
                rel_path = file_path.relative_to(path)
                hasher.update(str(rel_path).encode())
                
                # Include file metadata (size, mtime) for change detection
                stat = file_path.stat()
                hasher.update(str(stat.st_size).encode())
                hasher.update(str(int(stat.st_mtime)).encode())
                
                # Include file content
                with open(file_path, "rb") as f:
                    # Read in chunks to handle large files
                    while chunk := f.read(8192):
                        hasher.update(chunk)
        
        return hasher.hexdigest()
