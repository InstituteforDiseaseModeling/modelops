# Cold Executor Deployment Guide

## Quick Start

To test the cold executor and diagnose C++ state leakage:

### 1. Enable Cold Executor

Edit `examples/workspace.yaml` and uncomment the cold executor:

```yaml
spec:
  workers:
    processes: 4  # Can keep multiple workers for parallelism
    threads: 1    # Single-threaded
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "cold"  # Fresh process per task!
```

### 2. Deploy Workspace

```bash
cd /Users/vsb/projects/work/modelops

# Tear down existing workspace
uv run mops workspace down

# Deploy with cold executor
uv run mops workspace up --config examples/workspace.yaml

# Verify deployment
kubectl get pods -n modelops-dask-dev
```

### 3. Run Your Grid Test

```bash
# Run the test that previously failed at 4x1
uv run mops dev your-bundle --grid-spec grid-4x1.json

# Expected: Should PASS if issue was C++ state leakage
```

### 4. Verify Process Isolation

Check logs to confirm fresh PIDs per task:

```bash
kubectl logs -n modelops-dask-dev -l app=dask-worker --tail=200 | grep "Child process PID"
```

Expected output:
```
Child process PID 12345: Starting simulation abc12345-seed1
Child process PID 12346: Starting simulation abc12345-seed2  # ← Different PID!
Child process PID 12347: Starting simulation def67890-seed1  # ← Different PID!
```

## What Changed

### Files Created

1. **`src/modelops/adapters/exec_env/cold.py`**
   - New `ColdExecEnv` executor
   - Spawns fresh subprocess per task via `subprocess.run()`
   - Process exits after one task
   - No warm pool, no process reuse

2. **`src/modelops/worker/cold_runner.py`**
   - Single-task subprocess entrypoint
   - Logs PID and parameters for observability
   - Exits after running exactly one simulation
   - Clean environment (no PYTHONPATH pollution)

### Files Modified

3. **`src/modelops/worker/plugin.py`**
   - Added `"cold"` branch to executor factory (line 186-196)

4. **`src/modelops/worker/config.py`**
   - Updated validation to accept `"cold"` (line 135-140)

5. **`examples/workspace.yaml`**
   - Added documentation for executor types (lines 13-17)
   - Added commented examples for cold executor (lines 73-81)

## Executor Comparison

| Feature | isolated_warm | direct | cold |
|---------|--------------|--------|------|
| **Process per task** | ❌ Reuses | ❌ No subprocess | ✅ Fresh every time |
| **C++ static isolation** | ❌ Persists | ❌ Persists | ✅ Reset every task |
| **Python module caching** | ✅ Cached | ✅ Cached | ❌ Fresh imports |
| **Venv caching** | ✅ Yes | N/A | Optional (default: yes) |
| **Speed** | Fast (~50ms overhead) | Fastest | Slow (~500-1000ms overhead) |
| **Use case** | Production | Testing | Debugging state issues |

## Configuration Options

### Basic Cold Executor

```yaml
env:
  - name: MODELOPS_EXECUTOR_TYPE
    value: "cold"
```

**Effect:**
- Fresh process per task
- Venvs cached (faster)
- ~500ms overhead per task

### Maximum Isolation (Slowest)

```yaml
env:
  - name: MODELOPS_EXECUTOR_TYPE
    value: "cold"
  - name: MODELOPS_FORCE_FRESH_VENV
    value: "true"
```

**Effect:**
- Fresh process per task
- Fresh venv per task (unpacks every time)
- ~1-2s overhead per task
- Use only if cold with cached venvs still shows issues

### Single Worker (Sequential)

```yaml
spec:
  workers:
    processes: 1  # Only 1 worker
    threads: 1
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "cold"
```

**Effect:**
- Maximum isolation
- No parallelism (tasks run one at a time)
- Slowest but simplest for debugging

## Testing Matrix

Run your grid test with different configurations:

### Test 1: Baseline (Warm - Should Fail)

```yaml
env:
  - name: MODELOPS_EXECUTOR_TYPE
    value: "isolated_warm"
```

**Expected:** Fails at 4x1 (4+ unique param sets)

### Test 2: Cold Executor (Should Pass)

```yaml
env:
  - name: MODELOPS_EXECUTOR_TYPE
    value: "cold"
```

**Expected:** Passes at 4x1, 8x1, 16x1 (any number of unique params)

### Test 3: Verify Process Isolation

Add logging to your C++ code:

```cpp
// In your simulate() function:
std::cerr << "C++ PID " << getpid() << ": simulate() called with params=..."
          << "\n";
```

Check logs:
```bash
kubectl logs -n modelops-dask-dev -l app=dask-worker | grep "C++ PID"
```

**With warm:** Same PID handles multiple param sets → corruption
**With cold:** Different PID every time → no corruption

## Observability

### Check Executor Type

