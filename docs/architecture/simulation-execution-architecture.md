# ModelOps Simulation Execution Architecture

## Overview

ModelOps orchestrates distributed simulation execution through a carefully designed system that balances performance with isolation. At its core, the architecture maintains pools of warm, isolated subprocesses on each Dask worker, achieving a **16.45x performance improvement** over cold starts while guaranteeing complete dependency isolation between different simulation bundles.

The system implements a hexagonal architecture where domain logic remains independent of infrastructure concerns, with Dask providing the distributed compute fabric and isolated subprocesses ensuring reproducible execution environments.

## Architecture

```
Dask Cluster
├── Scheduler
│   └── Distributes ModelOpsWorkerPlugin to all workers
│
└── Workers (multiple)
    ├── ModelOpsWorkerPlugin (composition root)
    │   ├── RuntimeConfig (from environment)
    │   ├── Creates all adapters
    │   └── Wires dependencies
    │
    ├── SimulationExecutor (domain layer)
    │   └── ExecutionEnvironment (port)
    │
    └── IsolatedWarmExecEnv (infrastructure)
        ├── BundleRepository (fetches code)
        ├── ProvenanceStore (stores outputs)
        └── WarmProcessManager
            └── Process Pool (OrderedDict)
                ├── Key: {bundle_digest}-py{version}-{deps_hash}
                └── WarmProcess: subprocess + JSON-RPC
```

## Core Design Principles

### 1. Hexagonal Architecture

The system strictly separates domain logic from infrastructure:

- **Domain Core**: `SimulationExecutor` knows nothing about Dask, subprocesses, or virtual environments
- **Ports**: Clean interfaces in `modelops-contracts` define boundaries
- **Adapters**: Infrastructure adapters handle all technical complexity
- **Dependency Inversion**: Core depends only on abstractions, never on concrete implementations

### 2. Process Isolation Model

Each simulation runs in complete isolation:

```
Bundle A (digest: abc123)
├── Dedicated subprocess
├── Own virtual environment at /tmp/venvs/abc123-py3.11-deps8f2a/
├── Frozen dependencies from uv.lock
└── No shared state with other bundles

Bundle B (digest: def456)  
├── Different subprocess
├── Own virtual environment at /tmp/venvs/def456-py3.11-deps3c1b/
├── Can have completely different dependencies
└── Runs concurrently without interference
```

### 3. Warm Pool Strategy

Performance through intelligent reuse:

- **First execution**: Create venv, install deps, start process (~42.5s)
- **Subsequent executions**: Reuse warm process (~2.6s)
- **Result**: 16.45x speedup for repeated executions
- **LRU eviction**: Automatically manages memory by evicting least-used processes

## Execution Flow

### Task Submission

1. **Client submits task** with bundle reference, parameters, and seed
2. **Dask schedules** task to available worker
3. **Worker plugin** ensures ModelOps runtime is initialized
4. **Task function** delegates to `SimulationExecutor`

### Bundle Resolution

1. **BundleRepository** fetches bundle from OCI registry or filesystem
2. **Content hash** becomes bundle digest (e.g., `sha256:abc123...`)
3. **Local path** cached at `/tmp/modelops/bundles/{digest}/`

### Process Management

1. **WarmProcessManager** checks for existing process with matching key
2. **Cache hit**: Reuse existing warm process
3. **Cache miss**: 
   - Acquire filesystem lock
   - Create virtual environment
   - Install dependencies
   - Start subprocess
   - Add to pool

### Execution

1. **Subprocess receives** task via JSON-RPC
2. **Wire function** discovered via Python entry points
3. **Simulation runs** in isolated environment
4. **Results returned** as artifacts
5. **Results stored** in ProvenanceStore (local or remote)

## Component Details

### SimulationService Port

The primary port published in `modelops-contracts` exposes the full API expected by calibration,
job runners, and CLI tools:

1. `submit(task)` – run a single `SimTask`
2. `submit_batch(tasks)` – efficient fan-out for many `SimTask` objects
3. `gather(futures)` – collect `SimReturn` objects in submission order
4. `submit_replicate_set(replicate_set, target_entrypoint)` – launch a `ReplicateSet` and optionally run worker-side aggregation (returns `AggregationReturn` when a target is provided)
5. `submit_batch_with_aggregation(replicate_sets, target_entrypoint)` – batch convenience for multiple replicate sets

