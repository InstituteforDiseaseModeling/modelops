# Target Losses Collection - Design Proposal

## Problem Statement

After running a simulation study with thousands of parameter sets and multiple targets, we need to collect all computed losses into a single Polars DataFrame for analysis. Currently, these losses are:
- Scattered across worker nodes in `AggregationReturn` objects
- Not easily accessible for optimization algorithms
- Memory-intensive if gathered all at once

## Current Architecture

### Data Flow
```
1. SimulationStudy defines parameter sets
   ↓
2. Each parameter set → N replicates (SimTask)
   ↓
3. N SimReturn objects with simulation outputs
   ↓
4. AggregationTask groups replicates by parameter set
   ↓
5. AggregationReturn contains:
   - loss: float (the target loss value)
   - diagnostics: Dict (additional metrics)
   - outputs: Dict[str, TableArtifact] (aggregated data)
```

### Key Data Structures

```python
@dataclass
class AggregationReturn:
    """Result from target evaluation/aggregation."""
    aggregation_id: str
    loss: float  # ← THIS IS WHAT WE WANT TO COLLECT
    diagnostics: Dict[str, Any]
    outputs: Dict[str, TableArtifact]
    n_replicates: int

    # MISSING: param_id to map back to parameters!

@dataclass
class AggregationTask:
    """Task for aggregating simulation results."""
    target_entrypoint: EntryPointId
    sim_returns: List[SimReturn]  # Results to aggregate
    bundle_ref: str

    # Can extract param_id from sim_returns[0].params.param_id
    # But this isn't available after aggregation completes
```

## Memory Challenges

### The Problem with Naive Collection
```python
# BAD: This could OOM on small client nodes
all_results = client.gather(aggregation_futures)  # Could be 10,000+ objects
df = pl.DataFrame(all_results)  # Huge memory spike
```

### Issues:
1. **Client memory bottleneck**: Gathering all results to client could exceed memory
2. **Large diagnostics**: Each result may have large diagnostic dictionaries
3. **Missing param_id**: Can't map losses to parameters without additional tracking
4. **Multiple targets**: Each param set may have multiple target losses

## Proposed Solution: Streaming Collection with Batching

### Design Overview
```python
def collect_target_losses_streaming(
    client: Client,
    job_id: str,
    aggregation_futures: List[Tuple[str, Future[AggregationReturn]]],  # (param_id, future)
    batch_size: int = 100,
    output_path: Optional[Path] = None
) -> pl.LazyFrame:
    """
    Collect target losses in batches to avoid memory issues.

    Returns a LazyFrame that can be materialized as needed.
    """

    # Write batches to temporary parquet files
    temp_dir = Path(f"/tmp/modelops/losses_{job_id}")
    temp_dir.mkdir(parents=True, exist_ok=True)

    batch_files = []

    # Process in batches to limit memory usage
    for i in range(0, len(aggregation_futures), batch_size):
        batch = aggregation_futures[i:i+batch_size]

        # Gather just this batch
        batch_param_ids = [param_id for param_id, _ in batch]
        batch_futures = [future for _, future in batch]
        batch_results = client.gather(batch_futures)

        # Extract minimal data (don't keep full diagnostics in memory)
        rows = []
        for param_id, result in zip(batch_param_ids, batch_results):
            rows.append({
                'job_id': job_id,
                'param_id': param_id,
                'loss': result.loss,
                'n_replicates': result.n_replicates,
                # Only keep summary of diagnostics, not full dict
                'diagnostic_keys': list(result.diagnostics.keys()),
                'has_outputs': len(result.outputs) > 0
            })

        # Write batch to parquet
        batch_df = pl.DataFrame(rows)
        batch_file = temp_dir / f"batch_{i:06d}.parquet"
        batch_df.write_parquet(batch_file)
        batch_files.append(batch_file)

        # Free memory
        del batch_results, rows, batch_df

    # Return lazy concatenation of all batches
    lazy_df = pl.concat([
        pl.scan_parquet(f) for f in batch_files
    ])

    # Optionally write final consolidated file
    if output_path:
        lazy_df.collect().write_parquet(output_path)

        # Clean up temp files
        for f in batch_files:
            f.unlink()
        temp_dir.rmdir()

    return lazy_df
```

### Integration Point: Job Runner

```python
# In src/modelops/runners/job_runner.py

class SimulationJobRunner:
    def run_study(self, study: SimulationStudy):
        # ... existing simulation code ...

        # Track param_id -> aggregation future mapping
        aggregation_futures_with_ids = []

        for param_set in study.parameter_sets:
            param_id = param_set.param_id

            # Run simulations for this param set
            sim_futures = [...]

            # For each target, create aggregation
            for target in study.targets:
                agg_future = client.submit(
                    aggregate_for_target,
                    sim_futures,
                    target,
                    key=f"agg-{param_id}-{target}"
                )

                # Track param_id with future
                aggregation_futures_with_ids.append((param_id, target, agg_future))

        # After all complete, collect losses
        losses_df = collect_target_losses_streaming(
            client,
            job_id,
            aggregation_futures_with_ids,
            batch_size=100  # Tune based on memory
        )

        # Upload summary to blob
        summary = losses_df.select([
            pl.col("param_id"),
            pl.col("loss").mean().alias("mean_loss"),
            pl.col("loss").std().alias("std_loss"),
        ]).collect()

        upload_to_blob(f"jobs/{job_id}/loss_summary.parquet", summary)
```

