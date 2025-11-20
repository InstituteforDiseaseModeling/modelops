# Workspace Configuration Design Issue

## Problem Statement

There's a confusing configuration flow with **three different sources of truth** for workspace configuration:

1. **Unified config**: `~/.modelops/modelops.yaml` (used by default)
2. **Standalone workspace YAML**: `examples/workspace.yaml` (used with `--config` flag)
3. **No CLI overrides**: Can't quickly test configuration changes without editing files

This creates several issues:
- Users expect `examples/workspace.yaml` to be active, but `~/.modelops/modelops.yaml` overrides it
- No way to quickly test `MODELOPS_EXECUTOR_TYPE=direct` without editing YAML
- Can't easily toggle threading or warm processes from CLI for debugging

## Current Behavior

### Configuration Priority

```
mops workspace up
  ↓
Checks for --config flag
  ↓
NO: Uses ~/.modelops/modelops.yaml (unified config)
  │   └─ workspace.worker_processes: 3
  │   └─ workspace.worker_threads: 1
  │   └─ NO env vars for MODELOPS_EXECUTOR_TYPE
  │
YES: Uses specified YAML file
      └─ spec.workers.processes: 4
      └─ spec.workers.threads: 1
      └─ spec.workers.env: (can set MODELOPS_EXECUTOR_TYPE)
```

### Current Deployed State

**Actual deployment shows**:
```bash
kubectl get deployment dask-workers -n modelops-dask-dev -o yaml
# Shows: --nworkers "3", --nthreads "1"
```

**Unified config has**:
```yaml
# ~/.modelops/modelops.yaml
workspace:
  worker_processes: 3  # ← This is what's deployed!
  worker_threads: 1
```

**Examples workspace.yaml has**:
```yaml
# examples/workspace.yaml
spec:
  workers:
    processes: 4  # ← User expects this, but it's not active
    threads: 1
    env:
      - name: DASK_WORKER__MEMORY__TARGET
        value: "0.90"
```

## Why This Is Bad Design

### Issue 1: Two Sources of Truth
- Unified config (`~/.modelops/modelops.yaml`) is used by default
- Example config (`examples/workspace.yaml`) looks authoritative but isn't used
- No indication which config is active

### Issue 2: Environment Variables Not Supported in Unified Config
- Unified config has no field for `workspace.worker_env`
- Can't set `MODELOPS_EXECUTOR_TYPE` without using `--config`
- Forces users to maintain separate YAML files for debugging

### Issue 3: No CLI Overrides for Quick Testing
```bash
# Would be nice to have:
mops workspace up --executor-type direct --processes 1 --threads 1

# Instead must do:
vim ~/.modelops/modelops.yaml  # or examples/workspace.yaml
# Edit, save, exit
mops workspace up --config examples/workspace.yaml
```

### Issue 4: Unified Config Missing Key Fields

From `unified_config.py:72-90`, WorkspaceSpec has:
```python
class WorkspaceSpec(BaseModel):
    scheduler_image: str
    scheduler_memory: str
    scheduler_cpu: str
    worker_image: str
    worker_replicas: int
    worker_processes: int  # ✅ Has this
    worker_threads: int    # ✅ Has this
    worker_memory: str
    worker_cpu: str
    autoscaling_enabled: bool
    autoscaling_min_workers: int
    autoscaling_max_workers: int
    autoscaling_target_cpu: int
    # ❌ MISSING: worker_env for environment variables!
```

**No way to set MODELOPS_EXECUTOR_TYPE in unified config!**

## Solutions

### Option 1: Add CLI Overrides (RECOMMENDED)

Add CLI flags to `mops workspace up` for common debugging scenarios:

```python
@app.command()
def up(
    config: Path | None = typer.Option(...),
    env: str | None = env_option(),
    # NEW: Worker configuration overrides
    processes: int | None = typer.Option(
        None, "--processes", help="Override worker processes per pod"
    ),
    threads: int | None = typer.Option(
        None, "--threads", help="Override threads per worker"
    ),
    executor_type: str | None = typer.Option(
        None, "--executor-type", help="Execution environment: 'isolated_warm' or 'direct'"
    ),
    force_fresh_venv: bool = typer.Option(
        False, "--force-fresh-venv", help="Force fresh venv on each execution"
    ),
    # Shorthand for common debugging scenarios
    debug_mode: bool = typer.Option(
        False, "--debug-mode", help="Enable maximum isolation (processes=1, threads=1, executor=direct)"
    ),
):
    """Deploy Dask workspace with optional configuration overrides."""

    # Load base config (from unified or specified file)
    validated_config = load_config(config)

    # Apply CLI overrides
    if debug_mode:
        processes = 1
        threads = 1
        executor_type = "direct"
        info("Debug mode enabled: single-threaded, no warm processes")

    if processes is not None:
        validated_config.spec["workers"]["processes"] = processes
        info(f"Overriding worker processes: {processes}")

    if threads is not None:
        validated_config.spec["workers"]["threads"] = threads
        info(f"Overriding worker threads: {threads}")

    if executor_type is not None:
        # Inject into env vars
        worker_env = validated_config.spec["workers"].get("env", [])
        # Remove existing MODELOPS_EXECUTOR_TYPE
        worker_env = [e for e in worker_env if e.get("name") != "MODELOPS_EXECUTOR_TYPE"]
        # Add new value
        worker_env.append({"name": "MODELOPS_EXECUTOR_TYPE", "value": executor_type})
        validated_config.spec["workers"]["env"] = worker_env
        info(f"Setting executor type: {executor_type}")

    # Continue with deployment...
```

**Pros**:
- Quick testing without editing files
- Clear what's being overridden
- Easy to test: `mops workspace up --debug-mode`

**Cons**:
- More CLI surface area
- Overrides not persisted (need to edit YAML for permanent changes)

### Option 2: Extend Unified Config Schema

Add `worker_env` field to `WorkspaceSpec`:

```python
class WorkspaceSpec(BaseModel):
    # ... existing fields ...
    worker_env: list[dict[str, str]] = Field(
        default_factory=list,
        description="Environment variables for worker pods"
    )
```

Update CLI to merge env vars from unified config:

```python
# In workspace.py up() command
"workers": {
    "processes": unified_config.workspace.worker_processes,
    "threads": unified_config.workspace.worker_threads,
    "env": [
        {"name": k, "value": v}
        for k, v in unified_config.workspace.worker_env.items()
    ] if unified_config.workspace.worker_env else [],
}
```

**Pros**:
- All config in one place
- Persisted across deployments
- No CLI complexity

**Cons**:
- Still need to edit YAML for quick testing
- Breaks existing unified configs (migration needed)

### Option 3: Environment Variable Overrides

Check environment variables before deployment:

```python
# In workspace up() command
import os

# Check for override env vars
if os.getenv("MODELOPS_OVERRIDE_EXECUTOR_TYPE"):
    executor_type = os.getenv("MODELOPS_OVERRIDE_EXECUTOR_TYPE")
    worker_env.append({"name": "MODELOPS_EXECUTOR_TYPE", "value": executor_type})
    info(f"Override from environment: EXECUTOR_TYPE={executor_type}")

if os.getenv("MODELOPS_OVERRIDE_PROCESSES"):
    processes = int(os.getenv("MODELOPS_OVERRIDE_PROCESSES"))
    validated_config.spec["workers"]["processes"] = processes
    info(f"Override from environment: PROCESSES={processes}")
```

**Usage**:
```bash
MODELOPS_OVERRIDE_EXECUTOR_TYPE=direct \
MODELOPS_OVERRIDE_PROCESSES=1 \
MODELOPS_OVERRIDE_THREADS=1 \
mops workspace up
```

**Pros**:
- No CLI changes needed
- Easy to script/automate
- Works with existing commands

**Cons**:
- Less discoverable than CLI flags
- Environment variable pollution
- Naming conflicts with worker runtime env vars

### Option 4: Config Profiles (Future)

Support named configuration profiles:

```yaml
# ~/.modelops/modelops.yaml
workspace:
  profiles:
    production:
      worker_processes: 4
      worker_threads: 1
      worker_env:
        MODELOPS_EXECUTOR_TYPE: isolated_warm

    debug:
      worker_processes: 1
      worker_threads: 1
      worker_env:
        MODELOPS_EXECUTOR_TYPE: direct
        MODELOPS_FORCE_FRESH_VENV: "true"
```

**Usage**:
```bash
mops workspace up --profile debug
mops workspace up --profile production
```

**Pros**:
- Named configurations for common scenarios
- Persisted and reusable
- Self-documenting

**Cons**:
- Most complex to implement
- Requires config migration
- Overkill for simple overrides

## Recommended Implementation

**Phase 1: Immediate (CLI Overrides)**

Add these flags to `mops workspace up`:
```bash
--processes <int>          # Override worker processes
--threads <int>            # Override worker threads
--executor-type <string>   # Set MODELOPS_EXECUTOR_TYPE
--debug-mode               # Shorthand for maximum isolation
```