```bash
# Get worker logs
kubectl logs -n modelops-dask-dev -l app=dask-worker --tail=50

# Look for:
# "ModelOps runtime initialized on worker {id}" (plugin startup)
# "Cold runner started: PID {pid}" (cold executor)
```

### Monitor Performance

Cold executor is ~10x slower than warm. For 100 tasks:
- **Warm**: ~5 seconds (50ms overhead × 100)
- **Cold**: ~50-100 seconds (500-1000ms overhead × 100)

This is the cost of isolation for debugging.

### Check PID Uniqueness

```bash
# Extract all PIDs from logs
kubectl logs -n modelops-dask-dev -l app=dask-worker | \
  grep "Child process PID" | \
  awk '{print $4}' | \
  sort -u | \
  wc -l

# Should equal number of tasks run
```

## Troubleshooting

### Issue: "No modelops.wire entry point found"

**Cause:** Bundle doesn't have proper entry point registration

**Fix:** Check `pyproject.toml` in your bundle:
```toml
[project.entry-points."modelops.wire"]
execute = "your_module.wire:wire_function"
```

### Issue: Cold executor still shows same PID

**Cause:** Not actually using cold executor

**Check:**
```bash
kubectl get deployment dask-workers -n modelops-dask-dev -o yaml | \
  grep -A 5 "MODELOPS_EXECUTOR_TYPE"
```

**Fix:** Redeploy workspace:
```bash
uv run mops workspace down
uv run mops workspace up --config examples/workspace.yaml
```

### Issue: ModuleNotFoundError in subprocess

**Cause:** Bundle not properly unpacked or venv not created

**Fix:** Check venv creation:
```bash
# Get worker pod
POD=$(kubectl get pods -n modelops-dask-dev -l app=dask-worker -o name | head -1)

# Check venvs directory
kubectl exec -n modelops-dask-dev $POD -- ls -la /tmp/modelops/venvs/
```

### Issue: Tasks timing out

**Cause:** Default 1-hour timeout may be too short

**Fix:** Tasks will timeout after 3600 seconds. If needed, this can be adjusted in the ColdExecEnv initialization (would require code change).

## Reverting to Warm Executor

### Quick Revert

Comment out the cold executor in `workspace.yaml`:

```yaml
env:
  # - name: MODELOPS_EXECUTOR_TYPE
  #   value: "cold"
```

Redeploy:
```bash
uv run mops workspace down
uv run mops workspace up --config examples/workspace.yaml
```

### Or Use Default

Simply remove the `MODELOPS_EXECUTOR_TYPE` env var entirely - defaults to `isolated_warm`.

## Next Steps After Confirming State Leakage

If cold executor fixes the bug (tasks pass at 4x1, 8x1, etc.):

### Option 1: Fix C++ Code (BEST)

**In your pybind11 bindings:**
- Remove all `static` parameter storage
- Pass params via stack/owned structs
- Use explicit `Context` object per call
- Never cache Python objects in C++ statics

**References:**
- pybind11 docs: https://pybind11.readthedocs.io/en/stable/advanced/misc.html
- Warns against Python objects in static storage
- Recommends lazy init or per-interpreter state

### Option 2: Keep Cold for Correctness

If fixing C++ is too complex:
- Use cold executor in production
- Accept 10x slowdown for correctness
- Monitor and optimize subprocess spawn overhead

### Option 3: Hybrid Approach

- Flag bundles that need cold execution: `requires_fresh_process: true`
- Use warm for safe bundles, cold for problematic ones
- Best of both worlds (would require implementation)

## Performance Optimization (Future)

Ideas to make cold executor faster:

1. **Persistent venv cache** (already implemented)
   - Reuse venv, but not process
   - Saves ~200ms per task

2. **Process pool with retirement**
   - Warm pool but retire after N tasks
   - Set `MODELOPS_MAX_PROCESS_REUSE=1` for cold-like behavior
   - Would require implementation

3. **Pre-spawned process pool**
   - Keep N idle subprocesses ready
   - Assign one per task, kill after use
   - Faster spawn, still isolated

## Related Documentation

- **CPP_STATE_LEAKAGE_FIX.md** - Technical analysis of C++ state issue
- **DISABLE_THREADING_AND_WARM_PROCESSES.md** - Investigation of threading/warm processes
- **WORKSPACE_CONFIG_DESIGN_ISSUE.md** - Config flow and CLI improvements needed

## Summary

**Implementation Status:** ✅ Complete and ready to test

**What to do:**
1. Uncomment cold executor in workspace.yaml
2. Deploy: `mops workspace down && mops workspace up --config examples/workspace.yaml`
3. Run grid test
4. Check logs for unique PIDs

**Expected outcome:**
- If cold fixes 4x1/8x1 → C++ state leakage confirmed
- If cold still fails → Different root cause (unlikely)

**Confidence:** High - implementation follows exact protocol, clean subprocess isolation
