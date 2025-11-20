# ModelOps Execution Environments: Technical Overview

**Document Purpose**: Technical reference for understanding cold and warm execution architectures in ModelOps, including current bugs and design issues.

**Date**: 2025-11-16
**Status**: Active development - cold executor has critical bugs

---

## Executive Summary

ModelOps provides two execution environments for running simulation tasks:

1. **Warm Executor (subprocess_runner.py)**: Long-lived subprocess pool with JSON-RPC communication (~50ms overhead per task)
2. **Cold Executor (cold_runner.py)**: Fresh process per task for maximum isolation (~500-1000ms overhead per task)

**Current Status**: Cold executor has critical bugs preventing it from working:
- ✅ Warm executor: Production-ready, works correctly
- ❌ Cold executor: Broken due to venv reuse bugs and inadequate dependency verification

---

## Architecture Comparison

### Warm Executor (Production)

```
┌─────────────────────────────────────────────────────────────┐
│ Dask Worker (has ModelOps installed)                        │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ WarmProcessManager                                    │  │
│  │ - Manages pool of subprocess_runner.py processes     │  │
│  │ - Each process lives for multiple tasks              │  │
│  │ - JSON-RPC 2.0 over stdin/stdout for communication   │  │
│  └──────────────────────────────────────────────────────┘  │
│                           │                                  │
│                           │ spawns N processes               │
│                           ▼                                  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Bundle Venv (NO ModelOps)                            │  │
│  │                                                       │  │
│  │  python subprocess_runner.py --bundle-path /path     │  │
│  │                                                       │  │
│  │  - Installs bundle deps on startup                   │  │
│  │  - Loads wire function once                          │  │
│  │  - Handles N tasks via JSON-RPC                      │  │
│  │  - Process lives until idle timeout                  │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

**Communication**: JSON-RPC 2.0 with Content-Length framing (LSP-style)

**Key Design Principle**: subprocess_runner.py is **completely standalone** with NO ModelOps dependencies. It runs in bundle venvs that only have user dependencies.

### Cold Executor (Broken)

```
┌─────────────────────────────────────────────────────────────┐
│ Dask Worker (has ModelOps installed)                        │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ ColdExecEnv                                           │  │
│  │ - Creates venv per bundle digest                     │  │
│  │ - Spawns fresh process per task                      │  │
│  │ - Reads result from stdout (one-shot JSON)           │  │
│  └──────────────────────────────────────────────────────┘  │
│                           │                                  │
│                           │ spawns 1 process per task        │
│                           ▼                                  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Bundle Venv (has ModelOps!)                          │  │
│  │                                                       │  │
│  │  python cold_runner.py --bundle-path /path < task    │  │
│  │                                                       │  │
│  │  - Installs bundle deps (with buggy verification)    │  │
│  │  - Loads wire function or target                     │  │
│  │  - Runs EXACTLY ONE task                             │  │
│  │  - Exits immediately (no state persists)             │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

**Communication**: Task JSON on stdin, result JSON on stdout (no persistent connection)

**Key Difference**: cold_runner.py is **part of ModelOps** and runs in the parent venv! This breaks the isolation principle from warm executor.

---

## Critical Bug Analysis

### Bug #1: Venv Digest Mismatch (CRITICAL)

**Location**: `src/modelops/adapters/exec_env/cold.py:330` and bundle resolution

**Symptom**: Bundle unpacked to `/tmp/modelops/bundles/bundles/17dc8d77fc01` but venv created as `d98f74186bc2-py3.12` (different digests!)

**Code**:
```python
# cold.py:330
venv_name = f"{digest[:16]}-py{sys.version_info.major}.{sys.version_info.minor}"
```

**Problem**: The `digest` parameter passed to `_get_or_create_venv()` doesn't match the actual bundle digest. This causes wrong venv reuse.

**Impact**: Tasks use venvs from DIFFERENT bundles, causing missing dependencies.

