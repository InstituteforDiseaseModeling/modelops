# Dask Deadlock Analysis - Update After Testing

Hi [Colleague],

Thank you for your analysis earlier. I've implemented and tested the suggested fixes, but unfortunately the deadlock persists at exactly the same point: **18/20 aggregations complete, 3998/4000 simulations complete**. I need your expertise to help identify what we're missing.

## What I've Implemented Based on Your Suggestions

### 1. Plugin Registration API
**Your suggestion**: Use `register_worker_plugin()` instead of `register_plugin()`

**What I found**: After researching the Dask documentation, I discovered that:
- `register_worker_plugin()` is actually DEPRECATED since Dask 2023.9.2
- `register_plugin()` is the current recommended API that handles all plugin types
- Our code was already using `register_plugin()`, which appears to be correct

**Current code** (src/modelops/services/dask_simulation.py:199):
```python
# Use the current API - register_plugin() handles all plugin types
# register_worker_plugin() is deprecated since 2023.9.2
self.client.register_plugin(plugin, name="modelops-runtime-v1")
```

**Question**: Could there be a different issue with how we're registering the plugin? The plugin inherits from `WorkerPlugin` and implements `setup()` and `teardown()` methods.

### 2. Key Truncation Fix
**Your suggestion**: Stop truncating param_id to 8 characters

**What I implemented**: Changed from `param_id[:8]` to full `param_id` in all task key generation:
```python
class TaskKeys:
    @staticmethod
    def sim_key(param_id: str, replicate_idx: int) -> str:
        # Use full param_id to avoid collisions
        return f"sim-{param_id}-{replicate_idx}"

    @staticmethod
    def agg_key(param_id: str) -> str:
        # Use full param_id to avoid collisions
        return f"agg-{param_id}"
```

**Verification**: I checked our actual study.json (20 parameter sets) and confirmed:
- No collisions even with 8-char truncation (probability was 0.000004%)
- All 20 param_ids are unique
- The param_id is a BLAKE2b-256 hash (64 hex chars) of canonicalized parameters

### 3. Direct Dependency Passing (Previously Implemented)
This was already in place from our earlier fix that got us from 4/20 to 18/20:
```python
def submit_replicate_set(self, replicate_set, target_entrypoint=None):
    # ... submit individual replicate tasks ...

    if target_entrypoint:
        # Pass futures as *args - Dask materializes them before calling
        agg_future = self.client.submit(
            _worker_run_aggregation_direct,
            *replicate_futures,  # Unpack as args, not gather()
            target_ep=target_entrypoint,
            bundle_ref=replicate_set.base_task.bundle_ref,
            key=TaskKeys.agg_key(param_id),
            **submit_kwargs
        )
```

### 4. Resource Constraints (Conditionally Applied)
Added aggregation resource constraints, but made them conditional to avoid test failures:
```python
# Check if any worker has aggregation resources
submit_kwargs = {"pure": False}
try:
    info = self.client.scheduler_info()
    has_aggregation_resource = any(
        'aggregation' in worker.get('resources', {})
        for worker in info.get('workers', {}).values()
    )
    if has_aggregation_resource:
        submit_kwargs['resources'] = {'aggregation': 1}
except Exception:
    pass  # Don't apply constraint if we can't check
```

Workers are configured with: `--resources aggregation=1`

## Current Symptoms (Unchanged)

1. **Consistent freeze point**: Always 18/20 aggregations, 3998/4000 simulations
2. **Pattern**: Exactly 2 aggregations and 2 simulations never complete
3. **Worker state**: Workers appear idle when frozen, not processing
4. **No errors**: No exceptions in worker logs, scheduler logs, or client

## Additional Information

### Environment
- **Dask version**: 2024.8.0 (distributed 2024.8.0)
- **Python**: 3.13
- **Kubernetes**: AKS with 4 worker pods
- **Worker config**: 4 processes per pod, 1 thread per process (16 total worker processes)
- **Job**: 20 parameter sets × 200 replicates = 4000 simulations + 20 aggregations

