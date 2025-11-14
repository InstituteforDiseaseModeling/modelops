# Cold Executor Quick Start

## What is the Cold Executor?

The cold executor spawns a **fresh Python process for every simulation task**. Each process:
- Runs exactly ONE task
- Exits immediately after completion
- Never shares C++ static state with other tasks
- Provides maximum isolation at the cost of performance (~10x slower)

## When to Use Cold Executor

Use the cold executor to diagnose and fix **C++ state leakage** issues:

### Symptoms of C++ State Leakage:
- ✅ Tests pass with 1-3 unique parameter sets
- ❌ Tests fail at 4+ unique parameter sets
- ❌ Results depend on task execution order
- ❌ Static/global variables in pybind11 extensions persist across tasks

### Root Cause:
With warm executor and `processes: 4`:
- Dask has 4 worker subprocesses
- When 5+ unique params run, one subprocess handles multiple parameter sets
- C++ static variables get corrupted after first parameter set

## Quick Deployment

### 1. Deploy Cold Executor Workspace

```bash
cd /Users/vsb/projects/work/modelops

# Tear down existing workspace
uv run mops workspace down

# Deploy with cold executor
uv run mops workspace up --config examples/workspace-cold.yaml

# Verify deployment
kubectl get pods -n modelops-dask-dev
```

### 2. Verify Cold Executor is Active

```bash
# Check environment variable in deployment
kubectl get deployment dask-workers -n modelops-dask-dev -o yaml | \
  grep -A 2 MODELOPS_EXECUTOR_TYPE

# Expected output:
# - name: MODELOPS_EXECUTOR_TYPE
#   value: cold
```

### 3. Run Your Test

```bash
# Run the test that previously failed at 4x1
uv run mops dev your-bundle --grid-spec grid-4x1.json

# Expected: Should PASS if issue was C++ state leakage
```

### 4. Verify Process Isolation

Check logs to confirm fresh PIDs per task:

```bash
kubectl logs -n modelops-dask-dev -l app=dask-worker --tail=200 | \
  grep "Child process PID"
```

Expected output:
```
Child process PID 12345: Starting simulation abc12345-seed1
Child process PID 12346: Starting simulation abc12345-seed2  # ← Different PID!
Child process PID 12347: Starting simulation def67890-seed1  # ← Different PID!
```

## Configuration Files

- **`workspace-cold.yaml`**: Cold executor workspace (ready to use)
- **`workspace.yaml`**: Standard workspace (cold executor commented out)

## Performance Impact

| Executor Type | Overhead per Task | Use Case |
|--------------|------------------|----------|
| `isolated_warm` | ~50ms | Production (default) |
| `cold` | ~500-1000ms | Debugging C++ state issues |
| `cold` + fresh venv | ~1-2s | Maximum isolation (if needed) |

## Maximum Isolation Mode

If cold executor with cached venvs still shows issues, enable fresh venv per task:

Edit `workspace-cold.yaml`:
```yaml
env:
  - name: MODELOPS_EXECUTOR_TYPE
    value: "cold"
  - name: MODELOPS_FORCE_FRESH_VENV  # Add this
    value: "true"
```

**Warning**: This is VERY slow (~1-2s overhead per task). Only use if absolutely necessary.

## Reverting to Warm Executor

```bash
# Deploy standard workspace
uv run mops workspace down
uv run mops workspace up --config examples/workspace.yaml
```

## Next Steps After Confirming C++ State Leakage

If cold executor fixes the bug (tasks pass at 4x1, 8x1, etc.):

### Option 1: Fix C++ Code (BEST)
- Remove all `static` parameter storage in pybind11 bindings
- Pass params via stack/owned structs
- Use explicit `Context` object per call
- Never cache Python objects in C++ statics

### Option 2: Keep Cold for Correctness
- Use cold executor in production
- Accept 10x slowdown for correctness
- Monitor and optimize subprocess spawn overhead

### Option 3: Hybrid Approach
- Flag bundles that need cold execution
- Use warm for safe bundles, cold for problematic ones

## Documentation

- **COLD_EXECUTOR_DEPLOYMENT_GUIDE.md** - Complete deployment guide
- **CPP_STATE_LEAKAGE_FIX.md** - Technical analysis of C++ state issue
- **DISABLE_THREADING_AND_WARM_PROCESSES.md** - Threading/warm process investigation

## Troubleshooting

### Issue: "No modelops.wire entry point found"

**Fix**: Check `pyproject.toml` in your bundle:
```toml
[project.entry-points."modelops.wire"]
execute = "your_module.wire:wire_function"
```

### Issue: Cold executor still shows same PID

**Check**: Verify deployment has cold executor enabled:
```bash
kubectl get deployment dask-workers -n modelops-dask-dev -o yaml | \
  grep -A 5 "MODELOPS_EXECUTOR_TYPE"
```

**Fix**: Redeploy workspace with correct config.

### Issue: Tasks timing out

**Cause**: Default 1-hour timeout may be too short for cold executor

**Current Limit**: Tasks timeout after 3600 seconds (1 hour)

## Implementation Status

✅ **Complete and ready to test**

**Files Created**:
- `src/modelops/adapters/exec_env/cold.py` (375 lines)
- `src/modelops/worker/cold_runner.py` (285 lines)
- `examples/workspace-cold.yaml` (ready-to-use config)

**Files Modified**:
- `src/modelops/worker/plugin.py` (wired cold executor)
- `src/modelops/worker/config.py` (validation updated)
- `examples/workspace.yaml` (documentation added)
