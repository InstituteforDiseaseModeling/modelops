#!/usr/bin/env python
"""Example demonstrating cache and replication features."""

import numpy as np
from modelops.services import LocalSimulationService
from modelops.services.cache import SimulationCache
from modelops.services.storage import LocalFileBackend
from modelops.services.ipc import to_ipc_tables
from modelops_contracts import UniqueParameterSet

def simple_simulation(params: dict, seed: int) -> dict:
    """Simple simulation function for testing."""
    import polars as pl
    rng = np.random.default_rng(seed)
    
    # Simulate some data  
    n_points = params.get("n_points", 100)
    mean = params.get("mean", 0.0)
    std = params.get("std", 1.0)
    
    data = rng.normal(mean, std, n_points)
    
    # Create a proper DataFrame
    df = pl.DataFrame({"value": data, "index": range(n_points)})
    
    # Return as IPC format
    return to_ipc_tables({"results": df})

def mean_aggregator(results: list) -> dict:
    """Aggregate multiple results by taking mean."""
    from modelops.services.ipc import from_ipc_tables
    import polars as pl
    
    # Extract data from each result
    dfs = []
    for result in results:
        tables = from_ipc_tables(result)
        dfs.append(tables["results"])
    
    # Stack all dataframes and compute mean by index
    combined = pl.concat(dfs)
    aggregated = combined.group_by("index").agg(pl.col("value").mean())
    aggregated = aggregated.sort("index")
    
    return to_ipc_tables({"aggregated": aggregated})

def main():
    """Demonstrate cache and replication features."""
    
    # Set up storage and cache
    storage = LocalFileBackend("/tmp/modelops_example_cache")
    cache = SimulationCache(backend=storage)
    
    # Create simulation service with cache
    service = LocalSimulationService(cache=cache)
    
    print("=" * 60)
    print("ModelOps Cache and Replication Example")
    print("=" * 60)
    
    # Example 1: Submit batch with caching
    print("\n1. Batch submission with UniqueParameterSet:")
    param_sets = [
        UniqueParameterSet.from_dict({"n_points": 100, "mean": 0.0, "std": 1.0}),
        UniqueParameterSet.from_dict({"n_points": 100, "mean": 1.0, "std": 1.0}),
        UniqueParameterSet.from_dict({"n_points": 100, "mean": 2.0, "std": 1.0}),
    ]
    
    # First run - computes and caches
    print("   First run (computing)...")
    futures = service.submit_batch(
        "__main__:simple_simulation",
        param_sets,
        seed=42,
        bundle_ref=""
    )
    results = service.gather(futures)
    print(f"   Computed {len(results)} results")
    
    # Second run - uses cache
    print("   Second run (from cache)...")
    futures = service.submit_batch(
        "__main__:simple_simulation",
        param_sets,
        seed=42,  # Same seed
        bundle_ref=""
    )
    results = service.gather(futures)
    print(f"   Retrieved {len(results)} cached results")
    
    # Example 2: Submit replicates
    print("\n2. Replicate submission:")
    params = {"n_points": 100, "mean": 0.5, "std": 2.0}
    
    print("   Submitting 10 replicates...")
    futures = service.submit_replicates(
        "__main__:simple_simulation",
        params,
        seed=123,
        bundle_ref="",
        n_replicates=10
    )
    results = service.gather(futures)
    print(f"   Gathered {len(results)} replicate results")
    
    # Example 3: Aggregation
    print("\n3. Aggregation:")
    print("   Aggregating replicates with mean...")
    aggregated = service.gather_and_aggregate(
        futures,
        mean_aggregator  # Using callable for local execution
    )
    
    from modelops.services.ipc import from_ipc_tables
    agg_data = from_ipc_tables(aggregated)
    print(f"   Aggregated result shape: {agg_data['aggregated'].shape}")
    
    # Show cache stats
    print("\n4. Cache statistics:")
    stats = cache.stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")
    
    print("\n✅ Example completed successfully!")

if __name__ == "__main__":
    main()