### Task Submission Pattern
```python
# For each of 20 parameter sets:
1. Submit 200 simulation tasks (sim-{param_id}-0 through sim-{param_id}-199)
2. Submit 1 aggregation task (agg-{param_id}) that depends on all 200 sims
3. Aggregation receives materialized results as *args from Dask
```

### The Mysterious "2 Missing" Pattern
- Missing: 2 aggregations (18/20 complete)
- Missing: 2 simulations (3998/4000 complete)
- This is NOT 2 × 200 = 400 missing simulations (which would indicate 2 full parameter sets)
- It's literally just 2 individual simulation tasks missing

## Questions for Further Investigation

1. **Scheduler vs Worker Plugin**: Could there be an issue with how `register_plugin()` determines whether to register on scheduler vs workers? Should we explicitly use a different registration method?

2. **Task Graph Visualization**: The pattern of exactly 2 missing tasks suggests something systematic. Could this be related to:
   - Task graph optimization/fusion?
   - Some tasks being marked as duplicates incorrectly?
   - A race condition in task scheduling?

3. **Memory/GC Issues**: With 4000 tasks, could we be hitting memory limits that cause silent task drops?

4. **Stderr Buffering**: You mentioned checking stderr handling. Our subprocess runner uses:
   ```python
   process = subprocess.Popen(..., stderr=subprocess.PIPE)
   ```
   Could full stderr buffers be blocking execution?

5. **Worker Process Lifecycle**: With 4 processes per pod, could there be an issue with:
   - Process pool exhaustion?
   - Plugin not being properly initialized on all processes?
   - Some processes losing their plugin state?

## Test Results

I tested both versions:
1. With the original `register_plugin()` - deadlock at 18/20
2. Briefly tried `register_worker_plugin()` (the deprecated one) - same deadlock at 18/20
3. Back to `register_plugin()` with full param_id - still deadlock at 18/20

## Next Steps?

Given that the obvious fixes haven't resolved the issue, I think we need to look deeper. Some ideas:

1. **Enable verbose Dask logging** to see exact task scheduling decisions
2. **Inspect the task graph** when frozen to see which specific tasks are stuck
3. **Check worker memory usage** at the freeze point
4. **Test with fewer replicates** (e.g., 10 instead of 200) to see if pattern changes
5. **Add explicit task tracking** to log every task start/complete to find the missing 2

Could you provide guidance on:
- Whether my understanding of `register_plugin()` vs `register_worker_plugin()` is correct?
- What could cause exactly 2 tasks to consistently fail in a 4020-task job?
- Any Dask internals that might explain this specific 18/20 pattern?
- Debugging strategies to identify which specific tasks are the 2 that never run?

Thank you for your continued help with this challenging issue!

## Appendix: Full Module Docstring

```python
"""Dask-based simulation service implementation.

IMPORTANT: Aggregation Deadlock Prevention
===========================================
This module implements critical deadlock prevention for aggregation tasks that depend
on large numbers of simulation tasks (e.g., 200 replicates per parameter set).

The Deadlock Pattern:
1. Each aggregation task depends on 200 simulation futures
2. With limited worker threads (e.g., 8 threads), aggregation tasks waiting for
   dependencies can consume all available threads
3. This prevents simulation tasks from running, creating a circular dependency

Solution Implemented (Oct 2025):
1. Direct dependency passing: Aggregation tasks receive simulation results as *args
   instead of calling gather() inside workers (commit d2d5f8)
2. Resource constraints: Aggregation tasks use resources={'aggregation': 1} to run
   only on workers configured with aggregation resources
3. Increased worker processes: Scale from 2 to 4 processes per pod for more threads

Without these measures, jobs freeze at 18/20 aggregations with 3998/4000 simulations
completed - a consistent pattern indicating thread starvation.
"""
```