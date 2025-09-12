#!/usr/bin/env python
"""Start a local Dask cluster for testing.

This replaces the need for command-line 'dask scheduler' and 'dask worker'.
"""

import time
from dask.distributed import LocalCluster, Client

def main():
    print("Starting local Dask cluster...")
    print("=" * 60)
    
    # Create a local cluster with explicit configuration
    cluster = LocalCluster(
        n_workers=2,                    # Number of worker processes
        threads_per_worker=2,            # Threads per worker
        scheduler_port=8786,             # Scheduler port (matches examples)
        dashboard_address=':8787',      # Dashboard port
        processes=True,                  # Use processes (not threads)
        silence_logs=False,              # Show logs
        memory_limit='4GB',              # Memory limit per worker
    )
    
    # Create client to connect to cluster
    client = Client(cluster)
    
    print(f"✅ Dask cluster started!")
    print(f"   Scheduler: {cluster.scheduler_address}")
    print(f"   Dashboard: http://localhost:8787")
    print(f"   Workers: {len(cluster.workers)}")
    print()
    print("Keep this running and run examples in another terminal:")
    print("  uv run python examples/test_dask_simple.py")
    print("  uv run python examples/test_simulation_e2e.py")
    print()
    print("Press Ctrl+C to stop the cluster...")
    
    try:
        # Keep running until interrupted
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down cluster...")
        client.close()
        cluster.close()
        print("✅ Cluster stopped")

if __name__ == "__main__":
    main()