# Disabling Dask Threading and Warm Process Workers

## Problem Statement

We need to investigate how to disable both:
1. **Dask worker threading** (use only single-threaded workers)
2. **Warm process workers** (use fresh processes for each simulation)

This is needed for debugging and isolating issues related to threading conflicts or warm worker state pollution.

## Current Architecture

### 1. Dask Worker Configuration

**Location**: `src/modelops/infra/components/workspace.py:532-543`

Workers are launched with:
```python
command=[
    "dask-worker",
    "tcp://dask-scheduler:8786",
    "--nworkers",
    str(worker_processes),  # From workspace.yaml: processes field
    "--nthreads",
    str(worker_threads),     # From workspace.yaml: threads field
    "--memory-limit",
    memory_limit,
    "--resources",
    "aggregation=1",
]
```

**Current values** (from `examples/workspace.yaml`):
- `processes: 4` (4 worker processes per pod)
- `threads: 1` (1 thread per process)
- Total: 4 processes × 1 thread = 4 single-threaded workers per pod

**To disable threading entirely**: Already done! `threads: 1` means single-threaded.

**To reduce concurrency further**: Set `processes: 1` for only one worker per pod.

### 2. Warm Process Pool

**Location**: `src/modelops/worker/config.py` + `src/modelops/worker/plugin.py`

**Current executor**: `isolated_warm` (uses warm process pool)

The worker plugin reads `executor_type` from environment:
```python
# In plugin.py:165
if config.executor_type == "isolated_warm":
    from modelops.adapters.exec_env.isolated_warm import IsolatedWarmExecEnv
    return IsolatedWarmExecEnv(
        bundle_repo=bundle_repo,
        venvs_dir=Path(config.venvs_dir),
        storage_dir=storage_dir,
        mem_limit_bytes=config.mem_limit_bytes,
        max_warm_processes=config.max_warm_processes,
        force_fresh_venv=config.force_fresh_venv,
        azure_backend=azure_backend,
    )
elif config.executor_type == "direct":
    from modelops.adapters.exec_env.direct import DirectExecEnv
    return DirectExecEnv(
        bundle_repo=bundle_repo,
        storage_dir=storage_dir,
        azure_backend=azure_backend,
    )
```

**Two executor types available**:
1. `isolated_warm` (default): Warm process pool with venv caching
2. `direct`: No warm processes, executes in worker process directly

## How to Disable Both

### Option 1: Single-Threaded, No Warm Processes (MOST ISOLATED)

**workspace.yaml**:
```yaml
spec:
  workers:
    replicas: 4
    processes: 1  # Only 1 worker per pod
    threads: 1    # Only 1 thread per worker
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "direct"  # No warm processes!
```

**Result**:
- 1 worker process per pod
- Single-threaded execution
- No warm process pool (fresh execution every time)
- Maximum isolation, minimal state sharing

**Trade-offs**:
- ❌ Much slower (no venv caching, no process reuse)
- ❌ Higher memory (venv unpacked every time)
- ✅ Maximum isolation for debugging
- ✅ No state leakage between simulations

### Option 2: Multi-Process, No Warm Processes (MODERATE)

**workspace.yaml**:
```yaml
spec:
  workers:
    replicas: 4
    processes: 4  # Multiple workers per pod
    threads: 1    # Single-threaded
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "direct"
```

**Result**:
- 4 worker processes per pod (parallelism maintained)
- Single-threaded execution
- No warm process pool
- Better throughput than Option 1

**Trade-offs**:
- ❌ Slower than warm pool (no venv caching)
- ✅ Decent parallelism (4 workers)
- ✅ No warm process state issues
- ⚠️ Workers still share pod (filesystem, network)

### Option 3: Single-Threaded with Warm Processes (CURRENT)

**workspace.yaml**:
```yaml
spec:
  workers:
    replicas: 4
    processes: 4  # Multiple workers
    threads: 1    # Single-threaded
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "isolated_warm"  # Default
```

**Result**:
- Current production configuration
- 4 single-threaded workers per pod
- Warm process pool with venv caching
- Fast but potential state leakage

**Trade-offs**:
- ✅ Fastest execution (venv cached)
- ✅ Good parallelism
- ❌ Potential for warm process state bugs
- ❌ Harder to debug state issues

### Option 4: Fresh Venvs with Warm Processes (DIAGNOSTIC)

