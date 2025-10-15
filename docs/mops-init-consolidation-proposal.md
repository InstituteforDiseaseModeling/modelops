# Consolidation Proposal: `mops init` Command

## Executive Summary

Consolidate `mops config init` and `mops infra init` into a single `mops init` command that creates a unified configuration file, improving user experience and reducing setup complexity.

## Current State

ModelOps currently has two separate initialization commands:

### 1. `mops config init`
- **Location**: `src/modelops/cli/config.py`
- **Creates**: `~/.modelops/config.yaml`
- **Contains**:
  - Pulumi organization (default: `institutefordiseasemodeling`)
  - Default environment (`dev`)
  - Default provider (`azure`)
  - Username (from system user)
- **Interactive**: Optional (default: non-interactive)
- **Dependencies**: None

### 2. `mops infra init`
- **Location**: `src/modelops/cli/infra.py`
- **Creates**: `~/.modelops/infrastructure.yaml`
- **Contains**:
  - Azure subscription ID
  - Full cluster configuration (AKS specs)
  - Storage, registry, workspace specifications
  - Complete unified infrastructure spec
- **Interactive**: Default (with `--non-interactive` option)
- **Dependencies**: Requires Azure CLI, uses helper functions:
  - `get_azure_subscriptions()`
  - `get_aks_versions()`
  - `verify_subscription()`

## Problems with Current Approach

1. **Confusing UX**: New users must remember to run both commands
2. **Unclear ordering**: Which init should be run first?
3. **Redundant steps**: Both are required for initial setup
4. **Multiple files**: Creates two separate config files in `~/.modelops/`
5. **Inconsistent defaults**: One is non-interactive by default, the other is interactive

## Proposed Solution: Unified `mops init`

### Design Principles

1. **Single command**: `mops init` handles all initialization
2. **Single config file**: Merge into `~/.modelops/modelops.yaml`
3. **Smart defaults**: Non-interactive by default with sensible values
4. **Progressive disclosure**: Basic users get defaults, advanced users can customize
5. **Clean code**: No backward compatibility needed (unreleased software)

### Implementation Architecture

#### File Structure
```
src/modelops/
├── cli/
│   ├── main.py          # Add top-level 'init' command
│   ├── init.py          # NEW: Unified init implementation
│   ├── config.py        # Hide init subcommand
│   └── infra.py         # Hide init subcommand
├── core/
│   ├── config.py        # Update to use unified model
│   └── unified_config.py # NEW: Unified configuration models
```

### Unified Configuration Model

```python
# src/modelops/core/unified_config.py
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime

class PulumiSettings(BaseModel):
    """Pulumi-specific settings."""
    backend_url: Optional[str] = None
    organization: str = "institutefordiseasemodeling"

class GeneralSettings(BaseModel):
    """General ModelOps settings."""
    environment: str = "dev"
    provider: str = "azure"
    username: str  # Required, set from system user

class NodePoolSpec(BaseModel):
    """Kubernetes node pool specification."""
    name: str
    mode: str  # System or User
    vm_size: str
    count: Optional[int] = None  # For fixed size
    min: Optional[int] = None     # For autoscaling
    max: Optional[int] = None     # For autoscaling

class AKSSpec(BaseModel):
    """Azure Kubernetes Service specification."""
    name: str = "modelops-cluster"
    kubernetes_version: str
    node_pools: List[NodePoolSpec]

class ClusterSpec(BaseModel):
    """Cluster infrastructure specification."""
    provider: str = "azure"
    subscription_id: str
    resource_group: str
    location: str = "eastus2"
    aks: AKSSpec

class StorageSpec(BaseModel):
    """Storage specification."""
    account_tier: str = "Standard"

class RegistrySpec(BaseModel):
    """Container registry specification."""
    sku: str = "Basic"

class WorkspaceSpec(BaseModel):
    """Dask workspace specification."""
    scheduler_image: str = "ghcr.io/vsbuffalo/modelops-dask-scheduler:latest"
    scheduler_replicas: int = 1
    worker_image: str = "ghcr.io/vsbuffalo/modelops-dask-worker:latest"
    worker_replicas: int = 2
    worker_processes: int = 4
    worker_threads: int = 1

class UnifiedModelOpsConfig(BaseModel):
    """Unified ModelOps configuration."""
    schema_version: int = 2
    generated: datetime = Field(default_factory=datetime.now)

    # Settings (from config.yaml)
    settings: GeneralSettings
    pulumi: PulumiSettings

    # Infrastructure (from infrastructure.yaml)
    cluster: ClusterSpec
    storage: StorageSpec = Field(default_factory=StorageSpec)
    registry: RegistrySpec = Field(default_factory=RegistrySpec)
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
```

