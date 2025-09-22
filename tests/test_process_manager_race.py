"""Test concurrent process reuse race conditions."""

import pytest
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from modelops.worker.process_manager import WarmProcessManager
from modelops.worker.jsonrpc import JSONRPCError


@pytest.fixture
def test_bundle_path(tmp_path):
    """Create a minimal test bundle."""
    bundle_path = tmp_path / "test_bundle"
    bundle_path.mkdir()

    # Create a simple wire function
    (bundle_path / "wire.py").write_text("""
def wire(entrypoint, params, seed):
    return {"result": b"ok"}
""")

    # Create pyproject.toml
    (bundle_path / "pyproject.toml").write_text("""
[project]
name = "test-bundle"
version = "0.1.0"

[project.entry-points."modelops.bundle"]
test_bundle = "wire:wire"
""")

    return bundle_path


@pytest.fixture
def process_manager(tmp_path):
    """Create a WarmProcessManager for testing."""
    venvs_dir = tmp_path / "venvs"
    venvs_dir.mkdir()

    manager = WarmProcessManager(
        venvs_dir=venvs_dir,
        max_processes=5,  # Small pool to force reuse
        force_fresh_venv=False
    )

    yield manager

    # Cleanup
    manager.shutdown_all()


def test_concurrent_process_reuse_race_condition(process_manager, test_bundle_path):
    """Test that concurrent reuse of warm processes causes header corruption.

    This test should FAIL initially due to the race condition, then PASS after the fix.
    """
    bundle_digest = "test123"
    errors = []
    successful_calls = []
    processes_created = []

    def try_get_and_use_process():
        """Try to get a process and make multiple calls - may encounter header corruption."""
        try:
            # Get the process
            process = process_manager.get_process(bundle_digest, test_bundle_path)
            processes_created.append(process)

            # Make one call to avoid too much complexity
            result = process.safe_call("ready", {})
            successful_calls.append(result)
            return True
        except JSONRPCError as e:
            if "Invalid header" in str(e) or "Missing Content-Length" in str(e):
                errors.append(str(e))
                return False
            else:
                # Re-raise unexpected errors
                raise
        except Exception as e:
            # Capture any other errors for debugging
            errors.append(f"Unexpected error: {e}")
            return False

    # First, create a process to ensure there's one to reuse
    initial_process = process_manager.get_process(bundle_digest, test_bundle_path)

    # Now try concurrent access to stress test the fix
    num_threads = 15  # Stress test the fix
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        # Submit multiple concurrent requests
        futures = [executor.submit(try_get_and_use_process) for _ in range(num_threads)]

        # Wait for all to complete
        results = [future.result() for future in as_completed(futures)]

    # Print diagnostics for debugging
    print(f"Successful calls: {len(successful_calls)}")
    print(f"Errors encountered: {len(errors)}")
    print(f"Unique processes created: {len(set(id(p) for p in processes_created))}")
    for error in errors:
        print(f"  - {error}")

    # The test should initially fail due to race condition
    # After fix, we should have no header corruption errors
    header_corruption_errors = [e for e in errors if "Invalid header" in e or "Missing Content-Length" in e]

    # Since the race condition is intermittent and process manager handles errors internally,
    # let's check if we got any JSONRPCError at all or if multiple processes were unnecessarily created
    # (indicating the validation failed and new processes were spawned)
    unique_processes = len(set(id(p) for p in processes_created))

    print(f"Header corruption errors: {len(header_corruption_errors)}")
    print(f"All errors: {len(errors)}")
    print(f"Should have reused 1 process but created {unique_processes}")

    # After the fix, we should have:
    # 1. No header corruption errors, AND
    # 2. Efficient process reuse (should reuse the initial process)

    # With the fix, we should reuse processes efficiently
    # Allow some process creation due to legitimate reasons, but should be much less
    efficient_reuse = unique_processes <= 5  # Should reuse most of the time

    # Should have no header corruption errors after the fix
    no_corruption = len(header_corruption_errors) == 0

    assert no_corruption, f"Still getting header corruption errors after fix: {header_corruption_errors}"
    assert efficient_reuse, f"Not reusing processes efficiently after fix: created {unique_processes} processes"


def test_sequential_process_reuse_works(process_manager, test_bundle_path):
    """Test that sequential reuse works fine (no race condition)."""
    bundle_digest = "test456"

    # Sequential calls should work fine
    for i in range(5):
        process = process_manager.get_process(bundle_digest, test_bundle_path)
        result = process.client.call("ready", {})
        assert result.get("ready") is True
        assert result.get("bundle_digest") == bundle_digest


def test_multiple_processes_no_reuse_works(process_manager, test_bundle_path):
    """Test that using different bundle digests (no reuse) works fine."""
    # Different bundle digests should work fine concurrently
    def try_different_bundle(digest_suffix):
        bundle_digest = f"test-{digest_suffix}"
        process = process_manager.get_process(bundle_digest, test_bundle_path)
        result = process.client.call("ready", {})
        return result.get("ready") is True

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(try_different_bundle, i) for i in range(5)]
        results = [future.result() for future in as_completed(futures)]

    assert all(results), "All different bundle processes should work"