**Phase 2: Near-term (Extend Unified Config)**

Add `worker_env` to `WorkspaceSpec`:
```python
worker_env: dict[str, str] = Field(
    default_factory=dict,
    description="Environment variables for worker pods"
)
```

**Phase 3: Future (Config Profiles)**

Support named profiles for common scenarios (debug, production, etc.)

## Current Workaround

**To test with executor_type=direct RIGHT NOW**:

### Method 1: Edit Unified Config
```bash
# Edit unified config
vim ~/.modelops/modelops.yaml

# Add env section (NOTE: This requires extending UnifiedConfig schema first!)
# This won't work yet - see Option 2 above

# Deploy
mops workspace up
```

### Method 2: Use Standalone Config
```bash
# Edit standalone config
vim examples/workspace.yaml

# Add env var:
spec:
  workers:
    processes: 1
    threads: 1
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "direct"

# Deploy with explicit config
mops workspace up --config examples/workspace.yaml
```

### Method 3: Update Unified Config and Use It
```bash
# Update unified config values
vim ~/.modelops/modelops.yaml

# Change:
workspace:
  worker_processes: 1  # Was 3
  worker_threads: 1    # Already 1

# Deploy (will use unified config)
mops workspace up
```

**BUT**: Can't set `MODELOPS_EXECUTOR_TYPE` this way because unified config doesn't support env vars!

## Immediate Action Required

**To test executor_type=direct, you MUST use Method 2** (standalone config with `--config` flag):

```bash
cd /Users/vsb/projects/work/modelops

# Edit examples/workspace.yaml
cat >> examples/workspace.yaml << 'EOF'

# Add this to workers section:
    env:
      - name: MODELOPS_EXECUTOR_TYPE
        value: "direct"
      - name: DASK_WORKER__MEMORY__TARGET
        value: "0.90"
      - name: DASK_WORKER__MEMORY__SPILL
        value: "0.95"
      - name: DASK_WORKER__MEMORY__PAUSE
        value: "0.98"
EOF

# Deploy
uv run mops workspace down
uv run mops workspace up --config examples/workspace.yaml
```

## Implementation Tasks

### Phase 1: CLI Overrides (Priority: HIGH)
- [ ] Add `--processes`, `--threads`, `--executor-type` flags to `workspace up`
- [ ] Add `--debug-mode` shorthand flag
- [ ] Implement override logic in workspace.py
- [ ] Add validation for override values
- [ ] Update CLI help text and examples

### Phase 2: Extend Unified Config (Priority: MEDIUM)
- [ ] Add `worker_env: dict[str, str]` to WorkspaceSpec
- [ ] Update workspace.py to merge env vars from unified config
- [ ] Create migration script for existing configs
- [ ] Update `mops init` to include common env vars

### Phase 3: Improved Config Flow (Priority: MEDIUM)
- [ ] Show which config is active: `mops workspace status --show-config`
- [ ] Warn when examples/workspace.yaml exists but unified config is used
- [ ] Add `mops workspace config validate` command
- [ ] Add `mops workspace config show` to display active config

### Phase 4: Documentation (Priority: HIGH)
- [ ] Document config priority and precedence
- [ ] Add examples for common debugging scenarios
- [ ] Update CLAUDE.md with config flow diagrams
- [ ] Create troubleshooting guide for config issues

## Related Files

- **Unified config model**: `src/modelops/core/unified_config.py`
- **Workspace CLI**: `src/modelops/cli/workspace.py`
- **Workspace service**: `src/modelops/client/workspace.py`
- **Pulumi component**: `src/modelops/infra/components/workspace.py`
- **Worker config**: `src/modelops/worker/config.py`
- **Unified config file**: `~/.modelops/modelops.yaml`
- **Example config**: `examples/workspace.yaml`

## Summary

**Current State**:
- ✅ Threading is disabled (`threads: 1`)
- ❌ Config from `~/.modelops/modelops.yaml` has `processes: 3` (not 4 from examples)
- ❌ No way to set `MODELOPS_EXECUTOR_TYPE` in unified config
- ❌ No CLI overrides for quick testing

**Immediate Fix** (to test executor_type=direct):
- Edit `examples/workspace.yaml` to add env vars
- Deploy with `--config examples/workspace.yaml`

**Long-term Fix**:
- Add CLI overrides (`--executor-type`, `--debug-mode`)
- Extend unified config with `worker_env` field
- Add config status/validation commands
