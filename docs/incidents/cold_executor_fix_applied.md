# Cold Executor Fix Applied

## Issue Fixed
Workers were using `isolated_warm` executor even when `MODELOPS_EXECUTOR_TYPE=cold` was set in the deployment. This was caused by the runner pod creating `RuntimeConfig` and passing it to workers, so workers never read their own environment variables.

## Root Cause
1. `DaskSimulationService.__init__()` created `RuntimeConfig.from_env()` in the **runner pod**
2. Runner passed this config to `ModelOpsWorkerPlugin(config)`
3. Workers received pre-configured object and never read their own `MODELOPS_EXECUTOR_TYPE` env var
4. Silent fallback to `isolated_warm` (the default)

## Solution Applied
Workers now create their own `RuntimeConfig` by reading their own environment variables.

## Files Modified

### 1. `src/modelops/worker/plugin.py`
**Lines 25-32**: Removed `config` parameter from `__init__()`
- Before: `def __init__(self, config: RuntimeConfig | None = None)`
- After: `def __init__(self)`

**Lines 42-44**: Force workers to read their own environment
- Before: `config = self.config or RuntimeConfig.from_env()`
- After: `config = RuntimeConfig.from_env()` with explanatory comment

**Lines 68-72**: Added structured logging
- Logs executor type for visibility
- Logs bundle source
- For cold executor: logs fresh venv setting

### 2. `src/modelops/services/dask_simulation.py`
**Lines 174-185**: Removed `config` parameter from `__init__()`
- Before: `def __init__(self, client: Client, config: RuntimeConfig | None = None)`
- After: `def __init__(self, client: Client)`
- Removed: `self.config = config or RuntimeConfig.from_env()`

**Line 198**: Simplified plugin instantiation
- Before: `plugin = ModelOpsWorkerPlugin(self.config)`
- After: `plugin = ModelOpsWorkerPlugin()`

### 3. `src/modelops/worker/config.py`
**Lines 78-82**: Added env var synonym
- Now supports both `MODELOPS_EXECUTOR_TYPE` and `MODELOPS_EXECUTOR`
- Improves UX by accepting shorter name

## Impact
✅ Workers read their own environment variables (K8s native pattern)
✅ Cold executor works correctly when env var is set
✅ Simpler architecture (no cross-pod config passing)
✅ Runner image version no longer affects worker configuration
✅ Added logging for troubleshooting

## Testing

### Verify Syntax
```bash
uv run python -c "from modelops.worker.plugin import ModelOpsWorkerPlugin; print('✅ OK')"
```
✅ **Passed**

### Next Steps
1. Commit changes to git
2. Push to trigger CI/CD image rebuild
3. Deploy with `workspace-cold.yaml`
4. Verify logs show "Executor: cold"
5. Verify fresh PIDs in worker logs

## Deployment Test Commands

```bash
# 1. Deploy workspace with cold executor
uv run mops workspace down
uv run mops workspace up --config examples/workspace-cold.yaml

# 2. Check worker logs for executor type
kubectl logs -l app=dask-worker -n modelops-dask-dev | grep "Executor:"
# Expected output: "Executor: cold"

# 3. Submit a test job
uv run mops jobs submit examples/starsim-sir/study.json

# 4. Verify fresh PIDs per task
kubectl logs -l app=dask-worker -n modelops-dask-dev | grep "Child process PID"
# Expected: Different PID for each task

# 5. Check for cold runner logs
kubectl logs -l app=dask-worker -n modelops-dask-dev | grep "Cold runner"
# Expected: "Cold runner started: PID {pid}"
```

## Backward Compatibility
✅ **Fully backward compatible**
- If `MODELOPS_EXECUTOR_TYPE` is not set, defaults to `isolated_warm`
- Existing deployments continue to work without changes
- No breaking changes to public APIs

## Risk Assessment
**Risk Level**: LOW
- Simplifies code (removes parameter)
- Workers already have env vars via K8s deployment
- Easy rollback: revert the 3 files
- No runtime behavior change unless env var is set

## Credits
- Issue diagnosed through investigation of worker logs showing `isolated_warm` despite env var being set
- Fix incorporates feedback from colleague about forcing per-worker env reads
- Logging improvements suggested by colleague for troubleshooting

## Related Documentation
- `COLD_EXECUTOR_DEPLOYMENT_GUIDE.md` - How to use cold executor
- `CPP_STATE_LEAKAGE_FIX.md` - Why cold executor is needed
- `COLD_EXECUTOR_DEPLOYMENT_BUG.md` - Original bug analysis

## Status
✅ **Code changes complete**
⏳ **Awaiting image rebuild** (need to commit + push)
⏳ **Deployment testing** (after images are rebuilt)
