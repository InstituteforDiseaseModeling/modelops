# IsolatedWarmExecEnv Refactor Plan

## Overview

The TODO comment "we need to clean up / refactor run() and run_aggregation()" in `src/modelops/adapters/exec_env/isolated_warm.py:4` refers to significant code duplication and single responsibility violations in the core execution methods.

## Current Issues

### Code Duplication
Both `run()` and `run_aggregation()` methods share nearly identical patterns:
- Bundle resolution (`self.bundle_repo.ensure_local()`)
- Error handling with similar try/catch blocks
- CAS artifact handling (deciding inline vs CAS storage)
- JSON encoding/decoding for subprocess communication

### Single Responsibility Violation
Each method handles multiple concerns:
- **Bundle resolution** (infrastructure)
- **Process execution** (delegation to process manager)
- **CAS decisions** (storage policy)
- **Data serialization** (wire protocol conversion)
- **Error handling** (exception mapping)

## Test Coverage Audit

### ❌ WEAK COVERAGE
**Missing Critical Tests**:
- No direct unit tests for `IsolatedWarmExecEnv.run()` or `run_aggregation()`
- No tests for bundle resolution, CAS decisions, artifact handling, error conversion
- Only integration tests that hit the methods indirectly via `DaskSimulationService`

### ✅ EXISTING COVERAGE
- JSON-RPC protocol thoroughly tested (`test_jsonrpc.py`)
- Large message handling (70KB+) covered (`test_subprocess_runner.py`)
- End-to-end workflows tested via integration tests (`test_simulation_e2e.py`, `test_dask_aggregation.py`)

## Refactor Plan

### Extract Helper Methods:

1. **`_resolve_bundle(bundle_ref) -> Tuple[str, Path]`** - Bundle resolution
2. **`_handle_artifacts(artifacts: Dict[str, bytes]) -> Dict[str, str]`** - CAS vs inline decisions
3. **`_create_sim_return(task, artifacts_or_error) -> SimReturn`** - Result construction
4. **`_resolve_cas_references(sim_returns) -> List[SimReturn]`** - CAS reference resolution
5. **`_serialize_sim_returns(resolved_returns) -> List[Dict]`** - JSON-RPC serialization
6. **`_create_error_return(bundle_ref, entrypoint, params, seed, exception) -> SimReturn`** - Error handling

## Before/After Comparison

### BEFORE (Current - Duplicated Logic)

```python
def run(self, task: SimTask) -> SimReturn:
    """Execute task - 161 lines of mixed concerns."""
    try:
        # 1. BUNDLE RESOLUTION (duplicated in run_aggregation)
        digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)

        # 2. EXECUTE (different subprocess call)
        artifacts = self._process_manager.execute_task(...)

        # 3. ERROR CHECK (duplicated pattern)
        if len(artifacts) == 1 and "error" in artifacts:
            error_data = base64.b64decode(artifacts["error"])
            error_info = json.loads(error_data)
            raise RuntimeError(...)

        # 4. CAS DECISIONS (duplicated logic - 15+ lines)
        artifact_refs = {}
        for name, data in artifacts.items():
            decoded_data = base64.b64decode(data) if isinstance(data, str) else data
            if len(decoded_data) > self.inline_artifact_max_bytes:
                checksum = hashlib.sha256(decoded_data).hexdigest()
                ref = self.cas.put(decoded_data, checksum)
                artifact_refs[name] = f"cas://{ref}"
            else:
                artifact_refs[name] = f"inline:{base64.b64encode(decoded_data).decode()}"

        # 5. CREATE SIM RETURN (repeated structure creation - 25+ lines)
        root = sim_root(...)
        tid = task_id(...)
        outputs = {}  # Convert artifact_refs to TableArtifacts...
        return SimReturn(...)

    except Exception as e:
        # ERROR HANDLING (56 lines of duplicated logic)
        logger.exception(...)
        root = sim_root(...)  # Same as above
        tid = task_id(...)    # Same as above
        error_info = ErrorInfo(...)
        # ... 40+ more lines of error artifact handling
        return SimReturn(...)

def run_aggregation(self, task: AggregationTask) -> AggregationReturn:
    """Execute aggregation - 84 lines with overlapping concerns."""
    try:
        # 1. BUNDLE RESOLUTION (exact duplicate)
        digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)

        # 2. CAS RESOLUTION (unique to aggregation - 15+ lines)
        resolved_returns = []
        for sr in task.sim_returns:
            resolved_outputs = {}
            for name, artifact in sr.outputs.items():
                if artifact.ref and artifact.ref.startswith("cas://"):
                    cas_ref = artifact.ref[6:]
                    data = self.cas.get(cas_ref)
                    resolved_artifact = replace(artifact, inline=data)
                    resolved_outputs[name] = resolved_artifact
                # ...

        # 3. SERIALIZATION (unique pattern - 20+ lines)
        serialized_returns = []  # Convert to dict format...

        # 4. EXECUTE (different subprocess call)
        result = self._process_manager.execute_aggregation(...)

        # 5. ERROR CHECK (similar pattern but different)
        if 'error' in result:
            error_msg = result['error']
            raise RuntimeError(...)

        return AggregationReturn(...)

    except Exception as e:
        logger.error(...)  # Different from run()
        raise  # Re-raises vs creating error return
```

