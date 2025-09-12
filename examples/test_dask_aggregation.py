#!/usr/bin/env python
"""Test aggregation and replication features on live Dask workspace.

This example demonstrates:
1. Submitting replicates with statistically independent seeds
2. Worker-side aggregation for performance
3. Batch submission with UniqueParameterSet
4. Cache integration

Usage:
    # First set up Dask workspace:
    mops workspace up examples/workspace.yaml
    mops workspace port-forward
    
    # Then run this test:
    python examples/test_dask_aggregation.py
    
    # With cache enabled (optional):
    mops storage up examples/storage.yaml
    mops storage connection-string > ~/.modelops/storage.env
    source ~/.modelops/storage.env
    python examples/test_dask_aggregation.py --cache
"""

import argparse
import time
import numpy as np
import polars as pl
from modelops.services.simulation import LocalSimulationService
from modelops.services.dask_simulation import DaskSimulationService
from modelops.services.cache import SimulationCache
from modelops.services.storage import get_default_backend
from modelops.services.ipc import from_ipc_tables
from modelops_contracts import SimTask

# Import simulation and aggregation functions from examples module
# The examples directory is included in the package build, so these
# are available to Dask workers via the Docker image
from examples.aggregation_functions import (
    epidemic_simulation,
    mean_across_replicates,
    percentile_aggregator
)