Any SimulationService implementation (Dask, Ray, single-process adapters) must provide these methods,
which keeps higher-level code (Calabaria, job runner, CLI) independent of the underlying execution
engine.

### ExecutionEnvironment Port

The secondary port implemented by each worker adapter now includes `run_aggregation(aggregation_task)`
in addition to `run(sim_task)`, `health_check`, and `shutdown`. This allows isolated environments
to evaluate targets on-worker without hopping back to the scheduler. Adapters that do not support
aggregation can raise `NotImplementedError`, but the standard `IsolatedWarmExecEnv` and
`ColdDebugExecEnv` fully implement the method so worker-side aggregation works uniformly.

### ModelOpsWorkerPlugin

The composition root that creates and wires all components on worker initialization:

- **Loads configuration** from environment variables
- **Creates adapters** based on config (BundleRepository, ExecutionEnvironment, ProvenanceStore)  
- **Attaches executor** to worker for task access
- **Manages lifecycle** with setup/teardown hooks

This eliminates the need for singletons or global state - Dask's plugin system provides proper lifecycle management.

### WarmProcessManager

Maintains a pool of warm subprocesses with intelligent caching:

**Process Keying**:
```
{bundle_digest[:12]}-py{major}.{minor}-{deps_hash[:8]}
```
- Changes to code, dependencies, or Python version create new processes
- With `MODELOPS_FORCE_FRESH_VENV=true`: adds UUID suffix for debugging

**Pool Management**:
- OrderedDict tracks processes for LRU eviction
- Maximum 128 processes (configurable)
- Validates process health before reuse
- File locking prevents concurrent venv creation races

### SubprocessRunner

Executes inside each isolated subprocess:

**Venv Management**:
- Creates virtual environment only when needed
- File locking prevents corruption from concurrent creation
- Atomic marker files track installation state
- Validates wire function discovery after dependency installation

**Wire Protocol**:
- JSON-RPC 2.0 over stdio with Content-Length framing
- Case-insensitive header parsing (RFC compliance)
- Base64 encoding for binary artifacts
- Proper error propagation with stdout/stderr capture

### IsolatedWarmExecEnv

The infrastructure adapter that orchestrates execution:

1. **Bundle Resolution**: Fetches from OCI registry or filesystem
2. **Process Management**: Gets warm process from pool or creates new
3. **Task Execution**: Sends via JSON-RPC to subprocess
4. **Artifact Storage**: Routes to CAS or inlines based on size
5. **Error Handling**: Converts infrastructure errors to domain types

---


## Bug Fixes & Hardening

Through extensive production testing, we've identified and fixed critical issues that could cause silent failures or race conditions:

### Silent Subprocess Failures

**Symptom**: Tasks appeared successful but returned empty results  
**Root Cause**: Subprocess errors not propagated to parent process  
**Fix**: Capture and check both stdout/stderr, raise exceptions on non-zero exit codes

### Race Condition in Venv Creation

**Symptom**: Corrupt virtual environments when multiple workers started simultaneously  
**Root Cause**: No synchronization around venv creation  
**Fix**: File-based locking with fcntl ensures exclusive access during creation

### False-Positive Dependency Checks

**Symptom**: "Wire function not found" errors despite dependencies installed  
**Root Cause**: Marker file check didn't validate actual import capability  
**Fix**: Try importing wire function before trusting marker file, reinstall if import fails

### JSON-RPC Framing Issues  

**Symptom**: Intermittent "Missing Content-Length header" errors (-32700)  
**Root Cause**: Headers written as text, case-sensitive parsing  
**Fix**: Write headers as bytes with CRLF, case-insensitive header matching per LSP spec

### Missing Configuration Pass-Through

**Symptom**: Configuration values ignored despite being set  
**Root Cause**: Config fields not passed through adapter chain  
**Fix**: Complete wiring of all RuntimeConfig fields to their consumers

## Performance Characteristics

### Benchmark Results

Testing with 5 simulation tasks shows dramatic performance improvement from process pooling:

