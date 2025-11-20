# Python Version Compatibility Issues

## Critical Issue: Python 3.13 + Dask Incompatibility (October 2024)

### Problem Summary
Jobs submitted to Dask would hang indefinitely with tasks submitted but never executing. This was a complete production blocker that manifested as:
- 4000+ tasks submitted to workers
- Workers receiving tasks but unable to execute them
- No error messages initially visible
- Jobs timing out after hours with no progress

### Root Cause Analysis

The issue was a **cascade of Python version incompatibilities**:

1. **Initial State**: System was running Python 3.13 with Dask 2024.8.0
2. **First Symptom**: Even simple lambda functions like `lambda x: x + 1` would not execute on workers
3. **Diagnosis**: Python 3.13 introduced breaking changes that made it incompatible with Dask's distributed execution model

### The Failure Cascade

1. **Dask Compatibility Failure** (Python 3.13 + Dask 2024.8.0/2024.10.0)
   - Workers could receive tasks but couldn't execute ANY Python code
   - This was due to changes in Python 3.13's internals that broke Dask's task serialization/execution

2. **Subprocess Runner Failure** (After downgrade to Python 3.12)
   - Once we fixed the Dask issue, a new problem emerged
   - The subprocess runner that creates isolated environments for simulations failed with:
     ```
     EOFError: Stream closed while reading headers
     ```
   - This was because the simulation bundles still required Python >=3.13

3. **Bundle Dependency Mismatch**
   - The `simulation-workflow` bundle had `requires-python = ">=3.13"` in its pyproject.toml
   - When the Python 3.12 worker tried to install it, pip/uv would fail
   - The subprocess would crash before the JSON-RPC handshake could complete

### Resolution Steps

1. **Downgrade all Docker images to Python 3.12**:
   ```dockerfile
   # Changed in all Dockerfiles
   FROM python:3.13-slim → FROM python:3.12-slim
   ```

2. **Update project Python requirements**:
   ```toml
   # pyproject.toml files
   requires-python = ">=3.13" → requires-python = ">=3.12"
   ```

3. **Force fresh virtual environments** (for debugging):
   ```yaml
   # workspace.yaml
   env:
     - name: MODELOPS_FORCE_FRESH_VENV
       value: "true"
   ```

4. **Update all dependent packages**:
   - modelops
   - modelops-calabaria
   - simulation bundles
   - Any other packages in the ecosystem

### Key Learnings

1. **Version Pinning is Critical**: All components in the distributed system must use compatible Python versions
2. **Subprocess Debugging**: The "Stream closed while reading headers" error means the subprocess died before JSON-RPC initialization - always check stderr for the actual error
3. **Cascade Failures**: What appears as a Dask issue can actually be multiple layers of incompatibility
4. **Testing Strategy**: Always test with the exact Python version used in production

### Prevention Measures

1. **CI/CD Validation**: Ensure all images build with the same Python version
2. **Bundle Validation**: Check that bundle Python requirements match worker Python version
3. **Integration Tests**: Run full end-to-end tests after any Python version change
4. **Version Matrix Testing**: Test Dask compatibility with new Python versions before upgrading

### Debugging Commands

When encountering similar issues:

```bash
# Check Python versions across components
kubectl -n modelops-dask-dev exec deployment/dask-scheduler -- python --version
kubectl -n modelops-dask-dev exec deployment/dask-workers -- python --version

# Check worker logs for subprocess errors
kubectl -n modelops-dask-dev logs deployment/dask-workers --tail=100

# Force fresh venvs for debugging
export MODELOPS_FORCE_FRESH_VENV=true

# Check bundle requirements
cat examples/simulation-workflow/pyproject.toml | grep requires-python
```

### Version Compatibility Matrix

| Python Version | Dask Version | Status | Notes |
|---------------|--------------|--------|-------|
| 3.13.x | 2024.8.0 | ❌ BROKEN | Workers cannot execute any tasks |
| 3.13.x | 2024.10.0 | ❌ BROKEN | Same issue, Dask not ready for 3.13 |
| 3.12.x | 2024.10.0 | ✅ WORKING | Recommended configuration |
| 3.12.x | 2024.8.0 | ✅ WORKING | Also compatible |

### Timeline of Issue

- **Initial State**: System running with Python 3.13, jobs hanging
- **First Attempt**: Updated Dask 2024.8.0 → 2024.10.0 (didn't help)
- **Key Discovery**: Even `lambda x: x + 1` wouldn't execute - fundamental incompatibility
- **Solution**: Downgrade Python 3.13 → 3.12 across all components
- **Secondary Issue**: Bundle pyproject.toml still required Python >=3.13
- **Final Fix**: Update all pyproject.toml files to Python >=3.12

This issue cost approximately 4 hours of debugging time and blocked all production jobs. The subtle nature of the failure (tasks submitted but not executing, with no clear error messages initially) made it particularly challenging to diagnose.