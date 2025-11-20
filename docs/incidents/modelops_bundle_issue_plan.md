# ModelOps-Bundle Issue Resolution Plan

**Repository:** https://github.com/InstituteforDiseaseModeling/modelops-bundle/issues
**Date:** 2025-01-11
**Total Open Issues:** 4

## Overview

This document outlines a comprehensive plan to address all open issues in modelops-bundle, organized by priority and complexity.

---

## Issue #3: Updated Targets Do Not Modify Registry ⭐ **HIGH PRIORITY**

**Type:** Bug - Data Integrity
**Impact:** Breaks calibration workflows when target code changes
**Complexity:** Medium

### Problem
When developers modify target definitions (e.g., rename functions in `incidence.py`), the `.modelops-bundle/registry.yaml` file is not updated. This leads to:
- Runtime errors when referencing non-existent targets
- Stale entries accumulating in registry
- Developer confusion and workflow disruption

### Root Cause
Registry is a static snapshot created at registration time with no validation or refresh mechanism.

### Proposed Solution

**Phase 1: Validation (Quick Win)**
```python
# Add registry validation on load
def validate_registry(registry: BundleRegistry) -> List[str]:
    """Check registry entries against actual code."""
    errors = []
    for target_id, entry in registry.targets.items():
        # Check if file exists
        if not Path(entry.path).exists():
            errors.append(f"Target {target_id}: file {entry.path} not found")
            continue

        # Check if entrypoint function exists
        module_path, func_name = entry.entrypoint.split(':')
        try:
            module = import_module(module_path)
            if not hasattr(module, func_name):
                errors.append(f"Target {target_id}: function {func_name} not found in {entry.path}")
        except ImportError:
            errors.append(f"Target {target_id}: cannot import {module_path}")

    return errors
```

**Phase 2: Smart Re-registration**
```python
# When registering targets, detect and remove stale entries
def register_target(path: Path, ...):
    # Discover current targets in file
    current_targets = discover_targets(path)

    # Remove stale entries that reference this file
    registry = load_registry()
    stale_ids = [
        tid for tid, entry in registry.targets.items()
        if entry.path == str(path) and tid not in current_targets
    ]

    for stale_id in stale_ids:
        del registry.targets[stale_id]
        console.print(f"[yellow]Removed stale target: {stale_id}[/yellow]")

    # Register new/updated targets
    for target in current_targets:
        register_target_entry(target)
```

**Phase 3: Auto-refresh Command**
```bash
# New CLI command
mops bundle refresh
# Scans all registered files, validates entries, removes stale ones
```

### Implementation Steps
1. Add `validate_registry()` function to `modelops_bundle/registry.py`
2. Call validation during `mops bundle status` and show warnings
3. Modify `register_target()` to detect and remove stale entries
4. Add `mops bundle refresh` command to force full registry rebuild
5. Update tests to verify validation catches stale entries
6. Document the refresh workflow in README

**Estimated Effort:** 1-2 days
**Testing Needs:**
- Rename target function and verify stale entry is removed on re-register
- Delete target file and verify validation catches it
- Test refresh command rebuilds registry correctly

---

## Issue #6: UX Improvement (Installation) ⭐ **HIGH PRIORITY**

**Type:** DevOps/UX - Installation Flow
**Impact:** Affects all users during setup and updates
**Complexity:** Low-Medium

### Problems Identified

1. **Azure CLI version not checked**
   - Users may have outdated `az` versions
   - ✅ **ALREADY FIXED** in modelops `install.sh` (our workspace-update branch)

2. **Duplicate installations** (`~/.local/bin/` vs `~/.local/share/uv/tools/`)
   - Causes version confusion
   - Multiple binaries can conflict

3. **`--force` flag required for reinstallation**
   - Not obvious to users
   - Should be automatic for bug fixes

### Proposed Solution

