"""Functions for testing aggregation and replication features with Dask.

These functions are separated into a module so they can be imported
by Dask workers running in separate processes.
"""

import numpy as np
import polars as pl
from typing import Dict, List
from modelops.services.ipc import to_ipc_tables, from_ipc_tables
from modelops_contracts import SimReturn


def epidemic_simulation(params: dict, seed: int) -> dict:
    """Simple epidemic simulation for testing.
    
    Simulates daily infections over time with stochasticity.
    """
    rng = np.random.default_rng(seed)
    
    # Parameters
    n_days = params.get("n_days", 100)
    r0 = params.get("r0", 2.5)
    recovery_rate = params.get("recovery_rate", 0.1)
    
    # Simple SIR-like dynamics with noise
    infections = np.zeros(n_days)
    infections[0] = 10  # Initial infections
    
    for t in range(1, n_days):
        growth = r0 * recovery_rate * infections[t-1]
        noise = rng.normal(0, np.sqrt(max(infections[t-1], 1)))
        infections[t] = max(0, infections[t-1] + growth + noise)
        
        # Cap at population
        infections[t] = min(infections[t], 100000)
    
    # Convert to DataFrame for IPC
    df = pl.DataFrame({
        "day": range(n_days),
        "infections": infections,
        "r0": [r0] * n_days,
        "seed": [seed] * n_days
    })
    
    return to_ipc_tables({"results": df})


def mean_across_replicates(results: List[SimReturn]) -> SimReturn:
    """Compute mean across replicates for each time point."""
    # Extract DataFrames from IPC format
    dfs = []
    for result in results:
        tables = from_ipc_tables(result)
        dfs.append(tables["results"])
    
    # Combine all replicates
    combined = pl.concat(dfs)
    
    # Group by day and compute statistics
    aggregated = combined.group_by("day").agg([
        pl.col("infections").mean().alias("mean_infections"),
        pl.col("infections").std().alias("std_infections"),
        pl.col("infections").quantile(0.025).alias("lower_bound"),
        pl.col("infections").quantile(0.975).alias("upper_bound"),
        pl.len().alias("n_replicates")
    ]).sort("day")
    
    return to_ipc_tables({"aggregated": aggregated})


def percentile_aggregator(results: List[SimReturn]) -> SimReturn:
    """Compute percentiles across replicates."""
    dfs = []
    for result in results:
        tables = from_ipc_tables(result)
        dfs.append(tables["results"])
    
    combined = pl.concat(dfs)
    
    # Compute multiple percentiles
    aggregated = combined.group_by("day").agg([
        pl.col("infections").quantile(0.05).alias("p5"),
        pl.col("infections").quantile(0.25).alias("p25"),
        pl.col("infections").quantile(0.50).alias("p50_median"),
        pl.col("infections").quantile(0.75).alias("p75"),
        pl.col("infections").quantile(0.95).alias("p95"),
    ]).sort("day")
    
    return to_ipc_tables({"percentiles": aggregated})