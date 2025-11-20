# Registry Staleness Problem

## Problem Statement

The bundle registry (`registry.yaml`) can become stale when code changes locally but hasn't been pushed yet. This creates a tension between:
- **Digest integrity**: Registry should only update on `push` to maintain digest-content coupling
- **Development workflow**: Users edit code and expect `status` to reflect current state

## Current Behavior

Registry updates ONLY on `push` to prevent "Stale + Synced" contradictions. This means:
- ✅ Prevents digest mismatches (the bug we fixed earlier)
- ❌ Creates local staleness during development
- ❌ Users see confusing errors for code that's already fixed

## Example Scenario

```bash
# Current state in starsim-sir:
$ cat .modelops-bundle/registry.yaml
targets:
  incidence_target:
    entrypoint: targets.incidence:incidence_target  # Function doesn't exist!
    model_output: BAD_OUTPUT_NAME                    # Wrong output name!

# Actual code in targets/incidence.py:
def incidence_replicate_mean_target(data_paths):  # Real function name
    return Target(model_output="incidence", ...)    # Real output name

# Preflight catches the mismatch:
$ mops-bundle status
Preflight Errors:
  ✗ Target expects model output 'BAD_OUTPUT_NAME' but no model provides it
  ✗ Function 'incidence_target' not found in incidence.py
```

The registry is showing OLD values because it hasn't been updated since the code was fixed.

## Proposed Solutions

### Option 1: Add `mops-bundle sync` Command (RECOMMENDED)

Add explicit command to update registry from current code WITHOUT pushing:

```bash
# Workflow becomes:
1. Edit code to fix bugs
2. mops-bundle sync        # Update registry.yaml from code
3. mops-bundle status       # Verify no errors
4. mops-bundle push         # Publish to cloud
```

**Implementation:**
- Add `sync` command that calls `register_model()` / `register_target()` for all entries
- Updates digests to match current code
- Does NOT push to cloud

**Pros:**
- Explicit user control over when registry updates
- Maintains digest integrity (digests update with registry)
- Simple mental model: `sync` = update local, `push` = publish cloud
- No surprising behavior

**Cons:**
- Adds another command to learn
- Extra manual step in workflow

### Option 2: Auto-sync on Status (with Warning)

Make `mops-bundle status` detect staleness and offer to sync:

```bash
$ mops-bundle status

Registry Staleness Detected:
  ⚠ Target 'incidence_target' entrypoint may have changed in code
  ⚠ Target 'incidence_target' model_output may have changed in code

Run 'mops-bundle sync' to update registry, or '--no-sync-check' to skip
```

**Pros:**
- Users see staleness immediately
- No silent failures
- Guides users to fix

**Cons:**
- More complexity in status command
- Might be noisy during active development
- Need to avoid false positives

### Option 3: Preflight Check for Staleness (LIGHTWEIGHT)

Add preflight validation that warns about known staleness patterns:

```python
def _check_registry_staleness(self) -> List[ValidationIssue]:
    """Check if registry entries match current code."""
    issues = []

    for target_id, target in self.registry.targets.items():
        # Use AST to check if function exists
        file_path = self._module_to_file(module_path)
        if not file_path:
            continue

        tree, _ = self._parse_file_ast(file_path)
        if not tree:
            continue

        # Check if registry function name exists in code
        if not self._symbol_in_ast(tree, function_name, "function"):
            issues.append(ValidationIssue(
                severity=CheckSeverity.WARNING,
                category="stale_registry",
                entity_type="target",
                entity_id=target_id,
                message=f"Registry references '{function_name}' but code may have changed",
                suggestion="Run 'mops-bundle sync' to update registry from code"
            ))
```

**Pros:**
- Minimal new surface area
- Integrates with existing preflight system
- Shows up in status automatically
- Can be implemented quickly

**Cons:**
- Only catches some types of staleness (entrypoint name changes)
- Doesn't auto-fix
- Still requires `sync` command to resolve

### Option 4: Separate Working/Published Registries

Maintain two registry files:
- `.modelops-bundle/registry.yaml` - working (updates on code change)
- `.modelops-bundle/published.yaml` - last pushed (updates on push)

**Pros:**
- Clear separation of local vs remote state
- Can diff to see what changed before pushing
- No staleness possible

**Cons:**
- Two sources of truth
- More complex mental model
- Unclear which registry is "canonical"

## Recommended Approach

**Combination of Option 1 + Option 3:**

1. **Add `mops-bundle sync` command** that re-registers all models/targets from code
   - Updates registry.yaml with current entrypoints, outputs, data files
   - Recomputes all digests
   - Does NOT push to cloud

2. **Add preflight staleness check** that detects common patterns:
   - Entrypoint function/class doesn't exist in code
   - Model output referenced by target doesn't exist in model

3. **Show clear guidance in status**:
   ```
   Preflight Warnings:
     ⚠ Registry may be stale - entrypoint 'incidence_target' not found
         Run 'mops-bundle sync' to update registry from current code
   ```

This approach provides:
- **Explicit control**: Users decide when to sync
- **Automatic detection**: Preflight catches common staleness
- **Clear workflow**: edit → sync → status → push
- **No surprising behavior**: Registry only changes when user runs sync/push

## Implementation Tasks

### Phase 1: Basic Sync Command
- [ ] Add `mops-bundle sync` CLI command
- [ ] Implement sync logic that re-scans all models/targets
- [ ] Update digests during sync
- [ ] Add tests for sync behavior

### Phase 2: Staleness Detection
- [ ] Add `_check_registry_staleness()` to preflight validator
- [ ] Check for missing entrypoint symbols in code
- [ ] Check for orphaned model outputs
- [ ] Show warnings in status output

### Phase 3: Enhanced Detection
- [ ] Detect when data files changed but digest unchanged
- [ ] Detect when code dependencies changed
- [ ] Add `--check-staleness` flag for deep validation

## Open Questions

1. **Should sync be automatic?**
   - Pro: Less user friction
   - Con: Surprising behavior, breaks digest expectations
   - **Decision needed**: Probably manual is safer

2. **Should sync warn before overwriting?**
   - Pro: Prevents accidental overwrites
   - Con: Extra confirmation step
   - **Decision needed**: Probably show diff and require --force for destructive changes

3. **Should push implicitly sync first?**
   - Pro: Ensures cloud gets latest code
   - Con: Hides staleness issues
   - **Decision needed**: Probably not - make staleness explicit

## Related Issues

- Registry digest staleness bug (FIXED): Push now updates registry digests
- AST-based entrypoint validation (FIXED): Can detect missing functions without imports
- This issue: Registry can become stale between edits and push

## Examples of Staleness

### Example 1: Renamed Function
```python
# Code change:
- def incidence_target(data):
+ def incidence_replicate_mean_target(data):

# Registry still says:
entrypoint: targets.incidence:incidence_target

# Result: Job submission fails with import error
```

### Example 2: Changed Model Output
```python
# Code change:
- outputs=["BAD_OUTPUT_NAME"]
+ outputs=["incidence"]

# Registry still says:
model_output: BAD_OUTPUT_NAME

# Result: Preflight error about missing output
```

### Example 3: Updated Data File
```python
# Code change: edited data/observed.csv

# Registry still says:
target_digest: sha256:old_digest

# Result: Cloud has wrong data, jobs use stale observations
```

## Status

**Current State**: Problem identified, solutions proposed
**Next Steps**: Implement Option 1 + Option 3 after more pressing bugs resolved
**Priority**: Medium (workaround exists: manually edit registry.yaml or push immediately)