**Update install.sh:**
```bash
#!/bin/bash

# 1. Check prerequisites
check_az_version() {
    if ! command -v az &> /dev/null; then
        error "Azure CLI not found"
        return 1
    fi

    local version=$(az version --query '"azure-cli"' -o tsv)
    if ! version_ge "$version" "2.31.0"; then
        warning "Azure CLI $version is too old (need >= 2.31.0)"
        echo "Upgrade with: az upgrade"
        return 1
    fi
}

# 2. Clean up old installations
cleanup_old_installations() {
    info "Cleaning up old installations..."

    # Remove old uv tool installations
    if command -v uv &> /dev/null; then
        uv tool uninstall modelops 2>/dev/null || true
        uv tool uninstall modelops-bundle 2>/dev/null || true
        uv tool uninstall calabaria 2>/dev/null || true
    fi

    # Remove orphaned binaries in ~/.local/bin
    rm -f ~/.local/bin/mops
    rm -f ~/.local/bin/modelops-bundle
    rm -f ~/.local/bin/cb
}

# 3. Install with --force by default
install_modelops() {
    local branch="${MODELOPS_BRANCH:-main}"
    local git_ref="@git+https://github.com/institutefordiseasemodeling/modelops.git"

    if [ "$branch" != "main" ]; then
        git_ref="${git_ref}@${branch}"
    fi

    info "Installing ModelOps suite (using --force to ensure clean install)..."
    uv tool install --force --python ">=3.12" "modelops[full]${git_ref}"
}

# Main flow
main() {
    check_az_version || exit 1
    cleanup_old_installations
    install_uv
    install_modelops
    configure_path
    verify_installation
}
```

### Implementation Steps
1. ✅ Add `check_az_version()` to install.sh (already done in workspace-update branch)
2. Add `cleanup_old_installations()` function
3. Make `--force` flag default in installation
4. Add verification step that checks binary locations
5. Document clean reinstall procedure in README
6. Test on fresh VM and with existing installations

**Estimated Effort:** 0.5-1 day (mostly testing)
**Testing Needs:**
- Fresh install on clean system
- Reinstall over existing installation
- Verify no duplicate binaries remain
- Test branch-specific installation

---

## Issue #4: Better Secret Handling ⭐ **MEDIUM PRIORITY**

**Type:** Security/UX - Credential Management
**Impact:** Developer workflow friction, security concerns
**Complexity:** Medium-High

### Problems

1. **Workspace restart required** when PAT environment variables change
2. **Wrong namespace deployment** - secrets go to `default` instead of `modelops-dask-dev`
3. **Friction in development** - hard to work with private repos

### Root Cause Analysis

Need to investigate:
- Where secrets are created in workspace provisioning
- Why namespace isn't being respected
- How direnv env vars are picked up

### Proposed Solutions

**Option 1: Secret Management Command** (Recommended)
```bash
# New CLI commands for secret management
mops secrets set GITHUB_TOKEN <value> --env dev
mops secrets update GITHUB_TOKEN <value> --env dev
mops secrets delete GITHUB_TOKEN --env dev
mops secrets list --env dev

# Directly updates K8s secrets without restart
kubectl create secret generic modelops-secrets \
  --from-literal=GITHUB_TOKEN=$TOKEN \
  -n modelops-dask-dev \
  --dry-run=client -o yaml | kubectl apply -f -
```

**Option 2: Hot-reload Secrets**
```python
# In workspace.py, add Secret resource that reads from ~/.modelops/secrets/
def create_secrets_from_local(namespace: str):
    """Create K8s secrets from ~/.modelops/secrets/ directory."""
    secrets_dir = Path.home() / ".modelops" / "secrets"

    if not secrets_dir.exists():
        return None

    secret_data = {}
    for secret_file in secrets_dir.glob("*"):
        if secret_file.is_file():
            key = secret_file.name.upper()
            value = secret_file.read_text().strip()
            secret_data[key] = value

    if not secret_data:
        return None

    return k8s.core.v1.Secret(
        "modelops-local-secrets",
        metadata=k8s.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            name="modelops-local-secrets",
        ),
        string_data=secret_data,  # Auto-encodes to base64
    )
```

**Option 3: Fix Namespace Bug** (Immediate Fix)
```python
# In workspace.py - ensure secrets use correct namespace
def create_github_secret(namespace: str, ...):
    return k8s.core.v1.Secret(
        "github-credentials",
        metadata=k8s.meta.v1.ObjectMetaArgs(
            namespace=namespace,  # ← CRITICAL: must use passed namespace
            name="github-credentials",
        ),
        # ...
    )
```

### Implementation Steps

1. **Immediate Fix** (Option 3):
   - Audit all Secret resources in workspace.py
   - Verify namespace parameter is used correctly
   - Add validation in tests