### AFTER (Refactored - Single Responsibility)

```python
def run(self, task: SimTask) -> SimReturn:
    """Execute simulation task - 15 lines orchestrating helpers."""
    try:
        # 1. Resolve bundle (helper)
        digest, bundle_path = self._resolve_bundle(task.bundle_ref)

        # 2. Execute in subprocess
        raw_artifacts = self._process_manager.execute_task(
            bundle_digest=digest,
            bundle_path=bundle_path,
            entrypoint=str(task.entrypoint) if task.entrypoint else "main",
            params=dict(task.params.params),
            seed=task.seed
        )

        # 3. Create return value (helper)
        return self._create_sim_return(task, raw_artifacts)

    except Exception as e:
        return self._create_error_return(
            task.bundle_ref,
            str(task.entrypoint) if task.entrypoint else "main",
            dict(task.params.params),
            task.seed,
            e
        )

def run_aggregation(self, task: AggregationTask) -> AggregationReturn:
    """Execute aggregation task - 18 lines orchestrating helpers."""
    try:
        # 1. Resolve bundle (same helper)
        digest, bundle_path = self._resolve_bundle(task.bundle_ref)

        # 2. Resolve CAS references (helper)
        resolved_returns = self._resolve_cas_references(task.sim_returns)

        # 3. Serialize for subprocess (helper)
        serialized_returns = self._serialize_sim_returns(resolved_returns)

        # 4. Execute aggregation
        result = self._process_manager.execute_aggregation(
            bundle_digest=digest,
            bundle_path=bundle_path,
            target_entrypoint=str(task.target_entrypoint),
            sim_returns=serialized_returns,
            target_data=task.target_data
        )

        # 5. Handle errors and return
        if 'error' in result:
            raise RuntimeError(f"Aggregation failed: {result['error']}")

        return AggregationReturn(
            aggregation_id=task.aggregation_id(),
            loss=result['loss'],
            diagnostics=result.get('diagnostics', {}),
            outputs={},
            n_replicates=result.get('n_replicates', len(task.sim_returns))
        )

    except Exception as e:
        logger.error(f"Aggregation execution failed: {e}")
        raise
```

### NEW HELPER METHODS (private)

