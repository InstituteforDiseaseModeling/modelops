# Warm Process Pool Design

## Overview

The ModelOps warm process pool maintains a cache of isolated subprocess environments for efficient simulation execution. Each subprocess runs in its own virtual environment with frozen dependencies, providing both isolation and performance through intelligent reuse.

## Architecture

```
WarmProcessManager (per Dask worker)
├── Process Pool (OrderedDict with LRU eviction)
│   ├── Process Key: {bundle_digest}-py{version}-{deps_hash}
│   └── WarmProcess: subprocess + JSON-RPC client
└── Filesystem Lock Manager (prevents concurrent venv creation)
```

## Cache Policy

### Process Keying

Processes are uniquely identified by a composite key:
```
{bundle_digest}-py{major}.{minor}-{deps_hash}
```

Where:
- `bundle_digest`: SHA256 hash of bundle content (code)
- `py{major}.{minor}`: Python version (e.g., py3.11)
- `deps_hash`: BLAKE2b hash of dependency files (uv.lock, requirements.txt, pyproject.toml)

### Reuse Strategy

1. **Cache Hit**: When a task matches an existing process key
   - Process is moved to end of LRU queue
   - Reuse counter incremented
   - Task executed immediately

2. **Cache Miss**: When no matching process exists
   - If pool full (default 128), evict LRU process
   - Create new virtual environment
   - Install dependencies
   - Start subprocess with JSON-RPC server
   - Cache for future reuse

### Eviction Policy

Processes are evicted when:
- Pool reaches `max_processes` limit (LRU eviction)
- Process dies unexpectedly
- Explicit shutdown requested
- (Future) TTL exceeded or max reuse count reached

## Isolation Guarantees

### Per-Process Isolation

Each process maintains:
- **Dedicated virtual environment**: Located at `venvs_dir/{key}/`
- **Frozen dependencies**: Installed once at process creation
- **Clean module cache**: No pollution between bundles
- **Single bundle commitment**: Process serves only one bundle digest forever

### Dependency Isolation

Dependencies are isolated through:
1. **Venv separation**: Each key gets its own virtual environment
2. **No shadowing**: Installed packages not shadowed by source paths
3. **Import cache clearing**: `importlib.invalidate_caches()` on setup
4. **Dependency validation**: Hash verification ensures correct deps

### Concurrency Safety

Protected against race conditions via:
- **Filesystem locks**: Prevent concurrent venv creation
- **Atomic operations**: Check-then-create wrapped in exclusive locks
- **Validation on reuse**: Verify process still serves expected digest

## Invalidation Triggers

A new process is created when:

1. **Bundle Changes**
   - Source code modified → new bundle_digest
   - Entry points changed → new bundle_digest

2. **Dependency Changes**
   - `uv.lock` modified → new deps_hash
   - `requirements.txt` updated → new deps_hash
   - `pyproject.toml` dependencies changed → new deps_hash

3. **Python Version Changes**
   - Different Python interpreter → new version in key

4. **Process Failures**
   - Subprocess dies → removed from pool
   - JSON-RPC errors → process terminated

## Implementation Details

### Virtual Environment Structure

```
/tmp/modelops/venvs/
├── {digest1}-py3.11-{deps_hash1}/
│   ├── bin/
│   │   └── python
│   ├── lib/
│   └── .deps_hash  # Stored for validation
├── {digest2}-py3.11-{deps_hash2}/
└── *.lock  # Filesystem locks for creation
```

### Process Lifecycle

1. **Creation**
   ```python
   1. Acquire filesystem lock on venv_key.lock
   2. Check if venv already exists (race protection)
   3. Create virtual environment: uv venv {path}
   4. Install dependencies: uv pip install -r requirements.txt
   5. Start subprocess with JSON-RPC server
   6. Wait for ready signal
   7. Release lock
   ```

2. **Execution**
   ```python
   1. Receive task via execute_task()
   2. Get or create warm process for bundle
   3. Send task via JSON-RPC
   4. Wait for response
   5. Return results
   ```

3. **Termination**
   ```python
   1. Send terminate signal (graceful)
   2. Wait for process exit (timeout 5s)
   3. Force kill if needed
   4. Remove from pool
   ```

### JSON-RPC Protocol

Communication between manager and subprocess:

```python
# Request
{
    "jsonrpc": "2.0",
    "method": "execute",
    "params": {
        "entrypoint": "model.scenarios/baseline",
        "params": {"alpha": 0.5},
        "seed": 42,
        "bundle_digest": "abc123..."  # Optional validation
    },
    "id": 1
}

# Response
{
    "jsonrpc": "2.0",
    "result": {
        "output1": "base64_encoded_data",
        "output2": "base64_encoded_data"
    },
    "id": 1
}
```

### Timeout & Hung Process Handling

Warm processes now enforce an upper bound on how long the manager waits for JSON-RPC responses:

- `WarmProcessManager` accepts `rpc_timeout_seconds` (default 30 minutes, override with `MODELOPS_RPC_TIMEOUT_SECONDS`). Every `execute` and `aggregate` call passes this timeout to `WarmProcess.safe_call`.
- `WarmProcess` stores a `default_timeout` so per-call overrides can be applied while maintaining a consistent limit for all other requests.
- `JSONRPCClient` now runs a background reader thread that demultiplexes responses into per-request queues. This allows each call to block with its own timeout instead of a single global `read_message()` loop.
- If a subprocess never replies (e.g., model deadlocks or blocks on input), the client raises `TimeoutError`. The manager terminates the warm process, removes it from the pool, and surfaces a clear error rather than hanging indefinitely.