2. **CLI Commands** (Option 1):
   - Add `mops secrets` command group
   - Implement set/update/delete/list operations
   - Use kubectl to update secrets directly
   - No workspace restart needed

3. **Documentation**:
   - Document secret management workflow
   - Add troubleshooting guide for common auth issues
   - Security best practices (never commit secrets)

**Estimated Effort:** 2-3 days
**Testing Needs:**
- Verify secrets land in correct namespace
- Test secret updates without workspace restart
- Test with real private GitHub repos
- Security audit of secret handling

---

## Issue #5: Simple Target Alignment ⭐ **LOW PRIORITY**

**Type:** Feature Request - New Alignment Strategy
**Impact:** Enables new use cases (scalar-only calibration)
**Complexity:** Low

### Problem
Current alignment strategies assume key-based matching (e.g., time series). Some applications need direct scalar-to-scalar comparison without keys.

### Proposed Solution

Daniel Klein has already drafted a `MetricAlignment` class. This just needs:

1. **Review and refine** the implementation
2. **Add tests** for scalar metric alignment
3. **Document** the new strategy in Calabaria docs
4. **Example** showing scalar-only calibration

### Implementation Steps

1. Review Daniel's proposed `MetricAlignment` class
2. Add comprehensive tests:
   ```python
   def test_metric_alignment_scalar():
       observed = pd.DataFrame({'metric': ['R0'], 'value': [2.5]})
       simulated = pd.DataFrame({'metric': ['R0'], 'value': [2.3, 2.6, 2.4]})

       aligned = MetricAlignment(metric='R0').align(observed, simulated)
       assert len(aligned) == 3  # One per replicate
   ```
3. Update Calabaria documentation with scalar alignment example
4. Add to strategy registry in `__init__.py`

**Estimated Effort:** 0.5-1 day
**Testing Needs:**
- Scalar metric alignment with multiple replicates
- Error handling for missing metrics
- Integration with existing evaluation strategies

---

## Priority Order

Based on impact and user pain:

### Sprint 1 (High Priority - 3-4 days)
1. **Issue #3**: Registry validation and stale entry removal (1-2 days)
2. **Issue #6**: Install script improvements (0.5-1 day)
3. **Issue #4**: Secret namespace bug fix (0.5 day)

### Sprint 2 (Medium Priority - 2-3 days)
1. **Issue #4**: Secret management CLI commands (2-3 days)

### Sprint 3 (Low Priority - 1 day)
1. **Issue #5**: Metric alignment strategy (0.5-1 day)

---

## Testing Strategy

### Automated Tests
- Unit tests for registry validation
- Integration tests for install script on multiple platforms
- Secret management tests with mocked K8s API
- Metric alignment strategy tests

### Manual Testing
- Fresh install on clean Ubuntu/macOS
- Reinstall over existing installation
- Target rename workflow (issue #3)
- Secret update without restart (issue #4)
- Private repo authentication (issue #4)

### Regression Testing
- Ensure existing workflows still work
- Bundle push/pull with new registry validation
- Calibration runs with updated targets
- Multi-environment secret management

---

## Success Criteria

**Issue #3 (Registry Updates):**
- ✅ Stale targets automatically removed on re-registration
- ✅ `mops bundle status` shows validation warnings
- ✅ `mops bundle refresh` rebuilds registry correctly

**Issue #6 (Installation UX):**
- ✅ `install.sh` checks Azure CLI version
- ✅ No duplicate binaries after reinstall
- ✅ `--force` flag used automatically

**Issue #4 (Secret Handling):**
- ✅ Secrets deploy to correct namespace
- ✅ Can update secrets without workspace restart
- ✅ Private repo authentication works reliably

**Issue #5 (Metric Alignment):**
- ✅ Scalar-only calibration examples work
- ✅ `MetricAlignment` documented and tested

---

## Next Steps

1. Create feature branches for each issue
2. Implement fixes in priority order
3. Submit PRs with comprehensive tests
4. Update documentation
5. Tag releases with fixed issues

## Notes

- Issue #6 Azure CLI check is already implemented in modelops `install.sh` on `feature/workspace-update` branch
- Issue #4 secret namespace bug likely in `src/modelops/infra/components/workspace.py`
- Issue #3 is highest impact - breaks active development workflows
- Consider backporting critical fixes to stable release branches
