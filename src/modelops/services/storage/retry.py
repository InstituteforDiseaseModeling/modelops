"""Retry logic for CAS operations with exponential backoff.

Provides generic retry mechanisms for handling version conflicts
in concurrent scenarios.
"""

import json
import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from .versioned import TooManyRetriesError, VersionedStore

logger = logging.getLogger(__name__)

T = TypeVar("T")


def update_with_retry(
    store: VersionedStore,
    key: str,
    update_fn: Callable[[dict], dict],
    max_attempts: int = 5,
    initial_delay: float = 0.1,
) -> dict:
    """Update a JSON value with CAS retry logic.

    Handles the common pattern of:
    1. Read current value
    2. Apply update function
    3. Try to write back
    4. Retry with backoff on conflict

    Args:
        store: VersionedStore implementation
        key: Storage key
        update_fn: Function that takes current dict and returns updated dict
        max_attempts: Maximum retry attempts before giving up
        initial_delay: Initial retry delay in seconds (doubles each attempt)

    Returns:
        The successfully updated value

    Raises:
        KeyError: If key doesn't exist
        TooManyRetriesError: If all retry attempts failed
    """
    for attempt in range(max_attempts):
        # Read current state
        result = store.get(key)
        if result is None:
            raise KeyError(f"Key {key} not found")

        current_bytes, version = result

        # Decode JSON
        try:
            current_value = json.loads(current_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Invalid JSON in {key}: {e}")

        # Apply update
        try:
            new_value = update_fn(current_value)
        except Exception:
            # Don't retry on update function errors - re-raise them
            # This allows business logic exceptions to propagate correctly
            raise

        # Encode back to bytes
        new_bytes = json.dumps(new_value, indent=2).encode("utf-8")

        # Try to write back
        if store.put(key, new_bytes, version):
            logger.debug(f"Successfully updated {key} on attempt {attempt + 1}")
            return new_value

        # Conflict - retry with exponential backoff + jitter
        if attempt < max_attempts - 1:
            delay = (2**attempt) * initial_delay  # 0.1s, 0.2s, 0.4s, 0.8s, 1.6s
            jitter = random.uniform(0, initial_delay)  # Up to 100ms jitter
            total_delay = delay + jitter

            logger.debug(
                f"CAS conflict on {key}, attempt {attempt + 1}/{max_attempts}, "
                f"retrying in {total_delay:.3f}s"
            )
            time.sleep(total_delay)
        else:
            logger.warning(f"CAS conflict on {key}, no more retries")

    raise TooManyRetriesError(f"Failed to update {key} after {max_attempts} attempts")


def create_with_retry(store: VersionedStore, key: str, value: dict, max_attempts: int = 3) -> bool:
    """Create a new JSON value with retry logic.

    Simpler than update_with_retry since create_if_absent is already atomic.
    We just retry a few times in case of transient errors.

    Args:
        store: VersionedStore implementation
        key: Storage key
        value: Initial value as dict
        max_attempts: Maximum retry attempts

    Returns:
        True if created, False if already exists

    Raises:
        Exception: If creation fails for reasons other than existence
    """
    value_bytes = json.dumps(value, indent=2).encode("utf-8")

    for attempt in range(max_attempts):
        try:
            if store.create_if_absent(key, value_bytes):
                logger.debug(f"Created {key} on attempt {attempt + 1}")
                return True
            else:
                logger.debug(f"Key {key} already exists")
                return False

        except Exception as e:
            if attempt < max_attempts - 1:
                logger.warning(f"Failed to create {key} on attempt {attempt + 1}: {e}, retrying")
                time.sleep(0.1 * (attempt + 1))
            else:
                raise

    return False  # Should never reach here


def get_json(store: VersionedStore, key: str) -> dict | None:
    """Get and decode a JSON value.

    Convenience function that handles JSON decoding.

    Args:
        store: VersionedStore implementation
        key: Storage key

    Returns:
        Decoded dict if exists, None otherwise
    """
    result = store.get(key)
    if result is None:
        return None

    data_bytes, _ = result
    try:
        return json.loads(data_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"Invalid JSON in {key}: {e}")
        return None