**Root Cause**: Unknown - need to trace where bundle_repo.ensure_local() gets its digest and why it doesn't match the bundle path.

### Bug #2: Inadequate Dependency Verification (CRITICAL)

**Location**: `src/modelops/worker/cold_runner.py:52-63`

**Code**:
```python
# Check if dependencies are already installed
if deps_marker.exists():
    # Verify installation by trying to discover wire function
    try:
        from importlib.metadata import entry_points
        eps = list(entry_points(group="modelops.wire"))
        if eps:
            logger.info(f"Dependencies already installed and verified")
            return  # ← BUG: Only checks wire function, not all deps!
    except Exception:
        # Discovery failed, need to reinstall
        logger.warning("Marker exists but wire discovery failed, will reinstall")
        deps_marker.unlink()
```

**Problem**: Verification only checks if the wire function is discoverable. It doesn't verify that ALL dependencies from pyproject.toml are installed.

**Scenario That Fails**:
1. Bundle A (no modelops-calabaria) creates venv, installs deps, writes `.deps_installed`
2. Bundle B (has modelops-calabaria) reuses same venv (due to Bug #1)
3. Wire function is still discoverable from Bundle A
4. Verification passes, but modelops-calabaria is NOT installed
5. Target import fails: `ModuleNotFoundError: No module named 'modelops_calabaria'`

**Fix Required**: Check for ALL dependencies in pyproject.toml, not just wire function. Or better: include bundle digest in venv name and never reuse across bundles.

### Bug #3: Doubled Bundle Path

**Location**: Bundle resolution in bundle_repo

**Evidence**: Bundle unpacked to `/tmp/modelops/bundles/bundles/17dc8d77fc01` (note "bundles" appears twice)

**Impact**: Minor but indicates sloppy path handling somewhere in bundle resolution.

### Bug #4: Cold Runner Not Standalone

**Design Flaw**: `cold_runner.py` is part of ModelOps package and runs with ModelOps installed in the venv. This violates the isolation principle established by `subprocess_runner.py`.

**Why It Matters**:
- subprocess_runner.py is **completely standalone** with NO ModelOps imports (see lines 5-38 of subprocess_runner.py)
- This allows bundle venvs to have ANY dependencies without conflicts
- cold_runner.py imports from modelops_contracts, breaking isolation

**Correct Design**: cold_runner.py should be standalone like subprocess_runner.py, using only stdlib and JSON serialization.

---

## Code Architecture Deep Dive

### Warm Executor: subprocess_runner.py

**Philosophy** (from lines 1-39):
```python
"""
CRITICAL: This module MUST remain standalone with NO ModelOps dependencies!

Why standalone is absolutely necessary:

1. This script runs inside isolated virtual environments (venvs) that contain
   ONLY the researcher/user's bundle dependencies, not ModelOps itself.

2. Environment isolation is crucial for:
   - Preventing dependency conflicts between bundles and ModelOps
   - Ensuring reproducible execution environments
   - Allowing bundles with incompatible dependencies to run on the same system
   - Maintaining clean separation between infrastructure (ModelOps) and science (bundles)

3. Even if ModelOps were available on PyPI, we would NOT install it in bundle
   venvs because:
   - Bundles may require different versions of libraries that ModelOps uses
   - We don't want bundle code to accidentally import/depend on ModelOps
   - The bundle environment should be exactly what the scientist specified

4. Communication pattern:
   - WarmProcessManager (has ModelOps) spawns this script with venv's Python
   - This script (no ModelOps) runs inside the venv
   - Communication via JSON-RPC 2.0 over stdin/stdout (language-agnostic)
   - All data serialized to JSON/base64 for clean boundary
"""
```

**Dependency Installation** (subprocess_runner.py:430-530):
```python
def ensure_dependencies_installed(bundle_path: Path, venv_path: Path) -> None:
    """Install bundle dependencies using file locking to prevent races."""
    deps_marker = venv_path / ".deps_installed"

    # Use file locking for concurrent safety
    lock_file = venv_path / ".install.lock"
    with open(lock_file, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        if deps_marker.exists():
            return  # Already installed by another process

        # Install from pyproject.toml or requirements.txt
        pyproject = bundle_path / "pyproject.toml"
        if pyproject.exists():
            subprocess.run([uv, "pip", "install", str(bundle_path)], check=True)

        deps_marker.write_text("installed")
```

**Wire Function Loading** (subprocess_runner.py:510-540):
```python
def load_wire_function(bundle_path: Path) -> Callable:
    """Discover and load wire function via entry points."""
    sys.path.insert(0, str(bundle_path))
    importlib.invalidate_caches()  # ← Critical after sys.path modification

    eps = list(entry_points(group="modelops.wire"))
    if not eps:
        raise RuntimeError("No modelops.wire entry point found")
    if len(eps) > 1:
        raise RuntimeError(f"Multiple wire entry points: {[ep.name for ep in eps]}")

    return eps[0].load()
```

**JSON-RPC Handler** (subprocess_runner.py:600-700):
```python
class SubprocessRunner:
    def __init__(self, bundle_path: Path, venv_path: Path):
        ensure_dependencies_installed(bundle_path, venv_path)
        self.wire_fn = load_wire_function(bundle_path)
        self.protocol = JSONRPCProtocol()

    def handle_sim_task(self, params: dict) -> dict:
        """Handle single simulation via wire function."""
        entrypoint = params["entrypoint"]
        param_dict = params["params"]
        seed = params["seed"]

        # Execute simulation
        result_bytes = self.wire_fn(entrypoint, param_dict, seed)

        # Serialize to JSON-safe format
        return {
            "task_id": compute_task_id(params, seed),
            "outputs": {
                name: {
                    "size": len(data),
                    "checksum": hashlib.blake2b(data).hexdigest(),
                    "inline": base64.b64encode(data).decode('ascii')
                }
                for name, data in result_bytes.items()
            }
        }

    def run(self):
        """Main loop: read JSON-RPC requests, handle, send responses."""
        while True:
            request = self.protocol.read_message()
            if request["method"] == "sim_task":
                result = self.handle_sim_task(request["params"])
                self.protocol.send_response(request["id"], result)
            elif request["method"] == "shutdown":
                break
```

**Key Strengths**:
1. ✅ Completely standalone - no ModelOps dependencies
2. ✅ Single dependency install per venv lifetime
3. ✅ Wire function loaded once, reused for N tasks
4. ✅ Minimal overhead after warmup (~50ms per task)
5. ✅ Proper cache invalidation after sys.path changes
6. ✅ File locking prevents concurrent installation races

### Cold Executor: cold_runner.py (BROKEN)

**Philosophy** (from lines 1-15):
```python
"""Single-task subprocess runner for cold execution.

This module is invoked as a fresh Python subprocess for each task.
It runs EXACTLY ONE task and exits immediately - no state persists.

This ensures complete isolation:
- Fresh Python interpreter
- Fresh module imports
- Fresh C++ extension loading (.so files)
- No cached globals or statics
"""
```

**Dependency Installation** (cold_runner.py:39-85):
```python
def ensure_dependencies_installed(bundle_path: Path) -> None:
    """Ensure bundle dependencies are installed in current venv."""
    venv_path = Path(sys.prefix)  # Current venv path
    deps_marker = venv_path / ".deps_installed"

    # BUG: Inadequate verification!
    if deps_marker.exists():
        try:
            from importlib.metadata import entry_points
            eps = list(entry_points(group="modelops.wire"))
            if eps:
                logger.info(f"Dependencies already installed and verified")
                return  # ← Returns without checking ALL deps!
        except Exception:
            logger.warning("Marker exists but wire discovery failed, will reinstall")
            deps_marker.unlink()

    # Install dependencies with file locking
    lock_file = venv_path / ".install.lock"
    with open(lock_file, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if deps_marker.exists():
            logger.info("Dependencies installed by another process")
            return  # ← BUG: Another process might have installed DIFFERENT bundle!

        _install_bundle_dependencies(bundle_path)
        deps_marker.write_text("installed")
```

**Simulation Execution** (cold_runner.py:185-277):
```python
def run_simulation_task(bundle_path: Path, task_json: str) -> str:
    """Run a single simulation task."""
    task_data = json.loads(task_json)
    entrypoint = task_data["entrypoint"]
    params = task_data["params"]
    seed = task_data["seed"]

    sys.path.insert(0, str(bundle_path))

    # Discover wire function
    from importlib.metadata import entry_points
    import importlib
    importlib.invalidate_caches()  # ← Added after bug fix
    eps = list(entry_points(group="modelops.wire"))

    wire_fn = eps[0].load()

    # Execute simulation (ONE TASK ONLY!)
    result_bytes = wire_fn(entrypoint, params, seed)

    # Return JSON dict to stdout
    return json.dumps({
        "task_id": tid,
        "outputs": serialize_outputs(result_bytes)
    })
```

**Aggregation Execution** (cold_runner.py:310-461):
```python
def run_aggregation_task(bundle_path: Path, task_json: str) -> str:
    """Run a single aggregation task."""
    agg_data = json.loads(task_json)
    target_entrypoint = agg_data["target_entrypoint"]
    sim_returns = deserialize_sim_returns(agg_data["sim_returns"])

    sys.path.insert(0, str(bundle_path))

    # Parse target entrypoint
    module_path, target_name = target_entrypoint.rsplit(":", 1)

    # Import target module
    import importlib
    importlib.invalidate_caches()  # ← Added after bug fix
    target_module = importlib.import_module(module_path)  # ← FAILS HERE!

    target_callable = getattr(target_module, target_name)

    # Execute target
    agg_result = target_callable(sim_returns)

    return json.dumps(agg_result)
```

**Key Weaknesses**:
1. ❌ NOT standalone - imports from modelops_contracts
2. ❌ Inadequate dependency verification (only checks wire function)
3. ❌ Venv reuse bugs cause wrong bundle dependencies
4. ❌ ~500-1000ms overhead per task (vs ~50ms for warm)
5. ❌ Dependencies re-verified on every process spawn
6. ✅ Cache invalidation added (but doesn't help if deps not installed)

### Cold Executor Parent: ColdExecEnv

**Venv Creation** (cold.py:310-358):
```python
def _get_or_create_venv(self, digest: str, bundle_path: Path) -> Path:
    """Get or create venv for bundle."""
    if self.force_fresh_venv:
        venv_name = f"{digest[:16]}-{uuid.uuid4().hex[:8]}"
    else:
        # BUG: digest doesn't match actual bundle digest!
        venv_name = f"{digest[:16]}-py{sys.version_info.major}.{sys.version_info.minor}"

    venv_path = self.venvs_dir / venv_name

    if venv_path.exists() and not self.force_fresh_venv:
        logger.debug(f"Reusing venv: {venv_path}")
        return venv_path  # ← Reuses even if bundle changed!

    # Create fresh venv
    subprocess.run(["uv", "venv", str(venv_path)], check=True)
    return venv_path
```

**Task Execution** (cold.py:82-190):
```python
def run(self, task: SimTask) -> SimReturn:
    """Execute simulation task in fresh subprocess."""
    # 1. Resolve bundle
    digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)

    # 2. Get or create venv
    venv_path = self._get_or_create_venv(digest, bundle_path)
    python_exe = venv_path / "bin" / "python"

    # 3. Serialize task
    task_data = {
        "entrypoint": str(task.entrypoint),
        "params": dict(task.params.params),
        "seed": task.seed,
    }
    task_json = json.dumps(task_data)

    # 4. Get cold_runner script path
    runner_script = Path(__file__).parent.parent.parent / "worker" / "cold_runner.py"

    # 5. Spawn fresh subprocess (exits after one task!)
    result = subprocess.run(
        [str(python_exe), "-u", str(runner_script), "--bundle-path", str(bundle_path)],
        input=task_json,
        capture_output=True,
        text=True,
        timeout=self.timeout_seconds,
        cwd=str(bundle_path),
        check=False,
    )

    # 6. Parse result
    if result.returncode != 0:
        return self._create_error_return(task, result.stderr, result.returncode)

    result_dict = json.loads(result.stdout)
    return reconstruct_sim_return(result_dict)
```

---

## Dependency Installation Comparison

### Warm Executor (subprocess_runner.py)

**File**: `subprocess_runner.py:430-530`

```python
def ensure_dependencies_installed(bundle_path: Path, venv_path: Path) -> None:
    """Install bundle dependencies using file locking."""
    deps_marker = venv_path / ".deps_installed"
    lock_file = venv_path / ".install.lock"

    with open(lock_file, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        # Check marker
        if deps_marker.exists():
            return

        # Install
        pyproject = bundle_path / "pyproject.toml"
        if pyproject.exists():
            if uv := shutil.which("uv"):
                subprocess.run([
                    uv, "pip", "install",
                    "--index-url", "https://pypi.org/simple",
                    "--python", sys.executable,
                    str(bundle_path)
                ], check=True)
            else:
                subprocess.run([
                    sys.executable, "-m", "pip", "install",
                    "--isolated", "--disable-pip-version-check",
                    str(bundle_path)
                ], check=True)

        # Mark as installed
        deps_marker.write_text("installed")
```

**Verification**: None! Just checks for `.deps_installed` marker file.

**Why It Works**: Each venv is tied to a specific bundle via naming. Venvs are never reused across different bundles.

### Cold Executor (cold_runner.py)

**File**: `cold_runner.py:39-85`

```python
def ensure_dependencies_installed(bundle_path: Path) -> None:
    """Ensure bundle dependencies are installed in current venv."""
    venv_path = Path(sys.prefix)
    deps_marker = venv_path / ".deps_installed"

    # BUG: This verification is inadequate!
    if deps_marker.exists():
        try:
            from importlib.metadata import entry_points
            eps = list(entry_points(group="modelops.wire"))
            if eps:
                # ✗ Only checks wire function, not all deps!
                logger.info(f"Dependencies already installed and verified")
                return
        except Exception:
            logger.warning("Marker exists but wire discovery failed, will reinstall")
            deps_marker.unlink()

    # Install with same logic as warm executor
    lock_file = venv_path / ".install.lock"
    with open(lock_file, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        if deps_marker.exists():
            # BUG: Another process might have installed DIFFERENT bundle!
            logger.info("Dependencies installed by another process")
            return

        logger.info(f"Installing dependencies for bundle: {bundle_path}")
        _install_bundle_dependencies(bundle_path)

        deps_marker.write_text("installed")
```

**Verification**: Checks if wire function is discoverable. This is wrong!

**Why It Fails**:
1. Bundle A (no calabaria) installs, writes marker
2. Bundle B (has calabaria) reuses venv due to Bug #1
3. Wire function still discoverable from Bundle A
4. Verification passes, but calabaria not installed
5. Import fails!

---

## Fix Recommendations

### Priority 1: Fix Venv Digest Mismatch (CRITICAL)

**Problem**: `digest` passed to `_get_or_create_venv()` doesn't match actual bundle digest.

**Fix Options**:

A. **Include full digest in venv name** (RECOMMENDED):
```python
# cold.py:330
venv_name = f"{digest}-py{sys.version_info.major}.{sys.version_info.minor}"
# Use FULL digest, not [:16]
```

B. **Verify bundle digest matches venv**:
```python
def _get_or_create_venv(self, digest: str, bundle_path: Path) -> Path:
    venv_name = f"{digest[:16]}-py{sys.version_info.major}.{sys.version_info.minor}"
    venv_path = self.venvs_dir / venv_name

    if venv_path.exists():
        # Verify digest matches
        marker = venv_path / ".bundle_digest"
        if marker.exists() and marker.read_text().strip() == digest:
            return venv_path
        else:
            # Wrong bundle! Delete and recreate
            shutil.rmtree(venv_path)

    # Create venv and write digest marker
    subprocess.run(["uv", "venv", str(venv_path)], check=True)
    (venv_path / ".bundle_digest").write_text(digest)
    return venv_path
```

### Priority 2: Fix Dependency Verification (CRITICAL)

**Problem**: Only checks wire function, not all deps.

**Fix Options**:

A. **Check for specific required packages** (QUICK FIX):
```python
def ensure_dependencies_installed(bundle_path: Path) -> None:
    venv_path = Path(sys.prefix)
    deps_marker = venv_path / ".deps_installed"

    if deps_marker.exists():
        # Verify ALL required packages are installed
        required_packages = _parse_requirements(bundle_path / "pyproject.toml")
        try:
            for pkg in required_packages:
                importlib.import_module(pkg)
            logger.info("All dependencies verified")
            return
        except ImportError as e:
            logger.warning(f"Missing dependency: {e.name}, will reinstall")
            deps_marker.unlink()

    # Install...
```

B. **Store bundle digest in marker** (BETTER):
```python
def ensure_dependencies_installed(bundle_path: Path) -> None:
    venv_path = Path(sys.prefix)

    # Include bundle digest in marker filename
    bundle_digest = compute_bundle_digest(bundle_path)
    deps_marker = venv_path / f".deps_installed_{bundle_digest[:16]}"

    if deps_marker.exists():
        logger.info("Dependencies for this bundle already installed")
        return

    # Install and mark with bundle-specific marker
    lock_file = venv_path / ".install.lock"
    with open(lock_file, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        if deps_marker.exists():
            return

        _install_bundle_dependencies(bundle_path)
        deps_marker.write_text(bundle_digest)
```

C. **Never reuse venvs** (SAFEST but SLOW):
```python
# cold.py: Always use force_fresh_venv=True
venv_name = f"{digest[:16]}-{uuid.uuid4().hex[:8]}"
```

### Priority 3: Make Cold Runner Standalone (DESIGN FIX)

**Problem**: cold_runner.py imports from modelops_contracts, breaking isolation.

**Fix**: Make cold_runner.py completely standalone like subprocess_runner.py:

1. Remove all modelops imports
2. Use plain dicts for data structures
3. Handle serialization with stdlib only

**Example**:
```python
# DON'T:
from modelops_contracts import SimReturn, TableArtifact

# DO:
def run_simulation_task(bundle_path: Path, task_json: str) -> str:
    """Run task and return plain dict (no modelops types)."""
    task_data = json.loads(task_json)

    # Execute...
    result_bytes = wire_fn(...)

    # Return plain dict
    return json.dumps({
        "task_id": "...",
        "outputs": {
            "name": {
                "size": 123,
                "checksum": "abc...",
                "inline": "base64data..."
            }
        }
    })
```

### Priority 4: Fix Doubled Bundle Path (LOW)

**Problem**: Bundle path is `/tmp/modelops/bundles/bundles/...` (doubled)

**Investigation Needed**: Trace through bundle_repo.ensure_local() to find where path construction doubles "bundles".

---

## Testing Recommendations

### Test Case 1: Different Bundles, Same Venv Name

Create two bundles with different dependencies:

**Bundle A**:
```toml
[project]
dependencies = ["numpy>=1.24.0"]
```

**Bundle B**:
```toml
[project]
dependencies = ["numpy>=1.24.0", "modelops-calabaria @ git+..."]
```

**Test**:
1. Run task from Bundle A → should create venv, install deps
2. Run task from Bundle B → should NOT reuse Bundle A's venv
3. Verify modelops-calabaria is available in Bundle B

**Expected Result**: Bundle B gets fresh venv or verification fails and reinstalls
**Current Result**: ❌ Bundle B reuses Bundle A's venv, calabaria missing

### Test Case 2: Concurrent Installation

**Test**:
1. Launch 10 tasks from same bundle in parallel
2. All tasks hit ensure_dependencies_installed() simultaneously

**Expected Result**: File locking prevents race, only one process installs
**Current Result**: ✅ File locking works (from warm executor testing)

### Test Case 3: Cache Invalidation

**Test**:
1. Create bundle with targets/incidence.py
2. Ensure targets/__init__.py exists
3. Run aggregation task

**Expected Result**: importlib.invalidate_caches() allows import
**Current Result**: ✅ Cache invalidation works (fixed in recent commit)

---

## Performance Comparison

### Warm Executor

**Per Task**:
- First task: ~500-1000ms (dependency install + wire function load)
- Subsequent tasks: ~50ms (just wire function call + serialization)

**Venv Lifecycle**:
- Created once per bundle digest
- Reused for all tasks from same bundle
- Process pool lives until idle timeout (default 5 min)

**Best For**: Production workloads with many tasks per bundle

### Cold Executor

**Per Task**:
- Every task: ~500-1000ms (process spawn + verification + execution)
- No warmup benefit
- Fresh Python interpreter per task

**Venv Lifecycle**:
- Created once per bundle digest (in theory)
- Reused across tasks (in theory)
- But verification is broken, causing wrong reuse

**Best For**: Debugging state leakage in native extensions (if it worked)

---

## Conclusions

### Warm Executor: ✅ Production Ready

**Strengths**:
- ✅ Completely standalone (no ModelOps in bundle venv)
- ✅ Robust file locking prevents races
- ✅ Efficient: ~50ms per task after warmup
- ✅ Proper cache invalidation
- ✅ Battle-tested and working

**Weaknesses**:
- ⚠️ State may persist across tasks (Python/C++ globals)
- ⚠️ Not suitable for debugging state leakage

### Cold Executor: ❌ Broken, Not Production Ready

**Critical Bugs**:
1. ❌ Venv digest mismatch causes wrong venv reuse
2. ❌ Inadequate dependency verification (only checks wire function)
3. ❌ Not standalone (imports from modelops_contracts)
4. ⚠️ Doubled bundle path suggests sloppy path handling

**If Fixed, Would Provide**:
- ✅ Complete isolation per task
- ✅ Fresh Python interpreter guarantees
- ✅ Useful for debugging state leakage

**But Currently**:
- ❌ Cannot run basic aggregation tasks
- ❌ Fails with ModuleNotFoundError for bundle deps
- ❌ ~10x slower than warm executor for no benefit

### Recommendation

1. **Use warm executor for production** - it works and is fast
2. **Fix cold executor or remove it** - current state is misleading
3. **If keeping cold executor**:
   - Fix venv digest matching (Priority 1)
   - Fix dependency verification (Priority 1)
   - Make cold_runner.py standalone (Priority 2)
   - Add comprehensive tests (Priority 3)

---

## Code Locations Reference

```
src/modelops/
├── adapters/exec_env/
│   ├── cold.py                      # ColdExecEnv parent (has ModelOps)
│   └── warm.py                      # WarmExecEnv parent (has ModelOps)
├── worker/
│   ├── cold_runner.py               # Cold subprocess (has ModelOps) ← BUG
│   ├── subprocess_runner.py         # Warm subprocess (standalone) ← WORKS
│   └── process_manager.py           # Manages warm pool
└── services/
    └── dask_simulation.py           # Dask integration
```

**Key Files to Review**:
1. `cold_runner.py:39-85` - Broken dependency verification
2. `cold.py:310-358` - Venv creation with digest bugs
3. `subprocess_runner.py:1-39` - Design philosophy (should apply to cold too)
4. `subprocess_runner.py:430-530` - Working dependency installation pattern

---

**End of Technical Overview**
