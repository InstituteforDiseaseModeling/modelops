"""Clean handling of Arrow IPC data transport encoding.

This module provides a type-safe way to handle Arrow IPC data that may be
encoded in different formats for transport (e.g., base64 for JSON-RPC).
"""

import base64
import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ArrowEncoding(Enum):
    """Encoding format for Arrow IPC data in transit."""

    RAW_BYTES = "raw_bytes"  # Direct bytes (in-memory)
    BASE64 = "base64"  # Base64-encoded string (JSON-RPC)


def decode_arrow_data(data: bytes | str, encoding_hint: str = None) -> bytes:
    """Decode Arrow IPC data from various transport encodings.

    Args:
        data: The Arrow IPC data in some encoding
        encoding_hint: Optional hint about the encoding format

    Returns:
        Raw Arrow IPC bytes ready for pl.read_ipc()

    Raises:
        ValueError: If the data format cannot be determined
    """
    # If already bytes, return as-is
    if isinstance(data, bytes):
        return data

    # String data needs decoding
    if isinstance(data, str):
        # Explicit encoding hint
        if encoding_hint == "base64":
            return base64.b64decode(data)

        # Auto-detect format (for backwards compatibility)
        # Check if it looks like base64 (no control chars, proper padding)
        try:
            decoded = base64.b64decode(data, validate=True)
            if decoded.startswith(b"ARROW"):
                return decoded
        except Exception:
            pass

        # If we get here, we couldn't determine the format
        raise ValueError(
            f"Cannot decode Arrow data: string of length {len(data)} "
            f"starting with {repr(data[:20])} is not valid base64"
        )

    raise TypeError(f"Arrow data must be bytes or str, got {type(data)}")


def extract_arrow_from_artifact(artifact: dict[str, Any] | bytes) -> bytes:
    """Extract Arrow IPC bytes from a TableArtifact-like structure.

    Args:
        artifact: Either a dict with 'inline' or 'data' field, or raw bytes

    Returns:
        Raw Arrow IPC bytes

    Raises:
        ValueError: If the artifact structure is invalid
    """
    # Direct bytes
    if isinstance(artifact, bytes):
        return artifact

    # Dict-like artifact
    if isinstance(artifact, dict):
        # Check for 'inline' field (standard TableArtifact)
        if "inline" in artifact:
            return decode_arrow_data(
                artifact["inline"],
                encoding_hint="base64",  # Serialization always uses base64
            )

        # Check for 'data' field (alternative format)
        if "data" in artifact:
            return decode_arrow_data(
                artifact["data"],
                encoding_hint="base64",  # Assume base64 for consistency
            )

        # Invalid structure
        available_keys = list(artifact.keys())
        raise ValueError(
            f"TableArtifact missing 'inline' or 'data' field. Available keys: {available_keys}"
        )

    raise TypeError(f"Artifact must be dict or bytes, got {type(artifact)}")
