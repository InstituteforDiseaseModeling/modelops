# Incident: Missing Dependency Error Masking

**Date:** 2025-11-22
**Status:** Fixed
**Severity:** High (Poor UX, delayed debugging)

## Summary

When a bundle was missing a required dependency (starsim), the error manifested as a cryptic `KeyError: 'incidence'` deep in the target aggregation code, rather than as a clear `ModuleNotFoundError: No module named 'starsim'` at import time.

This made debugging extremely difficult and time-consuming, requiring extensive investigation to trace back from the KeyError to the root cause.

## Root Cause

The starsim-sir example bundle was missing `starsim>=3.0.4` and `sciris>=3.2.4` from its `pyproject.toml` dependencies (accidentally removed in commit 0cf8f6f). When the model tried to `import starsim`, it failed, but this error was caught and masked by calabaria's wire execution error handler.

## Error Masking Chain

1. **Import fails**: `models/sir.py` tries to `import starsim`, raises `ModuleNotFoundError`
2. **Error caught in calabaria**: `modelops_wire.py:226` catches the exception
3. **Wrong error format returned**: Returned `{"table": b"", "metadata": {...}}` instead of `{"error": ...}`
4. **subprocess_runner continues**: Didn't recognize this as an error, continued processing
5. **Empty outputs**: `sim_output` dict is empty (no "incidence", "prevalence", etc.)
6. **Target fails with KeyError**: When target tries `out["incidence"]`, KeyError is raised

**What the user saw:**
```
KeyError: 'incidence'
  File ".../modelops_calabaria/core/target.py", line 37, in evaluate
    replicates_df = [out[self.model_output] for out in replicated_sim_outputs]
                     ~~~^^^^^^^^^^^^^^^^^^^
```

**What they SHOULD have seen:**
```
ImportError: Cannot import models.sir:StarsimSIR: No module named 'starsim'

HINT: Add 'starsim>=3.0.4' to your bundle's pyproject.toml dependencies
```

## The Fix

### 1. Fixed calabaria error format (commit 4d0bfc8)

Changed `modelops_wire.py` to return errors in the format subprocess_runner expects:

```python
# OLD (wrong format):
return {
    "table": b"",
    "metadata": _json_dumps({
        "error": str(e),
        ...
    })
}

# NEW (correct format):
error_info = {
    "error": str(e),
    "type": type(e).__name__,
    "entrypoint": entrypoint,
    "traceback": traceback.format_exc()
}
return {
    "error": base64.b64encode(_json_dumps(error_info)).decode("ascii")
}
```

Now when an import fails, subprocess_runner will recognize it as an error and surface it properly.

### 2. Restored missing dependencies

Added back to `examples/starsim-sir/pyproject.toml`:
```toml
dependencies = [
    ...
    "starsim>=3.0.4",
    "sciris>=3.2.4",
]
```

## Lessons Learned

1. **Exception handlers must preserve error semantics**: Catching an exception is fine, but the error must be propagated in a format the caller understands as an error.

2. **Validate assumptions early**: subprocess_runner should validate that `result_bytes` has expected keys or an "error" key before proceeding.

3. **Better error messages**: When import errors occur, we should provide hints about what dependency is missing and where to add it.

4. **Preflight checks need improvement**: Bundle validation should verify that models can actually be imported before submission.

## Remaining Work

1. **Add validation in subprocess_runner**: Check if result has expected keys or error key before processing
2. **Improve preflight checks**: Try to import models during `bundle register` and catch missing dependencies
3. **Better dependency documentation**: Document that models must declare ALL their dependencies (including simulation frameworks)
4. **Consider sandboxed import test**: During registration, try importing in a clean venv to catch missing deps early

## Timeline

- **2025-11-21 19:54**: Commit 0cf8f6f accidentally removes starsim/sciris dependencies
- **2025-11-22 04:49**: Job fails with `KeyError: 'incidence'`
- **2025-11-22 ~05:00**: Extensive debugging session to trace error
- **2025-11-22 ~06:00**: Root cause identified (missing dependencies)
- **2025-11-22 ~06:15**: Error handling fix implemented (commit 4d0bfc8)
- **2025-11-22 ~06:20**: Dependencies restored, job works

## Related Issues

- Error handling audit needed across all wire protocol entry points
- Preflight validation should be more comprehensive
- Need better error context propagation through the stack