**workspace.yaml**:
```yaml
spec:
  workers:
    processes: 4
    threads: 1
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "isolated_warm"
      - name: MODELOPS_FORCE_FRESH_VENV
        value: "true"  # Never reuse venvs!
```

**Result**:
- Uses warm processes but forces fresh venv every time
- Helps isolate whether issue is process reuse vs venv caching

**Trade-offs**:
- ❌ Slow (unpacks venv every simulation)
- ✅ Isolates venv caching from process reuse
- ✅ Good for diagnosing whether bug is in venv or process state

## Configuration Flow

### How Config Reaches Workers

```
workspace.yaml
  ↓
CLI: mops workspace up --config workspace.yaml
  ↓
Pulumi: DaskWorkspace ComponentResource
  ↓
Kubernetes: Deployment with env vars
  ↓
Pod starts → Worker starts → WorkerPlugin.setup()
  ↓
RuntimeConfig.from_env() reads environment
  ↓
plugin._make_execution_environment(config)
  ↓
Returns IsolatedWarmExecEnv or DirectExecEnv
```

### Available Environment Variables

From `src/modelops/worker/config.py:77-104`:

| Env Var | Default | Effect |
|---------|---------|--------|
| `MODELOPS_EXECUTOR_TYPE` | `"isolated_warm"` | `"direct"` disables warm processes |
| `MODELOPS_MAX_WARM_PROCESSES` | `128` | Max warm processes in pool |
| `MODELOPS_FORCE_FRESH_VENV` | `false` | `"true"` unpacks venv every time |
| `MODELOPS_VALIDATE_DEPS` | `true` | Check deps haven't changed on reuse |
| `MODELOPS_MAX_PROCESS_REUSE` | `1000` | Max times to reuse a process (future) |
| `MODELOPS_PROCESS_TTL` | `3600` | Max process age in seconds (future) |

### Workspace YAML Schema

```yaml
spec:
  workers:
    # Dask configuration
    processes: <int>  # --nworkers flag (number of worker processes per pod)
    threads: <int>    # --nthreads flag (threads per worker process)

    # Environment variables (passed to pods)
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "direct" | "isolated_warm"
      - name: MODELOPS_FORCE_FRESH_VENV
        value: "true" | "false"
      - name: MODELOPS_MAX_WARM_PROCESSES
        value: "<int>"
```

## Dask Worker Arguments

From Dask documentation and our implementation:

```bash
dask-worker tcp://scheduler:8786 \
  --nworkers 4 \      # Number of worker processes (our 'processes')
  --nthreads 1 \      # Threads per worker (our 'threads')
  --memory-limit 4GB  # Memory per worker
```

**Important**:
- `--nworkers` creates multiple **processes** (bypasses GIL)
- `--nthreads` creates **threads within each process** (subject to GIL)
- For pure Python (simulations): Use high `nworkers`, low `nthreads`
- For NumPy/Pandas: Use low `nworkers`, high `nthreads` (releases GIL)

## Testing Strategy

### Step 1: Baseline (Current Config)
```yaml
processes: 4
threads: 1
env:
  - name: MODELOPS_EXECUTOR_TYPE
    value: "isolated_warm"
```
**Expectation**: Fast but may show the bug

### Step 2: Disable Warm Processes
```yaml
processes: 4
threads: 1
env:
  - name: MODELOPS_EXECUTOR_TYPE
    value: "direct"
```
**Expectation**: Slower but if bug disappears → warm process issue

### Step 3: Single Worker (Maximum Isolation)
```yaml
processes: 1
threads: 1
env:
  - name: MODELOPS_EXECUTOR_TYPE
    value: "direct"
```
**Expectation**: Very slow but if bug disappears → concurrency issue

### Step 4: Force Fresh Venv (Isolate Venv Caching)
```yaml
processes: 4
threads: 1
env:
  - name: MODELOPS_EXECUTOR_TYPE
    value: "isolated_warm"
  - name: MODELOPS_FORCE_FRESH_VENV
    value: "true"
```
**Expectation**: Slow but if bug disappears → venv caching issue

## Implementation Tasks

### Phase 1: Document Current Behavior ✅
- [x] Identify where threading is configured (workspace.yaml + Pulumi)
- [x] Identify where warm processes are configured (RuntimeConfig)
- [x] Document environment variables and their effects
- [x] Create testing strategy

