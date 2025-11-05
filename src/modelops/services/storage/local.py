"""Local filesystem backend for development and testing."""

import json
import logging
from pathlib import Path

from ..storage_utils import atomic_write

logger = logging.getLogger(__name__)


class LocalFileBackend:
    """Local filesystem backend implementation.

    Stores data as files on the local filesystem. Useful for:
    - Development and testing
    - Small-scale experiments
    - Environments without cloud storage

    Note: This backend implements the StorageBackend protocol
    but doesn't inherit from CloudBlobBackend since it's not cloud-based.
    """

    def __init__(self, base_path: str = "/tmp/modelops_cache"):
        """Initialize local backend.

        Args:
            base_path: Base directory for storage
        """
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Initialized local storage at: {self.base_path}")

    def _get_path(self, key: str) -> Path:
        """Convert key to filesystem path.

        Args:
            key: Storage key (e.g., "cache/param_id/seed_42")

        Returns:
            Path object for the key
        """
        # Ensure path is safe (no .. or absolute paths)
        if ".." in key or Path(key).is_absolute():
            raise ValueError(f"Invalid key: {key}")

        path = self.base_path / key
        return path

    def exists(self, key: str) -> bool:
        """Check if file exists."""
        try:
            return self._get_path(key).exists()
        except Exception as e:
            logger.error(f"Error checking existence of {key}: {e}")
            return False

    def load(self, key: str) -> bytes:
        """Load file data."""
        try:
            path = self._get_path(key)
            if not path.exists():
                raise KeyError(f"Key not found: {key}")

            data = path.read_bytes()
            logger.debug(f"Loaded {len(data)} bytes from {key}")
            return data
        except KeyError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to load key '{key}': {e}")

    def save(self, key: str, data: bytes) -> None:
        """Save data to file atomically."""
        try:
            path = self._get_path(key)
            # Use atomic write to prevent corruption
            atomic_write(path, data)
            logger.debug(f"Saved {len(data)} bytes to {key}")
        except Exception as e:
            raise RuntimeError(f"Failed to save key '{key}': {e}")

    def delete(self, key: str) -> None:
        """Delete file."""
        try:
            path = self._get_path(key)
            if not path.exists():
                raise KeyError(f"Key not found: {key}")

            path.unlink()
            logger.debug(f"Deleted key: {key}")

            # Try to remove empty parent directories
            try:
                parent = path.parent
                while parent != self.base_path and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            except:
                pass  # Ignore errors when cleaning up directories

        except KeyError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to delete key '{key}': {e}")

    def list_keys(self, prefix: str) -> list[str]:
        """List all files with given prefix."""
        try:
            # Convert prefix to path
            prefix_path = self.base_path / prefix

            # Find all files under this prefix
            keys = []

            # If prefix is a directory, list all files under it
            if prefix_path.is_dir():
                for path in prefix_path.rglob("*"):
                    if path.is_file():
                        # Convert back to key format
                        relative_path = path.relative_to(self.base_path)
                        keys.append(str(relative_path))

            # Also check for files that start with prefix
            parent_dir = prefix_path.parent if prefix else self.base_path
            if parent_dir.exists():
                prefix_name = prefix_path.name if prefix else ""
                for path in parent_dir.glob(f"{prefix_name}*"):
                    if path.is_file():
                        relative_path = path.relative_to(self.base_path)
                        key = str(relative_path)
                        if key not in keys:
                            keys.append(key)
                    elif path.is_dir():
                        # Recursively add files from subdirectory
                        for subpath in path.rglob("*"):
                            if subpath.is_file():
                                relative_path = subpath.relative_to(self.base_path)
                                key = str(relative_path)
                                if key not in keys:
                                    keys.append(key)

            logger.debug(f"Listed {len(keys)} keys with prefix: {prefix}")
            return sorted(keys)

        except Exception as e:
            logger.error(f"Failed to list keys with prefix '{prefix}': {e}")
            return []

    def save_json(self, key: str, data: dict) -> None:
        """Save JSON data to file atomically."""
        try:
            path = self._get_path(key)
            json_str = json.dumps(data, indent=2)
            # Use atomic write for JSON too
            atomic_write(path, json_str.encode("utf-8"))
            logger.debug(f"Saved JSON to {key}")
        except Exception as e:
            raise RuntimeError(f"Failed to save JSON to '{key}': {e}")

    def load_json(self, key: str) -> dict:
        """Load JSON data from file."""
        try:
            path = self._get_path(key)
            if not path.exists():
                raise KeyError(f"Key not found: {key}")

            json_str = path.read_text(encoding="utf-8")
            data = json.loads(json_str)
            logger.debug(f"Loaded JSON from {key}")
            return data
        except KeyError:
            raise
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in '{key}': {e}")
        except Exception as e:
            raise RuntimeError(f"Failed to load JSON from '{key}': {e}")