```python
def _resolve_bundle(self, bundle_ref: str) -> Tuple[str, Path]:
    """Resolve bundle reference to local path."""
    return self.bundle_repo.ensure_local(bundle_ref)

def _handle_artifacts(self, raw_artifacts: Dict[str, Any]) -> Dict[str, str]:
    """Convert raw artifacts to CAS refs or inline data."""
    artifact_refs = {}
    for name, data in raw_artifacts.items():
        decoded_data = base64.b64decode(data) if isinstance(data, str) else data

        if len(decoded_data) > self.inline_artifact_max_bytes:
            checksum = hashlib.sha256(decoded_data).hexdigest()
            ref = self.cas.put(decoded_data, checksum)
            artifact_refs[name] = f"cas://{ref}"
        else:
            artifact_refs[name] = f"inline:{base64.b64encode(decoded_data).decode()}"

    return artifact_refs

def _create_sim_return(self, task: SimTask, raw_artifacts: Dict[str, Any]) -> SimReturn:
    """Create SimReturn from task and raw subprocess artifacts."""
    # Check for subprocess errors
    if len(raw_artifacts) == 1 and "error" in raw_artifacts:
        error_data = base64.b64decode(raw_artifacts["error"])
        error_info = json.loads(error_data)
        raise RuntimeError(f"Subprocess execution failed: {error_info.get('error')}")

    # Convert to artifact refs
    artifact_refs = self._handle_artifacts(raw_artifacts)

    # Create sim_root and task_id
    root = sim_root(
        bundle_ref=task.bundle_ref,
        params=dict(task.params.params),
        seed=task.seed,
        entrypoint=str(task.entrypoint) if task.entrypoint else "main"
    )

    tid = task_id(
        sim_root=root,
        entrypoint=str(task.entrypoint) if task.entrypoint else "main",
        outputs=tuple(artifact_refs.keys())
    )

    # Convert to TableArtifacts
    outputs = {}
    for name, ref in artifact_refs.items():
        if ref.startswith("cas://"):
            checksum = ref[6:]
            outputs[name] = TableArtifact(ref=ref, checksum=checksum, size=0, inline=None)
        elif ref.startswith("inline:"):
            inline_data = base64.b64decode(ref[7:])
            checksum = hashlib.sha256(inline_data).hexdigest()
            outputs[name] = TableArtifact(
                ref=None, checksum=checksum, size=len(inline_data), inline=inline_data
            )

    return SimReturn(task_id=tid, sim_root=root, outputs=outputs)

def _resolve_cas_references(self, sim_returns: List[SimReturn]) -> List[SimReturn]:
    """Resolve CAS references to inline data for aggregation."""
    resolved_returns = []
    for sr in sim_returns:
        resolved_outputs = {}
        for name, artifact in sr.outputs.items():
            if artifact.ref and artifact.ref.startswith("cas://") and not artifact.inline:
                cas_ref = artifact.ref[6:]
                data = self.cas.get(cas_ref)
                resolved_artifact = replace(artifact, inline=data)
                resolved_outputs[name] = resolved_artifact
            else:
                resolved_outputs[name] = artifact

        resolved_returns.append(replace(sr, outputs=resolved_outputs))

    return resolved_returns

def _serialize_sim_returns(self, resolved_returns: List[SimReturn]) -> List[Dict]:
    """Serialize SimReturns for JSON-RPC transport."""
    serialized_returns = []
    for sr in resolved_returns:
        sr_dict = {
            'task_id': sr.task_id,
            'sim_root': sr.sim_root,
            'outputs': {}
        }

        for name, artifact in sr.outputs.items():
            sr_dict['outputs'][name] = {
                'size': artifact.size,
                'checksum': artifact.checksum,
                'inline': base64.b64encode(artifact.inline).decode('ascii') if artifact.inline else None,
                'cas_ref': artifact.ref
            }

        serialized_returns.append(sr_dict)

    return serialized_returns

def _create_error_return(self, bundle_ref: str, entrypoint: str, params: dict, seed: int, exception: Exception) -> SimReturn:
    """Create error SimReturn from exception."""
    logger.exception(f"Task execution failed for bundle {bundle_ref}")

    # Create error structure
    root = sim_root(bundle_ref=bundle_ref, params=params, seed=seed, entrypoint=entrypoint)
    tid = task_id(sim_root=root, entrypoint=entrypoint, outputs=("error",))

    error_info = ErrorInfo(
        error_type=type(exception).__name__,
        message=str(exception),
        retryable=False
    )

    # Store error details
    error_details_data = json.dumps({
        "error": str(exception),
        "type": type(exception).__name__,
        "bundle_ref": bundle_ref,
        "entrypoint": entrypoint
    }).encode()

    checksum = hashlib.sha256(error_details_data).hexdigest()

    if len(error_details_data) > self.inline_artifact_max_bytes:
        error_ref = self.cas.put(error_details_data, checksum)
        error_details = TableArtifact(
            ref=f"cas://{error_ref}", checksum=checksum,
            size=len(error_details_data), inline=None
        )
    else:
        error_details = TableArtifact(
            ref=None, checksum=checksum,
            size=len(error_details_data), inline=error_details_data
        )

    return SimReturn(
        task_id=tid, sim_root=root, outputs={},
        error=error_info, error_details=error_details
    )
```

## Expected Benefits

### Code Reduction
- `run()`: **161 → 15 lines** (90% reduction)
- `run_aggregation()`: **84 → 18 lines** (78% reduction)

### Eliminated Duplication
- Bundle resolution logic (shared helper)
- CAS artifact handling patterns
- Error structure creation
- JSON encoding/decoding patterns

### Improved Testability
- Each helper method has single responsibility
- Private methods can be unit tested individually
- Error paths isolated and testable
- CAS logic separated from execution logic

### Expected Tests to Pass
All existing integration tests should continue passing since the public interface (`run()` and `run_aggregation()` signatures) remains identical. The refactor would benefit from adding unit tests for the new helper methods.

## Implementation Notes

1. **Preserve Public Interface**: Method signatures remain unchanged
2. **Maintain Error Semantics**: Error handling behavior stays identical
3. **Add Unit Tests**: New helper methods should be unit tested
4. **Incremental Implementation**: Extract one helper at a time, test between changes