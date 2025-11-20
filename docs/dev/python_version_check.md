# Pre-flight Python Version Check Plan

## Problem
- Bundle requires Python >=X (e.g., >=3.13) from pyproject.toml
- Workers have Python 3.12 (from Docker image)
- Jobs fail after K8s submission when workers can't install bundle
- Wastes time and resources discovering incompatibility late

## Solution: Add pre-flight check before job submission

## Implementation Options

### Option 1: Check at Bundle Push Time (Earliest detection)
In `modelops_bundle/api.py` when pushing:
1. Parse pyproject.toml for `requires-python`
2. Compare with known worker Python version (3.12)
3. Warn/error if incompatible

**Pros:** Catches issue earliest
**Cons:** Worker version might change between push and run

### Option 2: Check at Job Submit Time (Most accurate)
In `modelops/cli/jobs.py`:
1. When `--auto` is used, read local pyproject.toml
2. Extract `requires-python` field
3. Compare with worker Python version
4. Fail fast with clear error message

**Pros:** Catches issue right before submission
**Cons:** Only works with --auto flag

### Option 3: Add Worker Python Version to Config
1. Store worker Python version in config/environment
2. Check against it during job submission
3. Could query actual workers for their version

**Pros:** Most accurate, works for all submission types
**Cons:** Requires config updates when worker images change

## Recommended Implementation

```python
# In jobs.py submit() command, after loading bundle:

def check_python_compatibility(bundle_path: Path = Path(".")) -> None:
    """Check if bundle Python requirement matches worker runtime."""
    pyproject = bundle_path / "pyproject.toml"
    if pyproject.exists():
        import tomli
        with open(pyproject, "rb") as f:
            data = tomli.load(f)
            requires = data.get("project", {}).get("requires-python")
            if requires:
                # Parse requirement (e.g., ">=3.13")
                from packaging.specifiers import SpecifierSet
                spec = SpecifierSet(requires)
                worker_version = "3.12"  # Or get from config/env
                if worker_version not in spec:
                    raise ValueError(
                        f"Bundle requires Python {requires} but workers have {worker_version}. "
                        f"Update pyproject.toml to requires-python = '>=3.11'"
                    )

# Call before submit:
if auto:
    check_python_compatibility()
```

## Alternative: Query Worker Version Dynamically

```python
def get_worker_python_version():
    """Get Python version from running workers."""
    # Option 1: From ConfigMap/Secret
    # Option 2: Query a worker pod directly
    result = subprocess.run([
        "kubectl", "get", "pod", "-n", "modelops-dask-dev",
        "-l", "dask.org/component=worker", "-o",
        "jsonpath={.items[0].spec.containers[0].image}"
    ], capture_output=True, text=True)

    # Parse image tag for Python version
    # e.g., "ghcr.io/vsbuffalo/modelops-dask-worker:py312-latest"
    # Or exec into pod: kubectl exec ... -- python --version
    return "3.12"
```

## Benefits
- Catches incompatibility before wasting K8s resources
- Provides clear, actionable error message
- Saves debugging time
- Prevents mysterious failures in production

## Implementation Priority
1. **Immediate**: Add simple hardcoded check for Python 3.12
2. **Near-term**: Make worker version configurable
3. **Long-term**: Auto-detect from running infrastructure