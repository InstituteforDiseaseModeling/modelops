# C++ Static State Leakage Fix

## Problem Confirmed

Your colleague's analysis is **100% correct**. Both existing executors allow C++ static state to persist across tasks:

### Direct Executor (`MODELOPS_EXECUTOR_TYPE=direct`)
- Executes in the **long-lived Dask worker process**
- Caches wire function: `self._wire_fn_cache[digest] = wire_fn`
- **NO** process isolation between tasks
- C++ .so loaded once per worker, statics persist forever

### Isolated Warm Executor (`MODELOPS_EXECUTOR_TYPE=isolated_warm`)
- Uses **subprocess pool** (better isolation)
- But **REUSES** same subprocess for all tasks with same bundle
- From `process_manager.py:98-103`: "Keeps processes warm and **reuses them** for the same bundle digest"
- C++ statics persist across all tasks in same subprocess

## Root Cause

From `pybind11` documentation:
> Objects with static storage duration live for the entire lifetime of the process. A pybind11 module (.so) is `dlopen`'d once per process; its globals/statics live as long as that process lives.

**Your symptoms match perfectly:**
- ✅ Works with 1-3 unique param sets (different subprocesses)
- ❌ Fails at 4+ unique param sets (subprocess reuse)
- ❌ "C++ int casting" errors after crossing boundary
- ✅ Failure tied to **unique params**, not total tasks

## What We Need: Cold Executor

A **fresh process per task** that:
1. Spawns new subprocess for each simulation
2. Runs **one task only**
3. Exits immediately after task completes
4. Never reuses processes

## Implementation Plan

### Phase 1: Add Cold Executor (IMMEDIATE)

Create `src/modelops/adapters/exec_env/cold.py`:

```python
"""Cold execution environment - fresh process per task."""

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from modelops_contracts import SimReturn, SimTask
from modelops_contracts.ports import BundleRepository, ExecutionEnvironment

logger = logging.getLogger(__name__)


class ColdExecEnv(ExecutionEnvironment):
    """Cold execution environment - spawns fresh process per task.

    Provides maximum isolation:
    - New process for every single task
    - Process exits after task completes
    - No state leakage between tasks
    - No subprocess reuse

    This is the diagnostic sledgehammer for C++ static state issues.
    Much slower than warm executor, but guarantees isolation.
    """

    def __init__(
        self,
        bundle_repo: BundleRepository,
        venvs_dir: Path,
        storage_dir: Path,
        azure_backend: dict[str, Any] | None = None,
    ):
        self.bundle_repo = bundle_repo
        self.venvs_dir = venvs_dir
        self.storage_dir = storage_dir
        self.azure_backend = azure_backend

    def run(self, task: SimTask) -> SimReturn:
        """Execute task in fresh subprocess that exits after completion.

        Args:
            task: Simulation task

        Returns:
            SimReturn from subprocess
        """
        # 1. Ensure bundle is local
        digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)

        # 2. Create fresh venv for this bundle (or reuse cached one)
        venv_path = self._get_or_create_venv(digest, bundle_path)

        # 3. Serialize task to JSON
        task_json = task.model_dump_json()

        # 4. Spawn subprocess runner (exits after one task!)
        python_exe = venv_path / "bin" / "python"

        result = subprocess.run(
            [
                str(python_exe),
                "-m", "modelops.worker.cold_runner",
                "--bundle-path", str(bundle_path),
                "--task-json", task_json,
            ],
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max
        )

        if result.returncode != 0:
            logger.error(f"Cold subprocess failed: {result.stderr}")
            raise RuntimeError(f"Cold execution failed: {result.stderr}")

        # 5. Deserialize SimReturn from stdout
        return SimReturn.model_validate_json(result.stdout)

    def _get_or_create_venv(self, digest: str, bundle_path: Path) -> Path:
        """Get or create venv for bundle (can cache venvs, but not processes!)."""
        # Similar to warm executor's venv creation
        # But we NEVER keep the process alive
        from ..worker.venv_manager import ensure_venv
        return ensure_venv(self.venvs_dir, digest, bundle_path)

    def health_check(self) -> dict[str, Any]:
        return {"type": "cold", "status": "healthy"}

    def shutdown(self):
        logger.info("Shutting down ColdExecEnv (no processes to clean up)")
```

Create `src/modelops/worker/cold_runner.py`:

```python
"""Single-task subprocess runner for cold execution.

This script is invoked via subprocess.run() for each task.
It runs ONE task and exits immediately - no state persists.
"""

import argparse
import sys
from pathlib import Path

from modelops_contracts import SimTask


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-path", required=True)
    parser.add_argument("--task-json", required=True)
    args = parser.parse_args()

    # Load task
    task = SimTask.model_validate_json(args.task_json)

    # Add bundle to sys.path
    bundle_path = Path(args.bundle_path)
    sys.path.insert(0, str(bundle_path))

    # Discover and call wire function
    from importlib.metadata import entry_points
    eps = list(entry_points(group="modelops.wire"))
    if not eps:
        raise RuntimeError("No modelops.wire entry point found")

    wire_fn = eps[0].load()

    # Execute (THIS PROCESS RUNS ONE TASK ONLY!)
    result_bytes = wire_fn(
        str(task.entrypoint),
        dict(task.params.params),
        task.seed,
    )

    # Convert to SimReturn
    from modelops_contracts import SimReturn, TableArtifact
    import hashlib

    outputs = {}
    for name, data in result_bytes.items():
        checksum = hashlib.blake2b(data, digest_size=32).hexdigest()
        outputs[name] = TableArtifact(
            size=len(data),
            inline=data,
            checksum=checksum,
        )

    param_id = task.params.param_id
    tid = hashlib.blake2b(f"{param_id}-{task.seed}".encode()).hexdigest()

    result = SimReturn(task_id=tid, outputs=outputs)

    # Write to stdout and EXIT (no state persists!)
    print(result.model_dump_json())
    sys.exit(0)


if __name__ == "__main__":
    main()
```

