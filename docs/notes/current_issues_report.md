# Critical Issues Report - ModelOps Execution Pipeline

## Executive Summary
After fixing the Python 3.13 compatibility issue, jobs are now hanging again with different symptoms:
1. Subprocess runners fail to initialize (JSON-RPC handshake failure)
2. No aggregated results are being written
3. Jobs run extremely slowly or stall completely
4. Venv reuse may be causing issues even with Python 3.12

## Current State

### Environment
- **Python**: 3.12.12 (downgraded from 3.13)
- **Dask**: 2024.10.0
- **Workers**: 4 workers with 2 processes each, 1 thread per process
- **Bundle**: `simulation-workflow@sha256:c09b3d20509f8492615984651297ae1a823a47fad362f9cdf999718bdef55ed3`

### Symptoms
1. Jobs submit 4000 tasks (200 replicates × 20 parameters) successfully
2. Workers receive tasks but fail to execute with:
   ```
   EOFError: Stream closed while reading headers
   RuntimeError: Failed to initialize process: Stream closed while reading headers
   ```
3. Job hangs indefinitely after task submission
4. No aggregated results in output directory

## Problem Analysis

### Issue 1: Subprocess Runner Initialization Failure

**Location**: `/usr/local/lib/python3.12/site-packages/modelops/worker/process_manager.py`

The subprocess runner starts but dies before the JSON-RPC handshake completes. The error occurs at:
```python
# Line 295 in _start_subprocess
result = warm_process.safe_call("ready", {}, timeout=10.0)
```

**Possible Causes**:
1. Bundle dependencies fail to install silently
2. Subprocess crashes during import/initialization
3. Something writes to stdout breaking JSON-RPC framing
4. Python version mismatch in the created venv

### Issue 2: Missing Aggregated Results

When checking synced results:
```bash
find results/dask-workers-769d4dff9-tp6l9 | rg agg
# Returns nothing - no aggregated results
```

This suggests the aggregation tasks (`agg-{param_id}`) are never completing.

### Issue 3: Venv Caching Issues

Without `MODELOPS_FORCE_FRESH_VENV=true`, the system reuses cached venvs:
- Cache key: `sha256:{bundle_hash}-py3.12-{deps_hash}`
- Problem: If the venv was created with wrong Python or failed deps, it gets reused
- The lock file mechanism may be preventing cleanup of bad venvs

## Code Flow for Debugging

### 1. Job Submission Flow
```python
# src/modelops/runners/job_runner.py
run_simulation_job(job, client)
  └── sim_service.submit_batch_with_aggregation()
      └── For each param_id:
          ├── Submit N replicate tasks (sim-{param_id}-{i})
          └── Submit 1 aggregation task (agg-{param_id})
              └── Depends on all replicate tasks
```

### 2. Worker Task Execution
```python
# src/modelops/adapters/exec_env/isolated_warm.py
IsolatedWarmExecutionEnvironment.run()
  └── process_manager.execute_task()
      └── process_manager.get_process()
          └── _create_process_with_lock()
              └── _start_subprocess() # FAILS HERE
                  ├── Creates venv if needed
                  ├── Installs dependencies
                  ├── Starts subprocess_runner.py
                  └── Calls "ready" via JSON-RPC # Dies before responding
```

### 3. Subprocess Runner Initialization
```python
# src/modelops/worker/subprocess_runner.py
class SubprocessRunner:
    def __init__(self):
        self._setup()  # Installs deps, may fail silently
        self._start_server()  # JSON-RPC server

    def _install_dependencies(self):
        # Tries uv first, falls back to pip
        # May write to stdout breaking JSON-RPC
```

## Key Files to Examine

1. **Process Manager**: `src/modelops/worker/process_manager.py`
   - Lines 290-323: Subprocess initialization
   - Missing proper stderr capture

2. **Subprocess Runner**: `src/modelops/worker/subprocess_runner.py`
   - Dependency installation logic
   - JSON-RPC server initialization
   - Potential stdout pollution

3. **Job Runner**: `src/modelops/runners/job_runner.py`
   - Aggregation task submission
   - Result gathering logic

4. **Dask Simulation Service**: `src/modelops/services/dask_simulation.py`
   - Task submission and dependency management
   - Aggregation task setup

## Immediate Debugging Steps

```bash
# 1. Check if subprocess can start manually
kubectl -n modelops-dask-dev exec -it deployment/dask-workers -- bash
cd /tmp/modelops/bundles/bundles/{bundle_hash}
/tmp/modelops/venvs/*/bin/python \
  /usr/local/lib/python3.12/site-packages/modelops/worker/subprocess_runner.py \
  --bundle-path . \
  --venv-path /tmp/modelops/venvs/{venv_dir} \
  --bundle-digest {digest} 2>&1

# 2. Check what's in the bundle that might be failing
kubectl -n modelops-dask-dev exec deployment/dask-workers -- \
  cat /tmp/modelops/bundles/bundles/*/pyproject.toml

# 3. Try with forced fresh venv
kubectl -n modelops-dask-dev set env deployment/dask-workers \
  MODELOPS_FORCE_FRESH_VENV=true

# 4. Check for stdout pollution
# Add to subprocess_runner.py early:
import sys, logging
logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
sys.stdout = sys.stderr  # Redirect all stdout to stderr temporarily
```

## Recommended Fixes

### 1. Improve subprocess error capture
```python
# In process_manager.py _start_subprocess()
# Add continuous stderr drain
import threading
def drain_stderr(proc):
    for line in iter(proc.stderr.readline, b''):
        logger.error(f"Subprocess: {line.decode('utf-8').rstrip()}")

stderr_thread = threading.Thread(target=drain_stderr, args=(process,))
stderr_thread.daemon = True
stderr_thread.start()
```

### 2. Add subprocess health check
```python
# Before calling "ready", check process is alive
import time
for _ in range(10):
    if process.poll() is not None:
        # Process died, capture any remaining stderr
        stderr = process.stderr.read() if process.stderr else b""
        raise RuntimeError(f"Process died with code {process.returncode}: {stderr}")
    time.sleep(0.5)
```

### 3. Fix venv caching
```python
# Add validation before reusing venv
def validate_venv(venv_path):
    # Check Python version matches
    result = subprocess.run(
        [venv_path / "bin/python", "--version"],
        capture_output=True
    )
    if not "3.12" in result.stdout.decode():
        shutil.rmtree(venv_path)
        return False
    return True
```

## Critical Questions

1. **Why is the subprocess dying before JSON-RPC init?**
   - Check if bundle has new dependencies not in cache
   - Check if imports are failing silently

2. **Why are aggregation tasks not completing?**
   - Are the replicate tasks actually finishing?
   - Is the aggregation dependency chain working?

3. **What changed between the working and non-working state?**
   - Bundle hash changed (new push?)
   - Venv cache might be corrupted

## Next Steps

1. Enable verbose logging in subprocess runner
2. Add proper stderr capture to see actual failure
3. Test subprocess initialization manually
4. Consider clearing all venv caches
5. Add health checks throughout the initialization chain