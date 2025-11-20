# Cold Executor Deployment Bug - Post-Mortem

## Issue Summary

When deploying the cold executor via `workspace-cold.yaml` with `MODELOPS_EXECUTOR_TYPE=cold`, the system silently falls back to `isolated_warm` executor without any error.

## Root Cause #1: Config Created Client-Side

**The Problem:**

The `RuntimeConfig` is created **in the job runner pod**, NOT on worker pods:

```python
# src/modelops/services/dask_simulation.py:196
plugin = ModelOpsWorkerPlugin(self.config)  # self.config from runner pod!
```

**Why This Breaks Cold Executor:**

1. Runner pod has OLD image (without cold executor code)
2. Runner creates `RuntimeConfig.from_env()` in its own environment
3. Runner's OLD validation code only accepts `["isolated_warm", "direct"]`
4. Since "cold" is invalid, it falls back to default: `executor_type="isolated_warm"`
5. Runner passes this PRE-CONFIGURED object to workers
6. **Workers NEVER read `MODELOPS_EXECUTOR_TYPE` env var!**
7. Workers use isolated_warm silently

**Evidence:**

```bash
# Env var IS set in worker pods:
$ kubectl exec dask-workers-xxx -- env | grep MODELOPS_EXECUTOR_TYPE
MODELOPS_EXECUTOR_TYPE=cold

# But worker uses isolated_warm:
$ kubectl logs dask-workers-xxx | grep isolated_warm
File "/usr/local/lib/python3.12/site-packages/modelops/adapters/exec_env/isolated_warm.py"
```

## Root Cause #2: Images Don't Have Cold Executor Code

Even if config was correct, the Docker images are missing the cold executor:

```bash
# Cold executor doesn't exist in deployed image:
$ kubectl exec dask-workers-xxx -- python3 -c "from modelops.adapters.exec_env.cold import ColdExecEnv"
ModuleNotFoundError: No module named 'modelops.adapters.exec_env.cold'

# Validation still uses OLD code:
$ kubectl exec dask-workers-xxx -- python3 -c "..."
# Shows: if self.executor_type not in ["isolated_warm", "direct"]:
#                                       ^^^^^^^^^^^^^^^^^ NO "cold"!
```

**Files Missing:**
- `src/modelops/adapters/exec_env/cold.py` (375 lines)
- `src/modelops/worker/cold_runner.py` (285 lines)
- Updated `config.py` validation (line 135)
- Updated `plugin.py` factory (line 186-196)

## Root Cause #3: Silent Fallback

There's NO error when:
1. Validation rejects "cold" as invalid
2. Falls back to "isolated_warm" default
3. Workers use wrong executor

**Why No Error?**

- `register_plugin()` is async, doesn't wait for setup completion
- Runner logs "Worker plugin installed successfully" BEFORE setup finishes
- Any exceptions during `plugin.setup()` are swallowed by Dask
- No visibility into what executor was actually created

## Fix Required

### Short-term: Rebuild Images

1. **Commit cold executor code:**
   ```bash
   git add src/modelops/adapters/exec_env/cold.py
   git add src/modelops/worker/cold_runner.py
   git add src/modelops/worker/plugin.py  # Lines 186-196
   git add src/modelops/worker/config.py  # Line 135
   git commit -m "feat: add cold executor for C++ state isolation"
   git push origin main
   ```

2. **Wait for CI/CD to rebuild images:**
   - `modelops-dask-runner:latest`
   - `modelops-dask-worker:latest`
   - Takes ~5-10 minutes

3. **Restart workspace to pull new images:**
   ```bash
   uv run mops workspace down
   uv run mops workspace up --config examples/workspace-cold.yaml
   ```

### Medium-term: Fix Config Flow

**Option A: Workers Read Env Vars (Recommended)**

Change plugin to NOT accept pre-configured RuntimeConfig:

```python
# In ModelOpsWorkerPlugin.__init__:
def __init__(self):
    # Remove config parameter - force workers to read their own env
    pass

def setup(self, worker):
    # Workers create config from THEIR environment
    config = RuntimeConfig.from_env()
    config.validate()
    # ...
```