### Phase 2: Make Configuration User-Facing
- [ ] Add validation for `MODELOPS_EXECUTOR_TYPE` in workspace schema
- [ ] Add examples to workspace.yaml comments
- [ ] Document trade-offs in CLAUDE.md or user-facing docs
- [ ] Add CLI flag for quick testing: `mops workspace up --no-warm-processes`

### Phase 3: Enhanced Observability
- [ ] Log executor type on worker startup
- [ ] Log warm process pool stats (size, reuse count, cache hits)
- [ ] Add metrics for venv cache performance
- [ ] Add warning when `direct` executor is used (performance impact)

### Phase 4: Advanced Controls (Future)
- [ ] Implement `MODELOPS_MAX_PROCESS_REUSE` enforcement
- [ ] Implement `MODELOPS_PROCESS_TTL` enforcement
- [ ] Add warm process pool health checks
- [ ] Add graceful process retirement

## Quick Reference

### Disable Both Threading and Warm Processes (Debugging Mode)

**1. Edit workspace.yaml:**
```yaml
spec:
  workers:
    processes: 1  # Single worker
    threads: 1    # Single thread
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "direct"  # No warm processes
```

**2. Redeploy:**
```bash
cd /Users/vsb/projects/work/modelops
uv run mops workspace down
uv run mops workspace up --config examples/workspace.yaml
```

**3. Verify:**
```bash
# Check worker pods
kubectl get pods -n modelops-dask-dev

# Check worker logs for executor type
kubectl logs -n modelops-dask-dev -l app=dask-worker --tail=50 | grep -i "executor\|modelops runtime"
```

### Re-enable for Production

**Edit workspace.yaml:**
```yaml
spec:
  workers:
    processes: 4  # Restore parallelism
    threads: 1    # Keep single-threaded (pure Python)
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "isolated_warm"  # Re-enable warm processes
```

## Related Files

- **Workspace config**: `examples/workspace.yaml`
- **Pulumi deployment**: `src/modelops/infra/components/workspace.py`
- **Worker plugin**: `src/modelops/worker/plugin.py`
- **Runtime config**: `src/modelops/worker/config.py`
- **Warm executor**: `src/modelops/adapters/exec_env/isolated_warm.py`
- **Direct executor**: `src/modelops/adapters/exec_env/direct.py`

## Common Issues

### Issue 1: DirectExecEnv is Slower
**Expected**: Direct executor unpacks bundles every time, no venv caching
**Workaround**: Only use for debugging, not production

### Issue 2: Workers OOM with processes: 1
**Cause**: Each pod still requests 4Gi but only runs 1 worker
**Fix**: Adjust memory requests proportionally:
```yaml
resources:
  requests:
    memory: "4Gi"  # For processes: 4
    memory: "1Gi"  # For processes: 1
```

### Issue 3: Warm Processes Not Respecting Limits
**Cause**: `max_warm_processes` is per-worker, not global
**Calculation**: Total = `processes` × `max_warm_processes` × `replicas`
**Fix**: Reduce `MODELOPS_MAX_WARM_PROCESSES` or `processes`

### Issue 4: Config Changes Not Applied
**Cause**: Workspace config cached in Pulumi stack state
**Fix**: Redeploy with `workspace down` then `workspace up`

## Decision Tree

```
Is the bug related to threading?
├─ YES → Set threads: 1 (already done)
│
└─ NO → Is it related to process reuse?
    ├─ YES → Set MODELOPS_EXECUTOR_TYPE=direct
    │   └─ Still broken?
    │       └─ Set processes: 1 (maximum isolation)
    │
    └─ NO → Is it related to venv caching?
        └─ YES → Set MODELOPS_FORCE_FRESH_VENV=true
```

## Summary

**Current State**:
- Threading: ✅ Already disabled (`threads: 1`)
- Warm processes: ❌ Enabled (`executor_type: isolated_warm`)

**To Disable Warm Processes**:
- Set `MODELOPS_EXECUTOR_TYPE=direct` in workspace.yaml env section
- Redeploy workspace

**To Disable Both**:
- Set `processes: 1`, `threads: 1`, `MODELOPS_EXECUTOR_TYPE=direct`
- Provides maximum isolation for debugging

**Trade-offs**:
- Disabling warm processes → 2-5x slower execution
- Using direct executor → No venv caching, higher memory
- Using processes: 1 → Minimal parallelism

**Next Steps**: User should test with `MODELOPS_EXECUTOR_TYPE=direct` first to see if bug is related to warm process pool.