### New User Experience

#### Non-Interactive (Default)
```bash
$ mops init
✓ Checking prerequisites...
  • Azure CLI: found (2.51.0)
  • Logged in as: user@example.com

✓ Detected Azure subscription:
  • Development (024ed93f-313a-458a-840b-2022dd854d40)

✓ Using defaults:
  • Location: eastus2
  • Kubernetes: 1.30
  • Username: vsb

✓ Configuration saved to ~/.modelops/modelops.yaml

Ready to deploy! Next steps:
  mops infra up       # Create cloud resources
  mops job submit     # Run your first experiment
```

#### Interactive Mode
```bash
$ mops init --interactive
Welcome to ModelOps! Let's set up your environment.

Checking prerequisites...
✓ Azure CLI found

Setting up configuration...
Organization [institutefordiseasemodeling]:
Default environment [dev]:
Username [vsb]:

Configuring Azure infrastructure...
? Select Azure subscription:
  > Development (024ed93f-313a-458a-840b-2022dd854d40)
    Production (...)

? Azure region [eastus2]:
? Kubernetes version [1.30]:
? Worker VM size [Standard_B4ms]:
? Max workers [3]:

✓ Configuration saved to ~/.modelops/modelops.yaml

Ready to deploy! Next steps:
  mops infra up       # Create cloud resources
  mops job submit     # Run your first experiment
```

### Unified Config File Format

`~/.modelops/modelops.yaml`:

```yaml
# ModelOps Unified Configuration
# Generated: 2025-10-14T10:30:00
schema_version: 2

# General settings
settings:
  environment: dev
  provider: azure
  username: vsb

# Pulumi configuration
pulumi:
  backend_url: null  # Uses default file backend
  organization: institutefordiseasemodeling

# Infrastructure specification
cluster:
  provider: azure
  subscription_id: "024ed93f-313a-458a-840b-2022dd854d40"
  resource_group: modelops-vsb
  location: eastus2
  aks:
    name: modelops-cluster
    kubernetes_version: "1.30"
    node_pools:
      - name: system
        mode: System
        vm_size: Standard_B2s
        count: 1
      - name: workers
        mode: User
        vm_size: Standard_B4ms
        min: 1
        max: 3

storage:
  account_tier: Standard

registry:
  sku: Basic

workspace:
  scheduler_image: ghcr.io/vsbuffalo/modelops-dask-scheduler:latest
  scheduler_replicas: 1
  worker_image: ghcr.io/vsbuffalo/modelops-dask-worker:latest
  worker_replicas: 2
  worker_processes: 4
  worker_threads: 1
```

## Benefits

1. **Simpler onboarding**: One command to remember (`mops init`)
2. **Clearer mental model**: "Initialize ModelOps" vs "Initialize config then infrastructure"
3. **Guided setup**: Progressive disclosure with smart defaults
4. **Future-proof**: Easy to add new setup steps without adding more init commands
5. **Single source of truth**: One config file instead of two
6. **Better validation**: Can validate entire configuration holistically
7. **Atomic operations**: Either everything is configured or nothing is

## Implementation Tasks

### Phase 1: Core Implementation
1. **Create unified config models** (`src/modelops/core/unified_config.py`)
   - Define Pydantic models for unified configuration
   - Add validation and smart defaults
   - Add migration helper from old format to new

2. **Create unified init command** (`src/modelops/cli/init.py`)
   - Implement main `init()` function
   - Merge logic from `config.init()` and `infra.init()`
   - Add `--interactive` flag (default: False)
   - Move Azure helper functions to shared location

