"""Test bundle digest computation for integration tests.

IMPORTANT: This module is for TEST BUNDLES ONLY, not production bundles!

Production bundles use modelops-bundle's hashing.py for proper OCI/ORAS
artifact digests. This utility provides deterministic hashing for local
test directories to enable consistent testing across different environments
(local dev, CI, etc).

The key difference from production hashing:
- This hashes entire directory structures deterministically
- Production uses modelops-bundle for individual files and manifests
- This ignores timestamps and ephemeral files for reproducibility
- Production preserves all metadata needed for provenance

DO NOT use this for production bundle creation or verification!
"""

from pathlib import Path
import hashlib
import os
import stat
from typing import Set

# Files and directories to ignore for deterministic hashing
DEFAULT_IGNORES: Set[str] = {
    "__pycache__",
    ".git",
    ".DS_Store",
    ".pytest_cache",
    "*.pyc",
    "*.pyo",
    ".venv",
    "venv",
    ".env",
}


def _should_skip(path: Path) -> bool:
    """Check if a path should be skipped during hashing.

    Args:
        path: Path to check

    Returns:
        True if path should be skipped
    """
    name = path.name

    # Check exact matches
    if name in DEFAULT_IGNORES:
        return True

    # Check patterns
    if name.endswith((".pyc", ".pyo", ".swp", ".bak", "~")):
        return True

    # Skip hidden files except .gitignore which might affect bundle behavior
    if name.startswith(".") and name != ".gitignore":
        return True

    return False


def compute_test_bundle_digest(root: Path, include_mode: bool = False) -> str:
    """Compute deterministic SHA256 digest of a test bundle directory.

    This function computes a reproducible digest for test bundles by:
    - Sorting all files deterministically
    - Hashing only relative paths and file contents
    - Ignoring timestamps and other ephemeral metadata
    - Skipping temporary/cache files

    Args:
        root: Root directory of the test bundle
        include_mode: Whether to include file permissions (usually False for tests)

    Returns:
        64-character hex SHA256 digest (without "sha256:" prefix)

    Note:
        This is NOT compatible with modelops-bundle digests!
        Use only for test fixture resolution.
    """
    root = root.resolve()
    h = hashlib.sha256()

    # Add a marker to distinguish test digests from production digests
    h.update(b"TEST_BUNDLE_DIGEST_V1\0")

    # Walk the directory tree in sorted order
    for path in sorted(root.rglob("*")):
        if _should_skip(path):
            continue

        rel_path = path.relative_to(root).as_posix()

        # Handle symlinks
        if path.is_symlink():
            h.update(b"SYMLINK\0")
            h.update(rel_path.encode())
            h.update(b"\0")
            # Record where the symlink points
            try:
                target = os.readlink(path)
                h.update(target.encode())
            except OSError:
                h.update(b"BROKEN")
            h.update(b"\n")
            continue

        # Handle regular files
        if path.is_file():
            h.update(b"FILE\0")
            h.update(rel_path.encode())
            h.update(b"\0")

            # Optionally include file mode (executable bit)
            if include_mode:
                try:
                    mode = stat.S_IMODE(path.stat().st_mode)
                    h.update(str(mode).encode())
                    h.update(b"\0")
                except OSError:
                    pass

            # Hash file contents
            try:
                with path.open("rb") as f:
                    # Read in chunks for memory efficiency
                    while chunk := f.read(8192):
                        h.update(chunk)
            except (OSError, IOError):
                # File might have been deleted or be unreadable
                h.update(b"UNREADABLE")

            h.update(b"\n")

        # Handle directories (just record their existence)
        elif path.is_dir():
            h.update(b"DIR\0")
            h.update(rel_path.encode())
            h.update(b"\n")

    return h.hexdigest()


def format_test_bundle_ref(digest: str) -> str:
    """Format a test bundle digest as a bundle reference.

    Args:
        digest: 64-character hex digest from compute_test_bundle_digest

    Returns:
        Formatted bundle reference like "sha256:xxxxx"
    """
    if not digest or len(digest) != 64:
        raise ValueError(f"Invalid digest length: expected 64, got {len(digest)}")

    return f"sha256:{digest}"