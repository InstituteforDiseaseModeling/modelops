# Multiple Workspace Support Plan

## Analysis: Numeric Suffix and Multi-Workspace Support

### Where the suffix `16505041` came from:
The numeric suffix on `modelops-mvp-aks16505041` was **automatically added by Pulumi** due to its auto-naming feature. This happened because:

1. **Pulumi auto-naming by default**: When you create an Azure ManagedCluster resource in Pulumi without explicitly setting `resource_name`, Pulumi auto-generates a unique name by appending a random suffix
2. **Your config specified**: `name: modelops-mvp-aks` 
3. **Pulumi created**: `modelops-mvp-aks16505041` to ensure uniqueness

### Should we be doing auto-naming?

**NO** - for our use case, we should use **explicit, deterministic naming** because:

1. **Predictability**: We need to know exact resource names for cross-stack references
2. **Multi-environment support**: Our centralized naming already handles uniqueness via environment suffixes
3. **State management**: Deterministic names make it easier to query and manage resources

### Multi-Workspace Support Analysis

Currently, **multiple Dask workspaces are NOT properly supported** in the same environment. Issues:

1. **Single namespace hardcoded**: The workspace always creates namespace `modelops-dask-{env}`
2. **No workspace identifier**: No way to differentiate multiple workspaces
3. **Stack naming collision**: Can't create multiple workspace stacks for same env

## Proposed Solution

### 1. Fix AKS Naming (Disable Auto-naming)
```python
# In azure.py, explicitly set resource_name to prevent auto-suffix:
aks_resource = azure.containerservice.ManagedCluster(
    f"{name}-aks",  # Pulumi logical name
    resource_name=cluster_name,  # EXPLICIT Azure resource name
    resource_group_name=rg.name,
    # ... rest of config
)
```

### 2. Add Multi-Workspace Support
Update StackNaming to support workspace identifiers:
```python
@staticmethod
def get_workspace_stack_name(env: str, workspace_id: str = "default") -> str:
    """Get workspace stack name with optional workspace ID."""
    if workspace_id == "default":
        return f"modelops-workspace-{env}"
    return f"modelops-workspace-{env}-{workspace_id}"

@staticmethod
def get_workspace_namespace(env: str, workspace_id: str = "default") -> str:
    """Get Kubernetes namespace for workspace."""
    if workspace_id == "default":
        return f"modelops-dask-{env}"
    return f"modelops-dask-{env}-{workspace_id}"
```

### 3. Update Workspace CLI
Add `--workspace-id` parameter:
```python
@app.command()
def up(
    workspace_id: str = typer.Option("default", "--workspace-id", "-w"),
    # ... other params
):
    """Deploy Dask workspace with optional workspace ID for multiple workspaces."""
```

### 4. Fix Current State Issues
For your existing cluster:
1. **Option A**: Import existing resources (complex, preserves cluster)
2. **Option B**: Delete and recreate with proper naming (clean, recommended)

### Files to Modify:
1. `src/modelops/infra/components/azure.py` - Add explicit resource_name
2. `src/modelops/core/naming.py` - Add workspace ID support
3. `src/modelops/cli/workspace.py` - Add workspace-id parameter
4. `src/modelops/infra/components/workspace.py` - Use workspace ID in namespace

This will enable:
- Predictable resource names (no random suffixes)
- Multiple Dask workspaces per environment
- Clear separation between workspaces
- Consistent naming across all resources

## Implementation Priority

1. **Immediate**: Fix AKS naming to use explicit resource_name
2. **Next**: Add workspace ID support to enable multiple workspaces
3. **Future**: Consider workspace templates for different configurations (CPU-only, GPU-enabled, etc.)