```
Benchmark 1: cached venv
  Time (mean ± σ):      2.582 s ±  0.371 s
  Range (min … max):    2.390 s …  3.606 s    10 runs

Benchmark 2: fresh venv  
  Time (mean ± σ):     42.486 s ±  2.603 s
  Range (min … max):   40.659 s … 49.510 s    10 runs

Summary: cached venv ran 16.45 ± 2.57 times faster
```

### Resource Usage

**Memory per component**:
- Warm subprocess: 200-500MB
- Virtual environment: 50-200MB (depends on dependencies)
- Bundle cache: Variable (typically 10-100MB per bundle)

**At scale (128 processes)**:
- Memory: 25-64GB total
- Disk: 10-25GB for venvs and bundles
- CPU: Minimal overhead when idle

## Configuration

### Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MODELOPS_BUNDLE_SOURCE` | "oci" | Bundle source: "oci" or "file" |
| `MODELOPS_BUNDLE_REGISTRY` | - | OCI registry (e.g., ghcr.io/org/models) |
| `MODELOPS_EXECUTOR_TYPE` | "isolated_warm" | Execution strategy |
| `MODELOPS_MAX_WARM_PROCESSES` | 128 | Maximum process pool size |
| `MODELOPS_VENVS_DIR` | "/tmp/modelops/venvs" | Virtual environments location |
| `MODELOPS_FORCE_FRESH_VENV` | false | Debug: force fresh venv every time |
| `MODELOPS_INLINE_ARTIFACT_MAX_BYTES` | 64000 | Max size for inline artifacts |

### Debug Mode

`MODELOPS_FORCE_FRESH_VENV=true` forces fresh virtual environment creation for debugging:
- Bypasses warm process cache entirely
- Creates new venv with UUID suffix for every execution
- ~16x slower but useful for troubleshooting dependency issues

## Engineering Tradeoffs

### Process Isolation vs Performance

We chose full process isolation over threads or shared memory:

**Benefits**:
- Complete dependency isolation between bundles
- Memory limits enforceable via resource controls
- Clean failure boundaries (process crash doesn't affect others)
- No GIL contention for CPU-bound simulations

**Costs**:
- Higher memory overhead (200-500MB per process)
- IPC overhead for communication
- Venv creation time (mitigated by warm pool)

### Warm Pool vs Serverless

We maintain long-lived warm processes rather than spinning up fresh ones:

**Benefits**:
- 16.45x performance improvement from reuse
- Amortized JIT compilation and initialization costs
- Hot caches for frequently-used bundles

**Tradeoffs**:
- Memory held even when idle
- Complexity of pool management
- Need for health monitoring

### Content-Based Caching

Using bundle digest as cache key provides automatic invalidation:

**Benefits**:
- Zero cache coherency issues
- Reproducible execution
- No manual invalidation needed

**Limitations**:
- Any code change creates new process
- Can't share processes across similar bundles
- Disk usage grows with bundle versions

## Future Enhancements

### Replicate Grouping for ABMs

Based on production experience with Agent-Based Models, we plan to add grouped execution:

**Benefits for ABMs**:
- Amortize heavy initialization (contact networks, JIT compilation)
- Control memory usage (one replicate at a time)
- Avoid network I/O for large outputs
- Prevent compute oversubscription

**Implementation approach**:
- Default to grouped for memory-intensive workloads
- Add policy flag for fan-out when appropriate
- Instrument Tinit, Texec, and memory usage for auto-selection

### Instrumentation & Observability

Key metrics to add:
- Bundle initialization time (Tinit)
- Per-replicate execution time (Texec)  
- Peak memory usage per process
- Output size distribution
- Network transfer costs

### Process Lifecycle Management

- Implement TTL-based recycling
- Add max reuse counts
- Graceful degradation under memory pressure
- Predictive warm-up for scheduled workloads

## Summary

The ModelOps simulation execution architecture delivers production-grade distributed simulation with:

- **Performance**: 16.45x speedup through warm process pooling
- **Isolation**: Complete separation between simulation bundles
- **Reliability**: Battle-tested fixes for race conditions and silent failures
- **Flexibility**: Clean architecture enabling future enhancements

The system balances the competing demands of performance, isolation, and resource efficiency while maintaining a clean separation between domain logic and infrastructure concerns.
