"""Root test fixtures available to all tests."""

import os
import pytest
from dask.distributed import LocalCluster, Client

# CI detection
IS_CI = os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true"


@pytest.fixture(scope="session")
def dask_cluster():
    """Shared Dask cluster fixture for all integration tests.

    This fixture:
    1. First tries to connect to an existing cluster at tcp://localhost:8786
    2. If that fails, creates a LocalCluster with CI-appropriate settings
    3. Adjusts resources based on CI environment
    4. Handles timeouts and resource failures gracefully

    Returns:
        Client: Dask distributed client connected to the cluster
    """
    import asyncio
    from concurrent.futures import TimeoutError as FutureTimeoutError

    # Scale resources based on environment
    if IS_CI:
        n_workers = 1
        threads_per_worker = 1
        memory_limit = "1GB"
        timeout = "30s"
    else:
        n_workers = 2
        threads_per_worker = 2
        memory_limit = "2GB"
        timeout = "10s"

    client = None
    cluster = None

    # First, try to connect to an existing cluster (e.g., from make dask-local)
    try:
        client = Client("tcp://localhost:8786", timeout=timeout)
        # Test the connection
        client.nthreads()
        print("Connected to existing Dask cluster at tcp://localhost:8786")
        yield client
        # Don't close an external cluster
        return
    except Exception:
        # No existing cluster, we'll create our own
        pass

    # Create a LocalCluster
    try:
        cluster = LocalCluster(
            n_workers=n_workers,
            threads_per_worker=threads_per_worker,
            processes=True,
            silence_logs=True,
            dashboard_address=None if IS_CI else ":8787",
            memory_limit=memory_limit,
            death_timeout="5s",  # Faster worker cleanup
        )
        client = Client(cluster, timeout=timeout)
        print(f"Created LocalCluster with {n_workers} workers")

    except (TimeoutError, FutureTimeoutError, asyncio.TimeoutError) as e:
        pytest.skip(f"Dask cluster creation timed out - likely resource issue: {e}")
    except Exception as e:
        pytest.skip(f"Dask cluster creation failed: {e}")

    yield client

    # Cleanup
    if client:
        try:
            client.close(timeout=5)
        except Exception:
            pass  # Best effort

    if cluster:
        try:
            cluster.close(timeout=5)
        except Exception:
            pass  # Best effort