### For Multiple Targets

If there are multiple targets per parameter set:

```python
def collect_multi_target_losses(
    client: Client,
    job_id: str,
    aggregation_futures: List[Tuple[str, str, Future]],  # (param_id, target, future)
    batch_size: int = 100
) -> pl.LazyFrame:
    """Collect losses for multiple targets per param set."""

    temp_files = []

    for i in range(0, len(aggregation_futures), batch_size):
        batch = aggregation_futures[i:i+batch_size]

        rows = []
        for param_id, target, future in batch:
            result = future.result()  # or client.gather([future])[0]
            rows.append({
                'job_id': job_id,
                'param_id': param_id,
                'target': target,  # Which target this loss is for
                'loss': result.loss,
                'n_replicates': result.n_replicates,
            })

        # Write batch
        batch_df = pl.DataFrame(rows)
        # ... save to parquet ...

    # Result has multiple rows per param_id (one per target)
    # Can pivot if needed:
    return lazy_df.pivot(
        values="loss",
        index="param_id",
        columns="target"
    )
```

## Alternative: Worker-Side Aggregation

Instead of gathering to client, aggregate on workers:

```python
def collect_losses_on_worker(aggregation_results: List[AggregationReturn]) -> bytes:
    """Run on worker to pre-aggregate losses."""

    # Extract just losses (minimal memory)
    losses = [r.loss for r in aggregation_results]

    # Create minimal dataframe
    df = pl.DataFrame({
        'loss': losses,
        'count': len(losses)
    })

    # Return as bytes (Arrow IPC format)
    return df.write_ipc(None).getvalue()

# Submit to workers
futures = client.map(
    collect_losses_on_worker,
    grouped_aggregation_results,  # Group by worker
    workers=specific_workers
)

# Then gather the smaller, pre-aggregated results
aggregated_chunks = client.gather(futures)
```

## Required Changes

### 1. Track param_id with aggregation futures
Currently, once an `AggregationReturn` is computed, we lose the connection to which parameter set it came from.

**Options:**
a) Add `param_id` field to `AggregationReturn` (requires contract change)
b) Track mapping externally in job runner (proposed above)
c) Store mapping in ProvenanceStore metadata

### 2. Add collection step to job execution
After all aggregations complete, before job finishes, collect losses.

### 3. Handle multiple targets
The current proposal assumes we track which target each aggregation is for.

## Memory Analysis

### Current Memory Usage (Naive Approach)
- 10,000 parameter sets × 1 AggregationReturn × ~10KB each = ~100MB minimum
- If diagnostics are large (1MB each): 10GB!
- Plus DataFrame overhead: 2-3x

### Proposed Memory Usage (Streaming)
- Batch size 100 × ~10KB = ~1MB active memory
- Temporary files handle the rest
- Final LazyFrame doesn't materialize until needed

## API Usage Examples

```python
# Get losses for optimization
losses_df = collect_target_losses_streaming(
    client, job_id, futures, batch_size=50
)

# Analyze without loading all into memory
best_params = (
    losses_df
    .sort("loss")
    .head(10)
    .collect()  # Only materializes top 10
)

# Get summary statistics
summary = (
    losses_df
    .select([
        pl.col("loss").mean(),
        pl.col("loss").std(),
        pl.col("loss").min(),
        pl.col("loss").max(),
    ])
    .collect()  # Small result
)

# Save for later analysis
losses_df.collect().write_parquet("all_losses.parquet")
```

## Benefits of This Approach

1. **Memory efficient**: Batched processing prevents OOM
2. **Supports multiple targets**: Can collect all target losses
3. **Lazy evaluation**: Polars LazyFrame for efficient queries
4. **Persistent**: Saves to parquet for later analysis
5. **Distributed friendly**: Doesn't require all data on client

## Open Questions

1. **Where to store the param_id → aggregation mapping?**
   - Option A: Modify AggregationReturn to include param_id
   - Option B: Track in job runner (current proposal)
   - Option C: Store in metadata/manifest file

2. **How to handle failed aggregations?**
   - Include with NaN loss?
   - Skip entirely?
   - Track separately?

3. **Should we compute summary statistics on workers?**
   - Could reduce data transfer further
   - But limits flexibility

4. **Default batch size?**
   - Depends on typical diagnostic size
   - Could auto-tune based on first batch

## Implementation Status

- ❌ Streaming collection function
- ❌ Integration with job runner
- ❌ Param_id tracking
- ❌ Multiple target support
- ❌ Polars-based implementation
- ❌ Worker-side pre-aggregation option

## Next Steps

1. Decide on param_id tracking approach
2. Implement streaming collection
3. Add to job runner
4. Test with large-scale jobs
5. Add CLI command for post-hoc collection

This proposal provides automatic support for all target losses while being memory-efficient and using Polars for better performance.