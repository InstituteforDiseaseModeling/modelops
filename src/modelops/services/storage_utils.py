"""Storage utilities for atomic operations and safe file handling."""

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def atomic_write(path: str | Path, content: bytes) -> None:
    """Write file atomically using temp file + rename.

    This ensures that readers never see partial writes or corrupted files.
    The file is written to a temporary location then atomically renamed.

    Args:
        path: Target file path
        content: Bytes to write

    Raises:
        OSError: If write or rename fails
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Create temp file in same directory for atomic rename
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
        try:
            # Write content
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())  # Ensure data is on disk

            # Atomic rename (on POSIX systems)
            tmp_path.replace(path)
            logger.debug(f"Atomically wrote {len(content)} bytes to {path}")

        except Exception:
            # Clean up temp file on failure
            try:
                tmp_path.unlink()
            except:
                pass
            raise


def safe_read(path: str | Path) -> bytes | None:
    """Read file safely, returning None if not found.

    Args:
        path: File path to read

    Returns:
        File contents as bytes or None if not found
    """
    path = Path(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"Failed to read {path}: {e}")
        raise


def ensure_dir(path: str | Path) -> Path:
    """Ensure directory exists, creating if necessary.

    Args:
        path: Directory path

    Returns:
        Path object for the directory
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_rename(src: str | Path, dst: str | Path) -> None:
    """Atomically rename/move a file.

    Args:
        src: Source path
        dst: Destination path
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
