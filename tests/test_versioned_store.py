"""Tests for VersionedStore implementations.

Tests the CAS semantics across different implementations to ensure
they all behave consistently.
"""

import json
import pytest
import threading
import time
from typing import Any

from modelops.services.storage.versioned import VersionToken
from modelops.services.storage.memory import InMemoryVersionedStore
from modelops.services.storage.retry import update_with_retry, create_with_retry, get_json


class TestVersionedStore:
    """Test CAS semantics for versioned stores."""

    @pytest.fixture
    def store(self):
        """Provide a clean in-memory store for each test."""
        return InMemoryVersionedStore()

    def test_create_if_absent(self, store):
        """Test atomic creation."""
        # First create should succeed
        data1 = b'{"value": 1}'
        assert store.create_if_absent("key1", data1)

        # Second create should fail
        data2 = b'{"value": 2}'
        assert not store.create_if_absent("key1", data2)

        # Verify first value was kept
        result = store.get("key1")
        assert result is not None
        stored_data, _ = result
        assert stored_data == data1

    def test_get_nonexistent(self, store):
        """Test getting a key that doesn't exist."""
        result = store.get("nonexistent")
        assert result is None

    def test_cas_update_success(self, store):
        """Test successful CAS update."""
        # Create initial value
        initial = b'{"value": 1}'
        assert store.create_if_absent("key1", initial)

        # Get current version
        result = store.get("key1")
        assert result is not None
        data, version = result

        # Update with correct version
        updated = b'{"value": 2}'
        assert store.put("key1", updated, version)

        # Verify update
        result = store.get("key1")
        assert result is not None
        data, new_version = result
        assert data == updated
        assert new_version.value != version.value  # Version should change

    def test_cas_update_conflict(self, store):
        """Test CAS update with stale version."""
        # Create initial value
        initial = b'{"value": 1}'
        assert store.create_if_absent("key1", initial)

        # Get version
        result = store.get("key1")
        assert result is not None
        _, version1 = result

        # Update once
        update1 = b'{"value": 2}'
        assert store.put("key1", update1, version1)

        # Try to update with stale version
        update2 = b'{"value": 3}'
        assert not store.put("key1", update2, version1)  # Should fail

        # Verify value is still from first update
        result = store.get("key1")
        assert result is not None
        data, _ = result
        assert data == update1

    def test_list_keys(self, store):
        """Test listing keys with prefix."""
        # Create some keys
        store.create_if_absent("jobs/123/state.json", b'{}')
        store.create_if_absent("jobs/456/state.json", b'{}')
        store.create_if_absent("events/123/chunk_0.json", b'[]')

        # List all keys
        all_keys = store.list_keys()
        assert len(all_keys) == 3

        # List with prefix
        job_keys = store.list_keys("jobs/")
        assert len(job_keys) == 2
        assert "jobs/123/state.json" in job_keys
        assert "jobs/456/state.json" in job_keys

        event_keys = store.list_keys("events/")
        assert len(event_keys) == 1
        assert "events/123/chunk_0.json" in event_keys

    def test_delete(self, store):
        """Test deletion."""
        # Create and verify
        store.create_if_absent("key1", b'{"value": 1}')
        assert store.get("key1") is not None

        # Delete
        assert store.delete("key1")

        # Verify deleted
        assert store.get("key1") is None

        # Delete again should return False
        assert not store.delete("key1")

    def test_concurrent_updates(self, store):
        """Test concurrent CAS updates resolve correctly."""
        # Create initial value
        initial = {"counter": 0, "updates": []}
        store.create_if_absent("shared", json.dumps(initial).encode())

        results = []
        errors = []

        def increment_counter(worker_id: int):
            """Each worker tries to increment the counter."""
            try:
                def update_fn(current: dict) -> dict:
                    current["counter"] += 1
                    current["updates"].append(f"worker-{worker_id}")
                    return current

                update_with_retry(store, "shared", update_fn, max_attempts=10)
                results.append(worker_id)
            except Exception as e:
                errors.append((worker_id, str(e)))

        # Launch concurrent workers
        threads = []
        num_workers = 10
        for i in range(num_workers):
            thread = threading.Thread(target=increment_counter, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all to complete
        for thread in threads:
            thread.join()

        # Check results
        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert len(results) == num_workers

        # Verify final state
        final = get_json(store, "shared")
        assert final is not None
        assert final["counter"] == num_workers
        assert len(final["updates"]) == num_workers


class TestRetryLogic:
    """Test retry mechanisms."""

    @pytest.fixture
    def store(self):
        return InMemoryVersionedStore()

    def test_update_with_retry_success(self, store):
        """Test successful update with retry logic."""
        # Create initial state
        initial = {"value": 1}
        store.create_if_absent("key1", json.dumps(initial).encode())

        # Update function
        def increment(current: dict) -> dict:
            current["value"] += 1
            return current

        # Update should succeed
        result = update_with_retry(store, "key1", increment)
        assert result["value"] == 2

        # Verify persisted
        stored = get_json(store, "key1")
        assert stored["value"] == 2

    def test_update_with_retry_nonexistent(self, store):
        """Test update fails gracefully for nonexistent key."""
        def update_fn(current: dict) -> dict:
            return current

        with pytest.raises(KeyError, match="not found"):
            update_with_retry(store, "nonexistent", update_fn)

    def test_create_with_retry_already_exists(self, store):
        """Test create handles existing key gracefully."""
        value = {"test": "data"}

        # First create succeeds
        assert create_with_retry(store, "key1", value)

        # Second create returns False (not error)
        assert not create_with_retry(store, "key1", value)

    def test_get_json_invalid(self, store):
        """Test get_json handles invalid JSON gracefully."""
        # Store invalid JSON
        store.create_if_absent("bad", b'not json')

        # Should return None and log error
        result = get_json(store, "bad")
        assert result is None

    def test_update_function_error(self, store):
        """Test that errors in update function are not retried."""
        store.create_if_absent("key1", b'{"value": 1}')

        def failing_update(current: dict) -> dict:
            raise ValueError("Intentional error")

        with pytest.raises(ValueError, match="Intentional error"):
            update_with_retry(store, "key1", failing_update)


class TestInMemoryStore:
    """Tests specific to in-memory implementation."""

    def test_thread_safety(self):
        """Test that in-memory store is thread-safe."""
        store = InMemoryVersionedStore()

        # Create a key
        store.create_if_absent("counter", b'0')

        def increment_many():
            """Increment counter 100 times."""
            for _ in range(100):
                result = store.get("counter")
                if result:
                    data, version = result
                    value = int(data.decode())
                    new_value = str(value + 1).encode()
                    store.put("counter", new_value, version)
                    # Ignore failures (conflicts are expected)

        # Run multiple threads
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=increment_many)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # Counter should have some value (not all increments succeed due to conflicts)
        result = store.get("counter")
        assert result is not None
        data, _ = result
        value = int(data.decode())
        assert value > 0  # At least some increments succeeded

    def test_clear(self):
        """Test clearing the store."""
        store = InMemoryVersionedStore()

        # Add some data
        store.create_if_absent("key1", b'data1')
        store.create_if_absent("key2", b'data2')

        assert len(store.list_keys()) == 2

        # Clear
        store.clear()

        assert len(store.list_keys()) == 0
        assert store.get("key1") is None
        assert store.get("key2") is None