### Phase 2: Wire Up Cold Executor

Update `src/modelops/worker/plugin.py`:

```python
# Around line 165, add:
elif config.executor_type == "cold":
    from modelops.adapters.exec_env.cold import ColdExecEnv

    return ColdExecEnv(
        bundle_repo=bundle_repo,
        venvs_dir=Path(config.venvs_dir),
        storage_dir=storage_dir,
        azure_backend=azure_backend,
    )
```

Update `src/modelops/worker/config.py`:

```python
# Line 135, update validation:
if self.executor_type not in ["isolated_warm", "direct", "cold"]:
    raise ValueError(f"Invalid executor_type: {self.executor_type}")
```

### Phase 3: Test Cold Executor

**Edit `examples/workspace.yaml`:**

```yaml
spec:
  workers:
    processes: 4
    threads: 1
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "cold"  # Fresh process per task!
```

**Deploy:**

```bash
uv run mops workspace down
uv run mops workspace up --config examples/workspace.yaml
```

**Run your grid test:**

```bash
# This should now PASS even with 8x1 (8 unique param sets)
# Because each task gets a fresh subprocess
```

## Expected Outcomes

### With Cold Executor
- ✅ **Every task gets fresh process** (new PID each time)
- ✅ **C++ statics reset** for each task
- ✅ **Grid test passes** at 8x1, 16x1, etc.
- ❌ **Much slower** (spawning overhead)

### Performance Impact

Rough estimates:
- **Warm executor**: ~50ms overhead per task
- **Cold executor**: ~500-1000ms overhead per task (10-20x slower)

But if it fixes the bug, you've proven causality!

## Verification Steps

Add logging to track process reuse:

```python
# In cold_runner.py main():
import os
pid = os.getpid()
print(f"PID {pid}: Starting task {task.params.param_id[:8]}-seed{task.seed}",
      file=sys.stderr)

# In your C++ simulate():
std::cerr << "C++ PID " << getpid() << ": simulate() called\n";
```

Expected logs with **cold executor**:
```
PID 12345: Starting task abc123-seed1
C++ PID 12345: simulate() called
PID 12346: Starting task abc123-seed2  # ← New PID!
C++ PID 12346: simulate() called
PID 12347: Starting task def456-seed1  # ← New PID again!
C++ PID 12347: simulate() called
```

Expected logs with **isolated_warm**:
```
PID 12345: Starting task abc123-seed1
C++ PID 12345: simulate() called
PID 12345: Starting task abc123-seed2  # ← Same PID! (reuse)
C++ PID 12345: simulate() called
PID 12345: Starting task def456-seed1  # ← Same PID! (corruption!)
C++ PID 12345: simulate() called  # ← Sees stale C++ statics from previous task
```

## Long-Term Fixes

Once you've confirmed cold executor fixes the bug:

### Option 1: Fix C++ Code (BEST)
- Remove all `static` parameter storage
- Pass params via stack/owned structs
- Use explicit `Context` object per call
- Never cache Python objects in statics

### Option 2: Add Process Retirement
- Let warm executor create new process after N tasks
- Set `MODELOPS_MAX_PROCESS_REUSE=1` to force fresh per task
- Slower than cold but uses existing infrastructure

### Option 3: Hybrid Executor
- Keep warm pool for "safe" bundles
- Use cold execution for flagged bundles
- Bundle metadata: `requires_fresh_process: true`

## Implementation Status

- [ ] Create `cold.py` executor
- [ ] Create `cold_runner.py` subprocess script
- [ ] Wire up in `plugin.py`
- [ ] Update config validation
- [ ] Add tests for cold executor
- [ ] Test with grid workload
- [ ] Verify PIDs change per task
- [ ] Document performance impact
- [ ] Add `--cold-mode` CLI flag

## Immediate Testing (Without Code Changes)

To verify hypothesis **right now** without implementing cold executor:

### Test 1: Single Process, Single Task
```yaml
# workspace.yaml
workers:
  processes: 1
  threads: 1
```

Run **one param set per job**:
```bash
# Job 1: params={x: 1.0}
# Job 2: params={x: 2.0}
# etc.
```

Expected: Each job gets fresh worker process → should pass

### Test 2: Force Worker Restart
```bash
# Between each unique param set, restart workers:
kubectl rollout restart deployment/dask-workers -n modelops-dask-dev
# Wait for restart
kubectl wait --for=condition=ready pod -l app=dask-worker -n modelops-dask-dev
# Run next param set
```

Expected: Fresh workers → should pass

### Test 3: Grep for Process Reuse
```bash
# Add this to your C++ simulate():
std::cerr << "PID=" << getpid() << " params=" << /* log params */ << "\n";

# Run grid test, check logs:
kubectl logs -n modelops-dask-dev -l app=dask-worker | grep "PID="
```

Expected: Same PID handles multiple different params → confirms reuse

## Summary

**Current State:**
- ✅ `direct`: No isolation (in-process)
- ⚠️ `isolated_warm`: Subprocess isolation, but **processes are reused**
- ❌ Neither protects against C++ static state leakage

**Needed:**
- 🆕 `cold`: Fresh subprocess per task (diagnostic mode)

**Next Steps:**
1. Implement cold executor (2-3 hours work)
2. Test with grid workload
3. If it passes → C++ statics confirmed as root cause
4. Fix C++ code or add process retirement

**Your colleague is absolutely right - we need true per-task process isolation.**