This design keeps fast simulations unaffected while giving operators a safety valve for hung or misbehaving bundles. Long-running models can increase the timeout via environment configuration when needed.

## Configuration

Key configuration parameters:

```python
class RuntimeConfig:
    # Pool size
    max_warm_processes: int = 128
    
    # Isolation
    force_fresh_venv: bool = False  # Debug: never reuse
    validate_deps_on_reuse: bool = True
    
    # Future enhancements
    max_process_reuse_count: int = 1000
    process_ttl_seconds: int = 3600
```

## Monitoring & Debugging

### Metrics to Track

- **Pool utilization**: Active vs max processes
- **Cache hit rate**: Reuse vs creation ratio
- **Process age**: Time since creation
- **Reuse count**: Uses per process
- **Creation time**: Venv setup duration

### Debug Logging

Key log points:
- Process creation/reuse decisions
- Dependency hash computation
- Eviction events
- Validation failures
- Lock acquisition/release

## Security Considerations

1. **No shared state**: Processes fully isolated
2. **No module pollution**: Clean imports per process
3. **Digest validation**: Ensure correct bundle execution
4. **Filesystem permissions**: Venvs readable only by owner

## Future Enhancements

1. **TTL-based eviction**: Restart long-running processes
2. **Health checks**: Periodic validation of process state
3. **Dependency drift detection**: Warn if lockfile missing
4. **Memory limits**: Enforce per-process memory constraints
5. **GPU affinity**: Pin processes to specific GPUs

## Testing Strategy

Critical test scenarios:
1. Different bundles get different processes
2. Same bundle+deps reuses process
3. Dependency changes trigger new process
4. Concurrent creation doesn't corrupt venvs
5. Process death handled gracefully
6. LRU eviction works correctly
7. Python version isolation maintained

## Performance Benchmarks

### Warm Process Pool vs Fresh Venv Creation

Benchmark comparing cached venv reuse vs forced fresh venv creation for every execution:

```bash
hyperfine \
  --command-name 'cached venv' \
    'uv run python examples/test_simulation_e2e.py' \
  --command-name 'fresh venv' \
    'MODELOPS_FORCE_FRESH_VENV=true uv run python examples/test_simulation_e2e.py'
```

**Results:**
```
Benchmark 1: cached venv
  Time (mean ± σ):      2.582 s ±  0.371 s    [User: 0.326 s, System: 0.088 s]
  Range (min … max):    2.390 s …  3.606 s    10 runs

Benchmark 2: fresh venv
  Time (mean ± σ):     42.486 s ±  2.603 s    [User: 0.395 s, System: 0.124 s]
  Range (min … max):   40.659 s … 49.510 s    10 runs

Summary
  cached venv ran
   16.45 ± 2.57 times faster than fresh venv
```

**Key Findings:**
- Warm process pool provides **16.45x speedup** over fresh venv creation
- Cached execution: ~2.6 seconds average
- Fresh venv creation: ~42.5 seconds average
- The performance gain justifies the added complexity of process pool management
- `MODELOPS_FORCE_FRESH_VENV=true` remains available for debugging dependency issues

## Known Issues & Solutions

### JSON-RPC Subprocess Deadlock with Large Messages

**Issue**: When sending large messages (>65KB) through subprocess pipes, a deadlock can occur between parent and child processes.

**Root Cause**: 
The subprocess logs to stderr while the parent writes to stdin. If the stderr pipe buffer fills (default ~65KB on many systems), the subprocess blocks on stderr write while the parent blocks on stdin write, creating a deadlock.

**Deadlock Sequence**:
1. Parent sends large message (70KB) → writes to subprocess stdin
2. Subprocess starts reading the message headers
3. While reading, subprocess logs something → writes to stderr
4. Stderr pipe buffer fills up (buffer full at ~65KB)
5. Subprocess blocks on stderr write
6. Parent is still writing to stdin (hasn't finished the 70KB)
7. **DEADLOCK**: Parent blocked on stdin write, child blocked on stderr write

**Solution Implemented**: 
Changed `stderr=subprocess.PIPE` to `stderr=subprocess.DEVNULL` in `process_manager.py` to prevent the deadlock. This prevents the stderr buffer from filling up and blocking the subprocess.

**Alternative Solutions for Future**:
1. **Thread-based stderr reader**: Continuously drain stderr in a separate thread
2. **Larger pipe buffer**: Some systems allow increasing pipe buffer size via fcntl
3. **Conditional logging**: Disable subprocess logging in production mode
4. **File-based logging**: Redirect stderr to a file instead of pipe
5. **Async I/O**: Use asyncio for non-blocking pipe operations

**Trade-offs**:
- Current solution (DEVNULL) loses subprocess logs but prevents deadlock
- Logs are primarily for debugging during development
- Production stability takes precedence over debug visibility
- Can add environment variable to enable stderr capture with proper reader thread if needed