**Benefits:**
- Workers use their own env vars
- Runner image version doesn't matter
- Consistent with K8s ConfigMap pattern

**Option B: Pass Executor Type Only**

```python
plugin = ModelOpsWorkerPlugin(executor_type="cold")  # Just pass the string
```

Workers then read other settings from env.

### Long-term: Add Validation

1. **Preflight Validation:**
   - Before submitting job, test that executor can be created
   - Verify worker image has required modules
   - Check env vars are readable

2. **Explicit Error on Setup Failure:**
   ```python
   # In dask_simulation.py:
   result = self.client.register_plugin(plugin, name="modelops-runtime-v1")

   # Add: Wait for plugin to be ready or fail
   if not self._wait_for_plugin_ready(timeout=30):
       raise RuntimeError("Plugin setup failed on workers - check logs")
   ```

3. **Log Executor Type:**
   ```python
   # In plugin.py:
   logger.info(f"ModelOps runtime initialized with {config.executor_type} executor")
   ```

## Related Issues

### Issue #2: Missing Target Preflight Validation

**Problem:**

Job spec accepted `target_entrypoint: "targets.incidence:incidence_target"` but this function doesn't exist:

```
AttributeError: module 'targets.incidence' has no attribute 'incidence_target'
```

**Why Preflight Didn't Catch It:**

There IS NO preflight validation of target functions! The system only validates:
- Bundle structure (manifest.json exists)
- Entry point module exists
- requirements.txt format

But NOT:
- Target modules exist
- Target functions exist
- Target functions have correct signature

**Fix Required:**

Add target validation to job spec loader:

```python
def validate_job_spec(spec: JobSpec):
    # Existing validation...

    # NEW: Validate target entrypoint
    if spec.target_entrypoint:
        module_path, target_name = spec.target_entrypoint.rsplit(":", 1)

        # Check module exists (static analysis or import test)
        # Check function/class exists
        # Check signature matches expected: (List[SimReturn]) -> AggregationReturn
```

## Testing After Fix

1. **Verify Cold Executor Loads:**
   ```bash
   kubectl exec dask-workers-xxx -- python3 -c \
     "from modelops.adapters.exec_env.cold import ColdExecEnv; print('✅ Available')"
   ```

2. **Verify Validation Accepts Cold:**
   ```bash
   kubectl exec dask-workers-xxx -- python3 -c \
     "from modelops.worker.config import RuntimeConfig; \
      c = RuntimeConfig(executor_type='cold'); c.validate(); print('✅ Valid')"
   ```

3. **Check Logs for Fresh PIDs:**
   ```bash
   kubectl logs -l app=dask-worker | grep "Child process PID"
   # Should see DIFFERENT PID for each task
   ```

4. **Verify Executor Type:**
   ```bash
   kubectl logs -l app=dask-worker | grep "ModelOps runtime initialized"
   # Should say "ColdExecEnv" or "cold executor"
   ```

## Lessons Learned

1. **Config should be created where it's used** (workers, not runner)
2. **Async operations need explicit status checks** (don't assume success)
3. **Preflight validation should cover ALL referenced resources** (targets, models, etc.)
4. **Silent fallbacks are bugs** - fail fast and loud
5. **Image versioning matters** - `:latest` can mask deployment issues

## Timeline

- **20:52**: Workspace deployed with `MODELOPS_EXECUTOR_TYPE=cold`
- **20:53**: Job submitted, tasks start running
- **20:54**: Job fails with missing target (unrelated)
- **20:55**: Workers restarted to pick up env var
- **20:56**: Still using isolated_warm (config bug discovered)
- **21:00**: Investigation reveals:
  - Env var IS set in pods ✅
  - Cold executor code NOT in image ❌
  - Config created client-side ❌
  - No validation of targets ❌

## Status

- ❌ Cold executor NOT working (needs image rebuild)
- ❌ Target validation NOT implemented (needs code change)
- ✅ Root causes identified
- ✅ Fix plan documented
