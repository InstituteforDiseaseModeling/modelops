"""Utility functions for error handling and formatting."""

import base64
import json
import logging

logger = logging.getLogger(__name__)


def decode_base64_error(error_msg: str) -> str:
    """Decode base64-encoded error messages to human-readable format.

    Args:
        error_msg: Potentially base64-encoded error message

    Returns:
        Human-readable error message, or original if not base64
    """
    try:
        # Try to decode as base64
        decoded = base64.b64decode(error_msg)
        error_data = json.loads(decoded)

        # Format a human-readable error message
        formatted = []
        formatted.append(f"Error: {error_data.get('error', 'Unknown error')}")
        formatted.append(f"Type: {error_data.get('type', 'Unknown')}")

        if "target_entrypoint" in error_data:
            formatted.append(f"Target: {error_data['target_entrypoint']}")

        if "traceback" in error_data:
            formatted.append("Traceback:")
            formatted.append(error_data["traceback"])

        return "\n  ".join(formatted)

    except Exception:
        # If decoding fails, return original message
        return error_msg


def format_aggregation_error(result: dict) -> str:
    """Format aggregation error from subprocess result.

    Args:
        result: Result dict from subprocess with 'error' field

    Returns:
        Formatted error message
    """
    if "error" not in result:
        return "Unknown aggregation error"

    error_msg = decode_base64_error(result["error"])
    error_type = result.get("type", "Unknown")

    if error_msg != result["error"]:
        # Successfully decoded
        return f"Aggregation failed:\n  {error_msg}"
    else:
        # Not base64 or decode failed
        return f"Aggregation failed: {error_msg} (type: {error_type})"