3. **Update main CLI** (`src/modelops/cli/main.py`)
   - Add top-level `init` command
   - Hide old init subcommands with `hidden=True`

4. **Update config loading** (`src/modelops/core/config.py`)
   - Support loading from new unified file
   - Fallback to old files for compatibility during transition
   - Update `get_username()` to use unified config

5. **Update paths** (`src/modelops/core/paths.py`)
   - Add `UNIFIED_CONFIG_FILE = Path.home() / ".modelops" / "modelops.yaml"`
   - Update references throughout codebase

### Phase 2: Integration Updates
1. **Update infra commands**
   - Modify `infra up` to read from unified config
   - Update spec loading in `UnifiedInfraSpec`

2. **Update example Makefile**
   - Change from two init commands to one
   - Update `examples/simulation-workflow/Makefile`

3. **Documentation updates**
   - Update README.md quickstart
   - Update docs/setup/quick-start.md
   - Update CLI reference documentation

### Phase 3: Testing & Polish
1. **Add comprehensive tests**
   - Test unified config model validation
   - Test init command in both modes
   - Test migration from old format

2. **Add migration command** (optional)
   - `mops config migrate` to convert old files to new format

## Implementation Code Sketch

### src/modelops/cli/init.py
```python
import typer
from pathlib import Path
from ..core.unified_config import UnifiedModelOpsConfig
from .display import console, success, error, info
from .infra import get_azure_subscriptions, get_aks_versions

def init(
    interactive: bool = typer.Option(
        False, "--interactive", "-i",
        help="Interactive mode with prompts"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Custom output path (default: ~/.modelops/modelops.yaml)"
    )
):
    """Initialize ModelOps configuration."""
    import getpass
    import shutil

    # Check prerequisites
    if not shutil.which("az"):
        error("Azure CLI not found. Install: https://aka.ms/azure-cli")
        raise typer.Exit(1)

    # Get Azure subscriptions
    subs = get_azure_subscriptions()
    if not subs:
        error("No Azure subscriptions found. Run: az login")
        raise typer.Exit(1)

    # Build configuration
    config = UnifiedModelOpsConfig(
        settings=GeneralSettings(
            username=getpass.getuser(),
            environment="dev",
            provider="azure"
        ),
        pulumi=PulumiSettings(),
        cluster=ClusterSpec(
            subscription_id=subs[0]['id'],
            resource_group=f"modelops-{getpass.getuser()}",
            location="eastus2",
            aks=AKSSpec(
                kubernetes_version="1.30",
                node_pools=[
                    NodePoolSpec(
                        name="system",
                        mode="System",
                        vm_size="Standard_B2s",
                        count=1
                    ),
                    NodePoolSpec(
                        name="workers",
                        mode="User",
                        vm_size="Standard_B4ms",
                        min=1,
                        max=3
                    )
                ]
            )
        )
    )

    if interactive:
        # Prompt for customization
        config = prompt_for_config(config, subs)

    # Save configuration
    output = output or Path.home() / ".modelops" / "modelops.yaml"
    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, 'w') as f:
        yaml.dump(config.model_dump(), f, default_flow_style=False)

    success(f"✓ Configuration saved to {output}")
    console.print("\nReady to deploy! Next steps:")
    console.print("  mops infra up       # Create cloud resources")
    console.print("  mops job submit     # Run your first experiment")
```

## Decisions Made

1. **Single unified file**: Yes, merge into `~/.modelops/modelops.yaml`
2. **Hide old commands**: Yes, use `hidden=True` (no deprecation warnings needed)
3. **Command name**: Use `init` (simpler and more standard than `setup`)
4. **Default mode**: Non-interactive with smart defaults
5. **Config format**: YAML with nested structure matching Pydantic models

## Migration Notes

- Since this is unreleased software, no backward compatibility is required
- Existing users (if any) can manually run `mops init` to recreate config
- Old config files can be left in place and ignored

## Success Criteria

1. Single `mops init` command creates complete configuration
2. Non-interactive mode works with zero prompts
3. Interactive mode provides helpful prompts for customization
4. Generated config works with `mops infra up` immediately
5. Makefile in examples works with new command
6. Documentation is clear and complete
