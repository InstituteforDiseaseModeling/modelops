#!/usr/bin/env python
"""Test warm process pool caching effectiveness."""

import time
from dask.distributed import Client
from modelops_contracts import SimTask
from modelops.services.dask_simulation import DaskSimulationService
from modelops.worker.config import RuntimeConfig

def main():
    """Test process caching."""
    client = Client("tcp://localhost:8786")
    print(f"Connected to cluster with {len(client.scheduler_info()['workers'])} workers")
    
    # Create config WITHOUT force_fresh_venv so caching works
    config = RuntimeConfig(
        bundle_source="file",
        bundles_dir="/Users/vsb/projects/work/modelops",
        force_fresh_venv=False  # IMPORTANT: Allow process reuse!
    )
    
    service = DaskSimulationService(client, config)
    
    # Define the task
    task = SimTask.from_components(
        import_path="simulations.test",
        scenario="baseline",
        bundle_ref="local://examples/test_bundle",
        params={"alpha": 1.0, "beta": 2.0, "n_samples": 100},
        seed=42
    )
    
    print("\n=== Round 1: Cold Start (creating processes & venvs) ===")
    start = time.time()
    
    # Submit 5 tasks  
    futures = []
    for i in range(5):
        task_copy = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref="local://examples/test_bundle",
            params={"alpha": 1.0 + i*0.1, "beta": 2.0, "n_samples": 100},
            seed=42 + i
        )
        future = service.submit(task_copy)
        futures.append(future)
    
    # Gather results
    results = service.gather(futures)
    cold_time = time.time() - start
    
    print(f"Completed {len(results)} tasks in {cold_time:.2f}s")
    print(f"Rate: {len(results)/cold_time:.1f} tasks/second")
    
    # Check process pool status
    def check_pool():
        from dask.distributed import get_worker
        worker = get_worker()
        if hasattr(worker, 'modelops_runtime'):
            runtime = worker.modelops_runtime
            if hasattr(runtime, '_process_manager'):
                pm = runtime._process_manager
                return {
                    'n_processes': pm.active_count(),
                    'max_processes': pm.max_processes,
                    'force_fresh': pm.force_fresh_venv
                }
        return {'error': 'No process manager'}
    
    pool_info = client.submit(check_pool, pure=False).result()
    if 'error' in pool_info:
        print(f"\nCould not check process pool: {pool_info['error']}")
    else:
        print(f"\nProcess pool: {pool_info['n_processes']} warm processes (max: {pool_info['max_processes']})")
        print(f"Force fresh venv: {pool_info['force_fresh']}")
    
    print("\n=== Round 2: Warm Start (reusing processes) ===")
    start = time.time()
    
    # Submit another 5 tasks with SAME bundle
    futures = []
    for i in range(5):
        task_copy = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref="local://examples/test_bundle",  # SAME bundle!
            params={"alpha": 2.0 + i*0.1, "beta": 3.0, "n_samples": 100},
            seed=100 + i
        )
        future = service.submit(task_copy)
        futures.append(future)
    
    # Gather results
    results = service.gather(futures)
    warm_time = time.time() - start
    
    print(f"Completed {len(results)} tasks in {warm_time:.2f}s")
    print(f"Rate: {len(results)/warm_time:.1f} tasks/second")
    
    # Calculate speedup
    speedup = cold_time / warm_time
    print(f"\n🚀 Speedup: {speedup:.1f}x faster with warm processes!")
    
    if speedup < 2:
        print("⚠️  Warning: Expected at least 2x speedup. Check if caching is working.")
    else:
        print("✅ Process caching is working effectively!")
    
    client.close()

if __name__ == "__main__":
    main()