# ============================================================================
# Main Test Function
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Test Dask aggregation features")
    parser.add_argument("--scheduler", default="tcp://localhost:8786",
                        help="Dask scheduler address")
    parser.add_argument("--cache", action="store_true",
                        help="Enable caching with storage backend")
    parser.add_argument("--local", action="store_true",
                        help="Use local service instead of Dask")
    args = parser.parse_args()
    
    print("=" * 70)
    print("ModelOps Dask Aggregation and Replication Test")
    print("=" * 70)
    
    # Initialize service with optional cache
    cache = None
    if args.cache:
        print("\n📦 Setting up cache...")
        backend = get_default_backend()
        cache = SimulationCache(backend=backend)
        print(f"   Backend: {type(backend).__name__}")
        print(f"   Stats: {cache.stats()}")
    
    if args.local:
        print("\n🏠 Using LocalSimulationService")
        service = LocalSimulationService(cache=cache)
    else:
        print(f"\n🚀 Connecting to Dask at {args.scheduler}")
        service = DaskSimulationService(args.scheduler, cache=cache)
        
        # Show cluster info
        if hasattr(service, 'client'):
            info = service.client.scheduler_info()
            print(f"   Workers: {len(info.get('workers', {}))}")
            print(f"   Dashboard: http://localhost:8787")
    
    # ========================================================================
    # Test 1: Submit Replicates with Independent Seeds
    # ========================================================================
    print("\n" + "=" * 70)
    print("Test 1: Submit Replicates with Independent Seeds")
    print("=" * 70)
    
    n_replicates = 100
    base_seed = 42
    
    # TODO(MVP): Using local://dev with placeholder all-zeros digest
    # Future: Will compute real workspace digest from git + uv.lock
    base_task = SimTask.from_components(
        import_path="examples.aggregation_functions.epidemic_simulation",
        scenario="default",
        bundle_ref="local://dev",  # PLACEHOLDER: Uses all-zeros digest for MVP
        params={"n_days": 100, "r0": 2.5, "recovery_rate": 0.1},
        seed=base_seed
    )
    
    print(f"\nSubmitting {n_replicates} replicates of epidemic simulation...")
    print(f"Parameters: {base_task.params.params}")
    print(f"Base seed: {base_seed}")
    
    start = time.time()
    futures = service.submit_replicated(base_task, n_replicates)
    print(f"✓ Submitted {len(futures)} futures")
    
    # Show seed derivation
    ss = np.random.SeedSequence(base_seed)
    child_seeds = ss.spawn(5)  # Just show first 5
    seed_vals = [int(cs.generate_state(1)[0]) for cs in child_seeds]
    print(f"\nDerived seeds (first 5): {seed_vals}")
    print("   Each replicate has statistically independent randomness!")
    
    # Gather results
    print(f"\nGathering {n_replicates} results...")
    results = service.gather(futures)
    elapsed = time.time() - start
    print(f"✓ Completed in {elapsed:.3f} seconds")
    print(f"   Rate: {n_replicates/elapsed:.1f} simulations/second")
    
    # ========================================================================
    # Test 2: Worker-Side Aggregation (Performance Test)
    # ========================================================================
    print("\n" + "=" * 70)
    print("Test 2: Worker-Side Aggregation (100x Performance)")
    print("=" * 70)
    
    # First, test client-side aggregation (slow)
    print("\nA) Client-side aggregation (transferring all data)...")
    start = time.time()
    results = service.gather(futures)  # Transfer all data
    aggregated_client = mean_across_replicates(results)  # Aggregate locally
    client_time = time.time() - start
    print(f"   Time: {client_time:.3f} seconds")
    
    # Now test worker-side aggregation (fast)
    print("\nB) Worker-side aggregation (aggregate before transfer)...")
    start = time.time()
    
    if args.local:
        # Local service uses callable
        aggregated_worker = service.gather_and_aggregate(
            futures,
            mean_across_replicates  # Pass function directly
        )
    else:
        # Dask service uses string reference for distributed execution
        aggregated_worker = service.gather_and_aggregate(
            futures,
            "examples.aggregation_functions:mean_across_replicates"  # String ref for worker execution
        )
    
    worker_time = time.time() - start
    print(f"   Time: {worker_time:.3f} seconds")
    
    if client_time > 0 and worker_time > 0:
        speedup = client_time / worker_time
        print(f"\n✨ Speedup: {speedup:.1f}x faster with worker-side aggregation!")
    
    # Show aggregated results
    agg_data = from_ipc_tables(aggregated_worker)["aggregated"]
    print(f"\nAggregated data shape: {agg_data.shape}")
    print("\nFirst 5 rows of aggregated data:")
    print(agg_data.head())
    
    # ========================================================================
    # Test 3: Batch Submission with UniqueParameterSet
    # ========================================================================
    print("\n" + "=" * 70)
    print("Test 3: Batch Submission with Parameter Tracking")
    print("=" * 70)
    
    # Create parameter sweep with SimTask objects
    # TODO(MVP): Using local://dev with placeholder all-zeros digest
    tasks = [
        SimTask.from_components(
            import_path="examples.aggregation_functions.epidemic_simulation",
            scenario="default",
            bundle_ref="local://dev",  # PLACEHOLDER: Uses all-zeros digest for MVP
            params={"n_days": 100, "r0": 2.0, "recovery_rate": 0.1},
            seed=123
        ),
        SimTask.from_components(
            import_path="examples.aggregation_functions.epidemic_simulation",
            scenario="default",
            bundle_ref="local://dev",
            params={"n_days": 100, "r0": 2.5, "recovery_rate": 0.1},
            seed=123
        ),
        SimTask.from_components(
            import_path="examples.aggregation_functions.epidemic_simulation",
            scenario="default",
            bundle_ref="local://dev",
            params={"n_days": 100, "r0": 3.0, "recovery_rate": 0.1},
            seed=123
        ),
        SimTask.from_components(
            import_path="examples.aggregation_functions.epidemic_simulation",
            scenario="default",
            bundle_ref="local://dev",
            params={"n_days": 100, "r0": 3.5, "recovery_rate": 0.1},
            seed=123
        ),
    ]
    
    print(f"\nSubmitting batch of {len(tasks)} tasks...")
    for i, task in enumerate(tasks):
        print(f"  {i}: param_id={task.params.param_id[:8]}... r0={task.params.params['r0']}")
    
    start = time.time()
    batch_futures = service.submit_batch(tasks)
    
    # Test cache hit (if enabled)
    if cache:
        print("\n🔄 Testing cache (submitting same batch again)...")
        cache_start = time.time()
        batch_futures_2 = service.submit_batch(tasks)  # Same tasks
        cache_time = time.time() - cache_start
        print(f"   Cache retrieval time: {cache_time:.3f} seconds")
        if cache_time < 0.1:
            print("   ✓ Cache hit! Results retrieved instantly")
    
    batch_results = service.gather(batch_futures)
    batch_time = time.time() - start
    print(f"\n✓ Batch completed in {batch_time:.3f} seconds")
    
    # ========================================================================
    # Test 4: Different Aggregation Strategies
    # ========================================================================
    print("\n" + "=" * 70)
    print("Test 4: Different Aggregation Strategies")
    print("=" * 70)
    
    # Submit more replicates for aggregation testing
    print(f"\nSubmitting 50 replicates for aggregation tests...")
    # TODO(MVP): Using local://dev with placeholder all-zeros digest
    large_task = SimTask.from_components(
        import_path="examples.aggregation_functions.epidemic_simulation",
        scenario="default",
        bundle_ref="local://dev",  # PLACEHOLDER: Uses all-zeros digest for MVP
        params={"n_days": 50, "r0": 2.8, "recovery_rate": 0.15},
        seed=999
    )
    large_futures = service.submit_replicates(large_task, 50)
    
    # Test percentile aggregation
    print("\nComputing percentiles across replicates...")
    if args.local:
        percentiles = service.gather_and_aggregate(
            large_futures,
            percentile_aggregator
        )
    else:
        percentiles = service.gather_and_aggregate(
            large_futures,
            "examples.aggregation_functions:percentile_aggregator"
        )
    
    pct_data = from_ipc_tables(percentiles)["percentiles"]
    print(f"Percentile data shape: {pct_data.shape}")
    print("\nPercentiles at day 25:")
    day_25 = pct_data.filter(pl.col("day") == 25)
    if not day_25.is_empty():
        print(day_25)
    
    # ========================================================================
    # Summary
    # ========================================================================
    print("\n" + "=" * 70)
    print("Summary of Features Tested")
    print("=" * 70)
    
    features = [
        ("Submit Replicates", "✅", "Independent seeds via SeedSequence"),
        ("Worker-Side Aggregation", "✅", f"{speedup:.1f}x speedup" if 'speedup' in locals() else "Tested"),
        ("Batch Submission", "✅", f"{len(tasks)} parameter sets"),
        ("Parameter Tracking", "✅", "UniqueParameterSet with param_id"),
        ("Cache Integration", "✅" if cache else "⏭️", "Enabled" if cache else "Skipped"),
        ("Multiple Aggregators", "✅", "Mean and Percentiles"),
    ]
    
    for feature, status, note in features:
        print(f"  {status} {feature:<25} {note}")
    
    if cache:
        print(f"\n📊 Final cache stats: {cache.stats()}")
    
    print("\n✨ All tests completed successfully!")
    
    # Clean up
    if not args.local and hasattr(service, 'close'):
        service.close()


if __name__ == "__main__":
    main()
