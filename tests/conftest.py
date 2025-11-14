"""Test configuration and shared fixtures for ModelOps tests."""

import logging
import os
import socket
import pytest
from pathlib import Path

from distributed import Client, LocalCluster


def pytest_addoption(parser):
    """Add custom command line options for pytest."""
    parser.addoption(
        "--dask-address",
        action="store",
        default=None,
        help="Connect to an existing Dask scheduler instead of creating a LocalCluster "
        "(e.g., tcp://localhost:8786).",
    )


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    """Check if a port is open without hanging."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def dask_cluster(request):
    """Shared Dask client for integration tests.

    Default: spin up a LocalCluster.
    Opt-in: --dask-address=... or env DASK_ADDRESS to use an external scheduler.

    This fixture provides a Dask cluster for integration tests that require
    distributed computation. It adapts resources based on the environment
    (CI vs local development) to balance performance and resource usage.

    Yields:
        Client: A Dask client connected to either an external or local cluster.

    Environment Variables:
        CI: Set to "true" to enable CI-specific resource limits.
        DASK_ADDRESS: Optional address of external Dask scheduler.
    """
    # Set default test configuration in environment for workers to inherit
    # Workers are subprocesses of LocalCluster and inherit os.environ
    test_dir = Path(__file__).parent
    examples_dir = test_dir.parent / "examples"

    os.environ.setdefault("MODELOPS_BUNDLE_SOURCE", "file")
    os.environ.setdefault("MODELOPS_BUNDLES_DIR", str(examples_dir))
    os.environ.setdefault("MODELOPS_FORCE_FRESH_VENV", "false")

    addr = request.config.getoption("--dask-address") or os.getenv("DASK_ADDRESS")
    IS_CI = os.getenv("CI") == "true"

    if addr:
        # Fast preflight check so we don't hang in Client()
        hostport = addr.replace("tcp://", "")
        if ":" not in hostport:
            pytest.fail(f"--dask-address must include host:port (got {addr})")
        host, port = hostport.split(":")
        if not _port_open(host, int(port), timeout=0.25):
            pytest.fail(f"External Dask scheduler not reachable at {addr} (port closed)")

        try:
            client = Client(addr, timeout="2s")  # short connect timeout
            client.wait_for_workers(1, timeout="5s")  # verify it's alive
            print(f"Connected to external Dask cluster at {addr}")
        except Exception as e:
            pytest.fail(f"Failed to connect to external Dask at {addr}: {e}")

        try:
            yield client
        finally:
            # Closing the client does not shut down the external cluster.
            try:
                client.close()
            except Exception:
                pass
        return

    # Default path: create a LocalCluster
    n_workers = 1 if IS_CI else 2
    threads_per_worker = 1 if IS_CI else 2
    memory_limit = "1GB" if IS_CI else "2GB"

    try:
        cluster = LocalCluster(
            n_workers=n_workers,
            threads_per_worker=threads_per_worker,
            processes=True,
            silence_logs=logging.ERROR,
            dashboard_address=None,  # avoid port conflicts in CI
            memory_limit=memory_limit,
            death_timeout="5s",
        )
        client = Client(cluster, timeout="10s")
        client.wait_for_workers(n_workers, timeout="10s")
        print(f"Created LocalCluster with {n_workers} workers")
    except Exception as e:
        pytest.fail(f"Failed to bring up LocalCluster: {e}")

    try:
        yield client
    finally:
        try:
            client.close()
        finally:
            cluster.close()


# Alias for backward compatibility
dask_client = dask_cluster
