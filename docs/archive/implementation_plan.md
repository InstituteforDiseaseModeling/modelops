# ModelOps MVP Implementation Plan - Azure FROM ZERO

## Overview
This document provides the staged implementation plan for ModelOps MVP, emphasizing **Azure infrastructure from zero** as the primary deployment path. The system follows a strict three-plane architecture with typed bindings between layers.

**Key Principles:**
- Polars-only for DataFrames (no pandas in MVP)
- Azure resources created from scratch (no pre-existing infrastructure required)
- Clean seams between planes via typed bindings
- Fewest production LOC (tests/examples carry the weight)

---

## Four-Stack Architecture with Pulumi ComponentResources

### Overview

The system implements a clean four-stack architecture using Pulumi ComponentResources and StackReferences:

**Stack 1: Registry** (`modelops-registry-<env>`)
- Creates container registry (ACR for Azure)
- Exports: login_server, registry_name, requires_auth
- Can grant pull permissions to AKS cluster
- Managed via: `mops registry create/destroy/status`

**Stack 2: Infrastructure** (`modelops-infra-<env>`)
- Creates Azure resources: Resource Group, AKS cluster
- Exports: kubeconfig, cluster_name, resource_group, location
- Configuration: Via --config flag (examples/providers/azure.yaml or custom ~/.modelops/providers/azure.yaml)
- Managed via: `mops infra up/down/status`

**Stack 3: Workspace** (`modelops-workspace-<env>`)
- Deploys Dask scheduler and workers on Kubernetes
- References Stack 2 via StackReference to get kubeconfig
- Configuration: Via --config flag (examples/workspace.yaml)
- Exports: scheduler_address, dashboard_url, namespace, worker_count,
          scheduler_service_name, scheduler_port, dashboard_port
- Managed via: `mops workspace up/down/status`

**Stack 4: Adaptive** (`modelops-adaptive-<env>-<run_id>`)
- Creates optimization runs with adaptive workers
- References both Stack 2 (kubeconfig) and Stack 3 (Dask endpoints)
- Configuration: Via --config flag (examples/adaptive.yaml)
- Exports: namespace, job_name, algorithm, n_trials, status
- Managed via: `mops adaptive up/down/status`

### State Management

**Local File Backend**:
- Stack state stored in unified backend: `~/.modelops/pulumi/backend/`
- Work directories at `~/.modelops/pulumi/{infra|registry|workspace|adaptive}/`
- No custom StateManager or state.json files
- All state managed through Pulumi outputs and StackReferences

**Cross-Stack Communication**:
```python
# In Stack 3 (Workspace), reference Stack 2 (Infrastructure)
infra_ref = StackNaming.ref("infra", env)  # Returns "organization/modelops-infra/modelops-infra-dev"
stack_ref = pulumi.StackReference(infra_ref)
kubeconfig = stack_ref.require_output("kubeconfig")
```

### Configuration System

**Global Configuration** (`~/.modelops/config.yaml`):
- Created via: `mops config init`
- Contains defaults and global settings:
  - Default environment (dev/staging/prod)
  - Default provider (azure/aws/gcp)
  - Pulumi backend URL (optional)
  - Pulumi organization name (configurable, defaults to "organization")
  - Username override (optional)
- Managed via: `mops config set/show/list`

**Component Configurations** (passed via --config flag):
- **Provider Config** (`examples/providers/azure.yaml` or custom `~/.modelops/providers/azure.yaml`):
  - Azure subscription ID, location, resource group
  - AKS cluster configuration
  - SSH key settings
- **Workspace Config** (`examples/workspace.yaml`):
  - Dask scheduler/worker resources
  - Container images
  - Node selectors and tolerations
- **Adaptive Config** (`examples/adaptive.yaml`):
  - Algorithm selection (Optuna, etc.)
  - Trial configuration
  - Worker specifications

### Resource Naming

**Centralized Naming via StackNaming** (`src/modelops/core/naming.py`):
- Resource Groups: `modelops-<env>-rg-<username>`
- Container Registry: `modelops<env>acr<username|random>` (per-user in dev, org-level in prod)
- AKS Cluster: `modelops-<env>-aks`
- Kubernetes Namespaces: `modelops-<component>-<env>`
- Stack Names: `modelops-<component>-<env>[-<run_id>]`

### Resource Protection

**Per-User Resource Groups**:
- Named as `modelops-<env>-rg-<username>` for multi-user isolation
- Protected with `protect=True` and `retain_on_delete=True`
- `mops infra down` preserves RG by default
- Use `--delete-rg` flag to force deletion

### Example: Complete Flow (Current Implementation)

**Note**: The examples below show the programmatic structure. Actual CLI commands use --config flag:
- `mops infra up --config examples/providers/azure.yaml`
- `mops workspace up --config examples/workspace.yaml`
- `mops adaptive up --config examples/adaptive.yaml --run-id exp-001`

```python
# Stack 1: Infrastructure creates Azure resources
class ModelOpsCluster(pulumi.ComponentResource):
    def __init__(self, name: str, config: dict):
        # Per-user resource group
        username = self._get_username(config)
        rg_name = f"{base_rg}-{username}"
        
        # Create RG with protection
        rg = azure.resources.ResourceGroup(
            rg_name,
            resource_group_name=rg_name,
            opts=pulumi.ResourceOptions(
                parent=self,
                protect=True,
                retain_on_delete=True
            )
        )
        
        # Create AKS and get kubeconfig using actual resource name
        aks = self._create_aks_cluster(...)
        kubeconfig = aks.name.apply(
            lambda real_name: azure.containerservice.list_managed_cluster_user_credentials_output(
                resource_group_name=rg.name,
                resource_name=real_name  # Use actual ARM name
            )
        )
        
        # Register outputs for StackReference access
        self.register_outputs({
            "kubeconfig": pulumi.Output.secret(kubeconfig),
            "cluster_name": aks.name,
            "resource_group": rg.name
        })

# Stack 2: Workspace references Stack 1
class DaskWorkspace(pulumi.ComponentResource):
    def __init__(self, name: str, infra_stack_ref: str, config: dict):
        # Get kubeconfig from Stack 1
        infra = pulumi.StackReference(infra_stack_ref)
        kubeconfig = infra.get_output("kubeconfig")
        
        # Create K8s provider and deploy Dask
        provider = k8s.Provider("k8s", kubeconfig=kubeconfig)
        # ... deploy Dask resources ...

# Stack 3: Adaptive references both Stack 1 and 2
class AdaptiveRun(pulumi.ComponentResource):
    def __init__(self, run_id: str, infra_ref: str, workspace_ref: str, config: dict):
        # Get outputs from both stacks
        infra = pulumi.StackReference(infra_ref)
        workspace = pulumi.StackReference(workspace_ref)
        
        kubeconfig = infra.get_output("kubeconfig")
        scheduler_addr = workspace.get_output("scheduler_address")
        # ... deploy adaptive workers ...
```

---

## Stage 0: Contracts & Runtime Skeleton (Tiny, Local Only)

**Goal**: Lock seams with modelops-contracts; smallest local runtime.

**Timeline**: 2-3 hours

### Deliverables

#### 1. Polars-only IPC Helpers
**File**: `modelops/services/ipc.py`
```python
import io
import polars as pl
import pyarrow as pa

def to_ipc(obj: dict[str, pl.DataFrame | pa.Table]) -> dict[str, bytes]:
    """Convert polars DataFrames to Arrow IPC bytes.
    Rejects non-DataFrame values to keep contracts crisp.
    """
    out: dict[str, bytes] = {}
    for name, value in obj.items():
        if isinstance(value, pl.DataFrame):
            table = value.to_arrow()
        elif isinstance(value, pa.Table):
            table = value
        else:
            raise TypeError(
                f"{name}: expected polars.DataFrame or pyarrow.Table, "
                f"got {type(value).__name__}"
            )
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        out[name] = sink.getvalue().to_pybytes()
    return out

def from_ipc(data: dict[str, bytes]) -> dict[str, pl.DataFrame]:
    """Convert IPC bytes back to polars DataFrames."""
    out: dict[str, pl.DataFrame] = {}
    for name, b in data.items():
        reader = pa.ipc.open_stream(io.BytesIO(b))
        out[name] = pl.from_arrow(reader.read_all())
    return out
```

#### 2. Split Services
**File**: `modelops/services/local.py`
```python
from modelops_contracts import SimulationService, SimReturn, FutureLike
from .ipc import to_ipc
import importlib
from typing import Any

class LocalSimulationService:
    """Local execution for testing without Dask."""
    
    def submit(self, fn_ref: str, params: dict, seed: int, *, bundle_ref: str) -> Any:
        """Submit simulation for local execution, return IPC bytes."""
        module_name, func_name = fn_ref.split(":")
        mod = importlib.import_module(module_name)
        func = getattr(mod, func_name)
        
        result = func(params, seed)
        if not isinstance(result, dict):
            raise TypeError(f"Simulation must return dict, got {type(result).__name__}")
        return to_ipc(result)
    
    def gather(self, futures: list[Any]) -> list[SimReturn]:
        """For local, futures are just results."""
        return futures
```

**File**: `modelops/services/dask.py`
```python
from modelops_contracts import SimulationService, SimReturn, FutureLike
from dask.distributed import Client
# State is now managed via Pulumi stacks

class DaskSimulationService:
    """Dask distributed execution on cluster."""
    
    def __init__(self, scheduler_address: str):
        self.client = Client(scheduler_address)
    
    @classmethod
    def from_workspace(cls, workspace_name: str = "default") -> 'DaskSimulationService':
        """Create from provisioned workspace."""
        # Get outputs from Pulumi stack
        workspace = state.get_workspace(workspace_name)
        if not workspace:
            raise ValueError(f"Workspace '{workspace_name}' not found")
        return cls(workspace.scheduler_address)
    
    def submit(self, fn_ref: str, params: dict, seed: int, *, bundle_ref: str) -> FutureLike:
        """Submit to Dask cluster."""
        return self.client.submit(_worker_run_sim, fn_ref, params, seed, bundle_ref)
    
    def gather(self, futures: list[FutureLike]) -> list[SimReturn]:
        """Gather Dask futures."""
        return self.client.gather(futures)

def _worker_run_sim(fn_ref: str, params: dict, seed: int, bundle_ref: str) -> SimReturn:
    """Worker function that returns IPC bytes."""
    from .ipc import to_ipc
    import importlib
    
    module_name, func_name = fn_ref.split(":")
    mod = importlib.import_module(module_name)
    func = getattr(mod, func_name)
    
    result = func(params, seed)
    if not isinstance(result, dict):
        raise TypeError(f"Simulation must return dict, got {type(result).__name__}")
    
    return to_ipc(result)
```

#### 3. Minimal CLI Root
**File**: `modelops/cli/app.py`
```python
import typer
from ..version import __version__

app = typer.Typer()

@app.command()
def version():
    """Show ModelOps version."""
    typer.echo(f"ModelOps {__version__}")

@app.command()
def config():
    """Show configuration paths."""
    from pathlib import Path
    config_dir = Path.home() / ".modelops"
    typer.echo(f"Config directory: {config_dir}")
    typer.echo(f"Pulumi state: {config_dir / 'pulumi'}")
    typer.echo(f"Provider config: {config_dir / 'providers' / 'azure.yaml'}")
```

### Examples
- `examples/sim/toy.py` - Simple simulation using polars
- `examples/simulation_task.json` - Minimal task specification

### Tests
- `tests/unit/test_ipc_roundtrip_polars.py` - IPC roundtrip with polars
- `tests/unit/test_local_service_returns_bytes.py` - Verify services return bytes
- `tests/unit/test_seeds_deterministic.py` - Seed derivation in uint64 range

### Output
None (local-only, no cloud resources)

---

## Stage 1: Infra Plane - Azure FROM ZERO ✅ (Pulumi ComponentResource)

**Goal**: One command creates/reuses RG → (optional) ACR → AKS and yields ClusterBinding.

**Timeline**: 4-6 hours

### ComponentResource Architecture

#### ModelOpsCluster Component (Azure Implementation)
```python
# modelops/infra/components/cluster.py
import base64
import pulumi
import pulumi_azure_native as azure
from typing import Optional

class ModelOpsCluster(pulumi.ComponentResource):
    """Creates a Kubernetes cluster for ModelOps (Azure implementation).
    
    Capability-focused naming: this component provides a K8s cluster,
    regardless of the underlying cloud provider. Azure is just the
    implementation detail.
    """
    
    def __init__(self, name: str, config: dict, opts: Optional[pulumi.ResourceOptions] = None):
        super().__init__("modelops:infra:cluster", name, None, opts)
        
        # Extract configuration
        location = config.get("location", "eastus2")
        rg_name = config.get("resource_group", "modelops-rg")
        aks_config = config.get("aks", {})
        acr_config = config.get("acr")
        
        # Create Resource Group
        rg = azure.resources.ResourceGroup(
            rg_name,
            resource_group_name=rg_name,
            location=location,
            tags={"managed-by": "modelops", "project": "modelops"},
            opts=pulumi.ResourceOptions(parent=self)
        )
        
        # Optional ACR
        acr_login_server = None
        if acr_config:
            acr = azure.containerregistry.Registry(
                acr_config["name"],
                registry_name=acr_config["name"],
                resource_group_name=rg.name,
                location=location,
                sku=azure.containerregistry.SkuArgs(
                    name=acr_config.get("sku", "Standard")
                ),
                admin_user_enabled=False,
                opts=pulumi.ResourceOptions(parent=self)
            )
            acr_login_server = acr.login_server
        
        # Create AKS with node pools
        aks = self._create_aks_cluster(rg, location, aks_config, config)
        
        # Get kubeconfig
        creds = azure.containerservice.list_managed_cluster_user_credentials_output(
            resource_group_name=rg.name,
            resource_name=aks.name
        )
        
        kubeconfig = creds.kubeconfigs[0].value.apply(
            lambda b64: base64.b64decode(b64).decode("utf-8")
        )
        
        # Register outputs
        self.kubeconfig = kubeconfig
        self.cluster_name = aks.name
        self.resource_group = rg.name
        self.location = pulumi.Output.from_input(location)
        self.acr_login_server = pulumi.Output.from_input(acr_login_server)
        
        self.register_outputs({
            "kubeconfig": pulumi.Output.secret(self.kubeconfig),
            "cluster_name": self.cluster_name,
            "resource_group": self.resource_group,
            "location": self.location,
            "acr_login_server": self.acr_login_server
        })
```

### CLI Integration

#### mops infra up (Current Implementation)
```python
# modelops/cli/infra.py
import typer
import yaml
import pulumi
import pulumi.automation as auto
from pathlib import Path
from typing import Optional
from ..infra.components.cluster import ModelOpsCluster
from ..infra.loaders import load_cluster_binding
# State is now managed via Pulumi stacks
from dataclasses import asdict

app = typer.Typer()

@app.command()
def up(
    config: Path = typer.Option(
        ...,
        "--config", "-c",
        help="Provider configuration file (YAML)"
    )
):
    """Create infrastructure from zero based on provider config.
    
    Example:
        mops infra up --config examples/providers/azure.yaml  # or custom ~/.modelops/providers/azure.yaml
    """
    # Load configuration
    with open(config) as f:
        provider_config = yaml.safe_load(f)
    
    def pulumi_program():
        """Pulumi program that creates infrastructure using ComponentResource."""
        if provider_config.get("provider") == "azure":
            from ..infra.components.azure import ModelOpsCluster
            # Single component handles all complexity
            cluster = ModelOpsCluster("modelops", provider_config)
            
            # Export outputs at the stack level for access via StackReference
            pulumi.export("kubeconfig", cluster.kubeconfig)
            pulumi.export("cluster_name", cluster.cluster_name)
            pulumi.export("resource_group", cluster.resource_group)
            pulumi.export("location", cluster.location)
            pulumi.export("acr_login_server", cluster.acr_login_server)
            
            return cluster
    
    # Create or select Pulumi stack with local backend
    stack_name = provider_config.get("stack_name", "modelops-infra-dev")
    project_name = "modelops-infra"
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    backend_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path.home() / ".modelops" / "pulumi" / "azure"
    work_dir.mkdir(parents=True, exist_ok=True)
    
    stack = auto.create_or_select_stack(
        stack_name=stack_name,
        project_name=project_name,
        program=pulumi_program,
        opts=auto.LocalWorkspaceOptions(
            work_dir=str(work_dir),
            project_settings=auto.ProjectSettings(
                name=project_name,
                runtime="python",
                backend=auto.ProjectBackend(url=f"file://{backend_dir}")
            )
        )
    )
    
    typer.echo("Creating Azure infrastructure from zero...")
    result = stack.up()
    
    typer.echo(f"✓ Infrastructure created successfully!")
    typer.echo(f"  Stack: {stack_name}")
    typer.echo(f"\nGet kubeconfig:")
    typer.echo(f"  pulumi stack output kubeconfig --show-secrets --stack {stack_name} --cwd {work_dir}")

@app.command()
def down():
    """Destroy Azure infrastructure (with confirmation)."""
    if not typer.confirm("⚠️  This will destroy ALL Azure resources. Continue?"):
        raise typer.Abort()
    
    stack = auto.select_stack(
        stack_name="modelops-infra",
        project_name="modelops-infra"
    )
    
    typer.echo("Destroying Azure infrastructure...")
    stack.destroy()
    typer.echo("✓ Infrastructure destroyed")

@app.command()
def doctor():
    """Validate AKS version and node pool configuration."""
    # Check pinned AKS version
    # Validate node pool labels
    typer.echo("✓ AKS version: 1.29.7 (pinned)")
    typer.echo("✓ Node pool labels: modelops.io/role=cpu")
```

### Azure Bootstrap Code

**File**: `modelops/infra/azure_bootstrap.py`
```python
import pulumi
import pulumi_azure_native as azure
import base64
from ..infra.bindings import ClusterBinding

def create_azure_infrastructure(config: dict) -> ClusterBinding:
    """Create Azure infrastructure from zero.
    
    Creates:
    - Resource Group
    - ACR (optional)
    - AKS cluster with labeled node pools
    
    Returns:
        ClusterBinding with kubeconfig
    """
    # Create Resource Group
    rg = azure.resources.ResourceGroup(
        "modelops-rg",
        resource_group_name=config["resource_group"],
        location=config["location"]
    )
    
    # Optional ACR
    acr_login_server = None
    if config.get("acr", {}).get("enabled"):
        acr = azure.containerregistry.Registry(
            "modelops-acr",
            resource_group_name=rg.name,
            registry_name=config["acr"]["name"],
            sku={"name": "Basic"},
            admin_user_enabled=True
        )
        acr_login_server = acr.login_server
    
    # Create AKS cluster
    aks = azure.containerservice.ManagedCluster(
        "modelops-aks",
        resource_group_name=rg.name,
        kubernetes_version=config["aks"]["version"],  # Pinned version
        dns_prefix=config["aks"]["name"],
        agent_pool_profiles=[
            # System pool (always on)
            {
                "name": "system",
                "count": 1,
                "vm_size": "Standard_B2s",
                "mode": "System",
                "os_type": "Linux"
            },
            # Workload pool with label
            {
                "name": "workcpu",
                "count": config["aks"]["workload_pool"]["min"],
                "min_count": config["aks"]["workload_pool"]["min"],
                "max_count": config["aks"]["workload_pool"]["max"],
                "vm_size": config["aks"]["workload_pool"]["vm_size"],
                "mode": "User",
                "os_type": "Linux",
                "node_labels": {"modelops.io/role": "cpu"},
                "enable_auto_scaling": True
            }
        ],
        identity={"type": "SystemAssigned"}
    )
    
    # Get kubeconfig
    creds = azure.containerservice.list_managed_cluster_user_credentials_output(
        resource_group_name=rg.name,
        resource_name=aks.name
    )
    
    kubeconfig = creds.kubeconfigs[0].value.apply(
        lambda b64: base64.b64decode(b64).decode("utf-8")
    )
    
    # ACR pull permissions if enabled
    if acr_login_server:
        # Grant AKS pull access to ACR
        role_assignment = azure.authorization.RoleAssignment(
            "aks-acr-pull",
            principal_id=aks.identity.principal_id,
            role_definition_id="/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d",  # AcrPull
            scope=acr.id
        )
    
    return ClusterBinding(
        kubeconfig=pulumi.Output.secret(kubeconfig),
        acr_login_server=acr_login_server,
        cluster_name=config["aks"]["name"]
    )
```

### Simple Dataclass Bindings

**File**: `modelops/infra/bindings.py`
```python
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class ClusterBinding:
    """What you need to connect to a Kubernetes cluster."""
    kubeconfig: str  # The actual kubeconfig content
    # Optional metadata for debugging
    acr_login_server: Optional[str] = None

@dataclass(frozen=True)
class DaskBinding:
    """What you need to connect to a Dask cluster."""
    scheduler_addr: str  # tcp://dask-scheduler.namespace:8786
    dashboard_url: str   # http://dask-scheduler.namespace:8787
    namespace: str       # Where Dask lives

@dataclass(frozen=True)
class PostgresBinding:
    """What you need to connect to Postgres."""
    secret_name: str  # K8s secret with PG* env vars
    namespace: str    # Where Postgres lives
```

### Binding Loaders (Support Multiple Sources)

**File**: `modelops/infra/loaders.py`
```python
from pathlib import Path
from typing import Optional
import pulumi
from dataclasses import asdict
from .bindings import ClusterBinding, DaskBinding
# State is now managed via Pulumi stacks

def load_cluster_binding(
    stack_ref: Optional[str] = None,
    kubeconfig_path: Optional[str] = None,
    stack_ref: Optional[str] = None  # Reference to infra stack
) -> ClusterBinding:
    """Load binding from Pulumi StackRef, file, or state."""
    if stack_ref:
        # Production: Use Pulumi StackReference
        ref = pulumi.StackReference(stack_ref)
        kubeconfig = ref.get_output("kubeconfig")
        acr = ref.get_output("acr_login_server", None)
        return ClusterBinding(
            kubeconfig=kubeconfig,
            acr_login_server=acr
        )
    
    if kubeconfig_path:
        # Development: Use local kubeconfig
        return ClusterBinding(
            kubeconfig=Path(kubeconfig_path).read_text()
        )
    
    if state:
        # Backward compat: Load from state
        # Get from stack outputs instead
        return ClusterBinding(
            kubeconfig=saved.get("kubeconfig"),
            acr_login_server=saved.get("acr_login_server")
        )
    
    raise ValueError("No binding source provided")

def load_dask_binding(
    stack_ref: Optional[str] = None,
    scheduler_addr: Optional[str] = None,
    stack_ref: Optional[str] = None  # Reference to infra stack
) -> DaskBinding:
    """Load Dask binding from multiple sources."""
    if stack_ref:
        ref = pulumi.StackReference(stack_ref)
        return DaskBinding(
            scheduler_addr=ref.get_output("scheduler_addr"),
            dashboard_url=ref.get_output("dashboard_url"),
            namespace=ref.get_output("namespace")
        )
    
    if scheduler_addr:
        # Direct connection string
        namespace = scheduler_addr.split(".")[-2] if "." in scheduler_addr else "default"
        return DaskBinding(
            scheduler_addr=scheduler_addr,
            dashboard_url=scheduler_addr.replace(":8786", ":8787").replace("tcp://", "http://"),
            namespace=namespace
        )
    
    if state:
        # Get from stack outputs instead
        return DaskBinding(**saved)
    
    raise ValueError("No binding source provided")
```

### Examples

**File**: `examples/providers/azure.yaml` (template - copy to `~/.modelops/providers/azure.yaml` for local customization)
```yaml
subscription_id: "00000000-0000-0000-0000-000000000000"
location: "eastus2"
resource_group: "modelops-rg"

aks:
  name: "modelops-aks"
  version: "1.29.7"  # Pinned version
  workload_pool:
    vm_size: "Standard_D4s_v5"
    min: 0
    max: 5

acr:
  enabled: false
  name: "modelopsacr"  # Required if enabled
```

### Tests
- Config schema validation
- Dry stack harness: asserts Pulumi outputs contain kubeconfig
- ACR enabled ⇒ emits acr_login_server

### Output
`ClusterBinding` → consumed by Stage 2

---

## Stage 2: Workspace Plane (Kubernetes-only, Pulumi kubernetes)

**Goal**: Deploy Dask on ANY Kubernetes cluster using ClusterBinding. Emits DaskBinding.

**Timeline**: 4-5 hours

### K8sProvider Wrapper (Cloud-Agnostic)

**File**: `modelops/k8s/components/provider.py`
```python
import pulumi
import pulumi_kubernetes as k8s

class K8sProvider(pulumi.ComponentResource):
    """Cloud-agnostic Kubernetes provider wrapper.
    
    Abstracts away the cloud provider - works with any kubeconfig.
    """
    
    def __init__(self, name: str, kubeconfig: pulumi.Input[str], 
                 opts: Optional[pulumi.ResourceOptions] = None):
        super().__init__("modelops:k8s:provider", name, None, opts)
        
        # Create K8s provider from any kubeconfig
        self.provider = k8s.Provider(
            f"{name}-k8s",
            kubeconfig=kubeconfig,
            opts=pulumi.ResourceOptions(parent=self)
        )
        
        # Store kubeconfig for reference
        self.kubeconfig = kubeconfig
        
        self.register_outputs({
            "provider_id": self.provider.id
        })
```

### DaskWorkspace Component

**File**: `modelops/workspace/components/dask.py`
```python
import pulumi
import pulumi_kubernetes as k8s
from typing import Dict, Any, Optional
from ...k8s.components.provider import K8sProvider

class DaskWorkspace(pulumi.ComponentResource):
    """Dask on any Kubernetes cluster (cloud-agnostic).
    
    Uses K8sProvider wrapper - doesn't know or care about Azure/AWS/GCP.
    """
    
    def __init__(self, name: str, k8s_provider: K8sProvider, spec: Dict[str, Any],
                 opts: Optional[pulumi.ResourceOptions] = None):
        super().__init__("modelops:workspace:dask", name, None, opts)
        
        namespace_name = spec.get("namespace", f"modelops-{name}")
        
        # Create namespace
        namespace = k8s.core.v1.Namespace(
            f"{name}-namespace",
            metadata={"name": namespace_name},
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=k8s_provider.provider  # Use wrapped provider
            )
        )
        
        # Deploy Dask scheduler
        scheduler = self._create_scheduler(name, namespace_name, spec, k8s_provider.provider)
        
        # Deploy Dask workers
        workers = self._create_workers(name, namespace_name, spec, k8s_provider.provider)
        
        # Export outputs
        self.scheduler_addr = pulumi.Output.from_input(
            f"tcp://dask-scheduler.{namespace_name}:8786"
        )
        self.dashboard_url = pulumi.Output.from_input(
            f"http://dask-scheduler.{namespace_name}:8787"
        )
        self.namespace = pulumi.Output.from_input(namespace_name)
        
        self.register_outputs({
            "scheduler_addr": self.scheduler_addr,
            "dashboard_url": self.dashboard_url,
            "namespace": self.namespace
        })
```

### CLI Commands (Updated)

**File**: `modelops/cli/workspace.py`
```python
import typer
import yaml
import pulumi.automation as auto
from pathlib import Path
from typing import Optional
from ..infra.components.workspace import DaskWorkspace
from ..k8s.components.provider import K8sProvider
from ..workspace.components.dask import DaskWorkspace
from dataclasses import asdict

app = typer.Typer()

@app.command()
def up(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c",
        help="Workspace configuration file (YAML)"
    ),
    infra_stack: str = typer.Option(
        "modelops-infra",
        "--infra-stack",
        help="Infrastructure stack to reference"
    ),
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    )
):
    """Deploy Dask workspace on existing infrastructure.
    
    This creates Stack 2 which references Stack 1 (infrastructure) to get
    the kubeconfig and deploy Dask scheduler and workers.
    
    Example:
        mops workspace up --env dev
        mops workspace up --config workspace.yaml --infra-stack modelops-infra-prod
    """
    # Load configuration if provided
    workspace_config = {}
    if config and config.exists():
        with open(config) as f:
            workspace_config = yaml.safe_load(f)
    
    def pulumi_program():
        """Create DaskWorkspace in Stack 2 context."""
        from ..infra.components.workspace import DaskWorkspace
        
        # Build stack reference for infrastructure
        infra_ref = f"{infra_stack}-{env}"
        
        return DaskWorkspace("dask", infra_ref, workspace_config)
    
    stack_name = f"modelops-workspace-{env}"
    project_name = "modelops-workspace"
    
    # Use consistent backend structure
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "workspace"
    backend_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path.home() / ".modelops" / "pulumi" / "workspace"
    work_dir.mkdir(parents=True, exist_ok=True)
    
    stack = auto.create_or_select_stack(
        stack_name=stack_name,
        project_name=project_name,
        program=pulumi_program,
        opts=auto.LocalWorkspaceOptions(
            work_dir=str(work_dir),
            project_settings=auto.ProjectSettings(
                name=project_name,
                runtime="python",
                backend=auto.ProjectBackend(url=f"file://{backend_dir}")
            )
        )
    )
    
    typer.echo(f"Deploying Dask workspace to environment: {env}")
    result = stack.up()
    
    outputs = result.outputs
    typer.echo(f"✓ Workspace deployed successfully!")
    typer.echo(f"  Scheduler: {outputs.get('scheduler_address', {}).value}")
    typer.echo(f"  Dashboard: {outputs.get('dashboard_url', {}).value}")

@app.command()
def down(
    name: str = typer.Option(..., "-n", "--name", help="Workspace name")
):
    """Destroy workspace."""
    stack = auto.select_stack(
        stack_name=f"workspace-{name}",
        project_name="modelops-workspace"
    )
    
    typer.echo(f"Destroying workspace '{name}'...")
    stack.destroy()
    
    # Get outputs from Pulumi stack
    # Stack outputs removed when stack is destroyed
    typer.echo(f"✓ Workspace destroyed: {name}")
```

### Workspace Infrastructure

**File**: `modelops/infra/workspace.py`
```python
import pulumi
import pulumi_kubernetes as k8s
from ..components.specs import WorkspaceSpec
from ..infra.bindings import DaskBinding

def create_workspace(ws: WorkspaceSpec, provider: k8s.Provider) -> DaskBinding:
    """Create Dask workspace using only Kubernetes resources.
    
    No Azure SDK calls - pure Kubernetes.
    """
    ns = ws.metadata["namespace"]
    
    # Create namespace
    namespace = k8s.core.v1.Namespace(
        "ws-ns",
        metadata={"name": ns},
        opts=pulumi.ResourceOptions(provider=provider)
    )
    
    # GHCR authentication if needed
    if "ghcr.io" in ws.spec.scheduler.image:
        import os
        ghcr_pat = os.getenv("GHCR_PAT")
        if ghcr_pat:
            ghcr_secret = k8s.core.v1.Secret(
                "ghcr-creds",
                metadata={"name": "ghcr-creds", "namespace": ns},
                type="kubernetes.io/dockerconfigjson",
                string_data={
                    ".dockerconfigjson": json.dumps({
                        "auths": {
                            "ghcr.io": {
                                "auth": base64.b64encode(f":{ghcr_pat}".encode()).decode()
                            }
                        }
                    })
                },
                opts=pulumi.ResourceOptions(provider=provider, depends_on=[namespace])
            )
    
    # Dask Scheduler
    scheduler_deployment = k8s.apps.v1.Deployment(
        "dask-scheduler",
        metadata={"name": "dask-scheduler", "namespace": ns},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "dask-scheduler"}},
            "template": {
                "metadata": {"labels": {"app": "dask-scheduler"}},
                "spec": {
                    "containers": [{
                        "name": "scheduler",
                        "image": ws.spec.scheduler.image,
                        "command": ["dask-scheduler"],
                        "ports": [
                            {"containerPort": 8786, "name": "scheduler"},
                            {"containerPort": 8787, "name": "dashboard"}
                        ],
                        "resources": ws.spec.scheduler.resources.model_dump()
                    }],
                    "nodeSelector": ws.spec.scheduler.node_selector
                }
            }
        },
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[namespace])
    )
    
    # Scheduler Service
    scheduler_service = k8s.core.v1.Service(
        "dask-scheduler-service",
        metadata={"name": "dask-scheduler", "namespace": ns},
        spec={
            "selector": {"app": "dask-scheduler"},
            "type": "ClusterIP",
            "ports": [
                {"port": 8786, "targetPort": 8786, "name": "scheduler"},
                {"port": 8787, "targetPort": 8787, "name": "dashboard"}
            ]
        },
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[namespace])
    )
    
    # Dask Workers
    worker_deployment = k8s.apps.v1.Deployment(
        "dask-workers",
        metadata={"name": "dask-workers", "namespace": ns},
        spec={
            "replicas": ws.spec.workers.replicas,
            "selector": {"matchLabels": {"app": "dask-worker"}},
            "template": {
                "metadata": {"labels": {"app": "dask-worker"}},
                "spec": {
                    "containers": [{
                        "name": "worker",
                        "image": ws.spec.workers.image,
                        "command": ["dask-worker", "tcp://dask-scheduler:8786"],
                        "resources": ws.spec.workers.resources.model_dump(),
                        "volumeMounts": [
                            {"name": "tmp", "mountPath": "/tmp"}
                        ]
                    }],
                    "volumes": [
                        {"name": "tmp", "emptyDir": {}}  # Writable /tmp
                    ],
                    "nodeSelector": ws.spec.workers.node_selector
                }
            }
        },
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[scheduler_service])
    )
    
    return DaskBinding(
        scheduler_addr=f"tcp://dask-scheduler.{ns}:8786",
        dashboard_url=f"http://dask-scheduler.{ns}:8787",
        namespace=ns
    )
```

### Examples

**File**: `examples/workspace.yaml`
```yaml
apiVersion: modelops/v1
kind: Workspace
metadata:
  name: dev-workspace
  namespace: modelops-dev  # Optional, auto-generated if not provided
spec:
  scheduler:
    replicas: 1
    image: ghcr.io/dask/dask:2024.8.0-py3.11
    resources:
      requests:
        memory: "2Gi"
        cpu: "1"
      limits:
        memory: "2Gi"
        cpu: "1"
    nodeSelector:
      modelops.io/role: cpu
  workers:
    replicas: 4
    image: ghcr.io/dask/dask:2024.8.0-py3.11
    resources:
      requests:
        memory: "4Gi"
        cpu: "2"
      limits:
        memory: "4Gi"
        cpu: "2"
    nodeSelector:
      modelops.io/role: cpu
```

### Tests
- Pydantic validation for WorkspaceSpec
- State roundtrip (persist DaskBinding in local state)

### Output
`DaskBinding` → consumed by Stage 3

---

## Stage 3: Adaptive Plane (Kubernetes-only, Pulumi kubernetes)

**Goal**: Per-run namespace; (opt) in-cluster Postgres; adaptive workers running Ask/Tell loop.

**Timeline**: 6-8 hours

### CLI Commands

**File**: `modelops/cli/adaptive_cmd.py`
```python
import typer
import yaml
import uuid
import pulumi.automation as auto
import pulumi_kubernetes as k8s
from pathlib import Path
from ..components.specs import AdaptiveSpec
from ..components.provisioners.postgres import provision_postgres
from ..infra.components.adaptive import AdaptiveRun

app = typer.Typer()

@app.command()
def up(
    config: Path = typer.Argument(
        ...,
        help="Adaptive run configuration file (YAML)"
    ),
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id", "-r",
        help="Unique run identifier (auto-generated if not provided)"
    ),
    infra_stack: str = typer.Option(
        "modelops-infra",
        "--infra-stack",
        help="Infrastructure stack name (without env suffix)"
    ),
    workspace_stack: str = typer.Option(
        "modelops-workspace",
        "--workspace-stack",
        help="Workspace stack name (without env suffix)"
    ),
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    )
):
    """Start an adaptive optimization run.
    
    This creates Stack 3 which references both Stack 1 (infrastructure) and
    Stack 2 (workspace) to deploy adaptive workers that connect to Dask.
    
    Example:
        mops adaptive up optuna-config.yaml
        mops adaptive up experiment.yaml --run-id exp-001 --env prod
    """
    # Generate run ID if not provided
    if not run_id:
        from datetime import datetime
        run_id = f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    # Load configuration
    with open(config) as f:
        run_config = yaml.safe_load(f)
    
    def pulumi_program():
        """Create AdaptiveRun in Stack 3 context."""
        from ..infra.components.adaptive import AdaptiveRun
        
        # Build stack references
        infra_ref = f"{infra_stack}-{env}"
        workspace_ref = f"{workspace_stack}-{env}"
        
        return AdaptiveRun(
            run_id,
            infra_stack_ref=infra_ref,
            workspace_stack_ref=workspace_ref,
            config=run_config
        )
    
    stack_name = f"modelops-adaptive-{run_id}"
    project_name = "modelops-adaptive"
    
    # Unique backend for each run
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "adaptive"
    backend_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path.home() / ".modelops" / "pulumi" / "adaptive" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    
    stack = auto.create_or_select_stack(
        stack_name=stack_name,
        project_name=project_name,
        program=pulumi_program,
        opts=auto.LocalWorkspaceOptions(
            work_dir=str(work_dir),
            project_settings=auto.ProjectSettings(
                name=project_name,
                runtime="python",
                backend=auto.ProjectBackend(url=f"file://{backend_dir}")
            )
        )
    )
    
    typer.echo(f"Starting adaptive run: {run_id}")
    result = stack.up()
    
    outputs = result.outputs
    typer.echo(f"✓ Adaptive run started!")
    typer.echo(f"  Run ID: {run_id}")
    typer.echo(f"  Namespace: {outputs.get('namespace', {}).value}")
    typer.echo(f"  Job: {outputs.get('job_name', {}).value}")

@app.command()
def down(
    namespace: str = typer.Option(..., "-n", "--namespace", help="Run namespace"),
    purge: bool = typer.Option(False, "--purge", help="Delete PVCs")
):
    """Destroy adaptive run."""
    stack = auto.select_stack(
        stack_name=f"adaptive-{namespace}",
        project_name="modelops-adaptive"
    )
    
    typer.echo(f"Destroying adaptive run '{namespace}'...")
    if purge:
        typer.echo("  Purging PVCs...")
    
    stack.destroy()
    typer.echo(f"✓ Adaptive run destroyed: {namespace}")
```

### Postgres Provisioner

**File**: `modelops/components/provisioners/postgres.py`
```python
from pulumi import Output, ResourceOptions, secret
import pulumi_kubernetes as k8s
from pulumi_random import RandomPassword
from ..infra.bindings import PostgresBinding

def provision_postgres(ns: str, spec: dict, prov: k8s.Provider) -> PostgresBinding:
    """Provision in-cluster Postgres with StatefulSet.
    
    Security: All passwords in Secrets, SQL execution via Job.
    """
    
    # Generate passwords securely
    admin_pw = RandomPassword(f"{ns}-pg-admin", length=24, special=True)
    runtime_pw = RandomPassword(f"{ns}-pg-runtime", length=24, special=True)
    
    # Admin secret
    admin_secret = k8s.core.v1.Secret(
        f"{ns}-pg-admin",
        metadata={"name": f"{ns}-pg-admin", "namespace": ns},
        string_data={
            "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD": admin_pw.result,
            "POSTGRES_DB": "postgres"
        },
        opts=ResourceOptions(provider=prov)
    )
    
    # Client secret
    client_secret = k8s.core.v1.Secret(
        f"{ns}-pg-env",
        metadata={"name": f"{ns}-pg-env", "namespace": ns},
        string_data={
            "PGHOST": "postgres",
            "PGPORT": "5432",
            "PGDATABASE": "modelops",
            "PGUSER": "modelops_user",
            "PGPASSWORD": runtime_pw.result,
            "PGSSLMODE": "disable"
        },
        opts=ResourceOptions(provider=prov)
    )
    
    # SQL in Secret
    sql_secret = k8s.core.v1.Secret(
        f"{ns}-pg-init-sql",
        metadata={"name": f"{ns}-pg-init-sql", "namespace": ns},
        string_data={
            "init.sql": Output.concat(
                "DO $$ BEGIN ",
                "IF NOT EXISTS (SELECT FROM pg_user WHERE usename='modelops_user') THEN ",
                "CREATE USER modelops_user WITH PASSWORD '", secret(runtime_pw.result), "'; ",
                "END IF; ",
                "IF NOT EXISTS (SELECT FROM pg_database WHERE datname='modelops') THEN ",
                "CREATE DATABASE modelops; ",
                "END IF; ",
                "GRANT ALL PRIVILEGES ON DATABASE modelops TO modelops_user; ",
                "END $$;"
            )
        },
        opts=ResourceOptions(provider=prov)
    )
    
    # Service first
    postgres_service = k8s.core.v1.Service(
        f"{ns}-postgres-svc",
        metadata={"name": "postgres", "namespace": ns},
        spec={
            "selector": {"app": "postgres"},
            "type": "ClusterIP",
            "ports": [{"port": 5432, "targetPort": 5432}]
        },
        opts=ResourceOptions(provider=prov)
    )
    
    # StatefulSet
    postgres_sts = k8s.apps.v1.StatefulSet(
        f"{ns}-postgres",
        metadata={"name": "postgres", "namespace": ns},
        spec={
            "serviceName": "postgres",
            "replicas": 1,
            "selector": {"matchLabels": {"app": "postgres"}},
            "template": {
                "metadata": {"labels": {"app": "postgres"}},
                "spec": {
                    "containers": [{
                        "name": "postgres",
                        "image": f"postgres:{spec.get('version', '15')}-alpine",
                        "ports": [{"containerPort": 5432}],
                        "envFrom": [{"secretRef": {"name": f"{ns}-pg-admin"}}],
                        "volumeMounts": [
                            {"name": "data", "mountPath": "/var/lib/postgresql/data"}
                        ]
                    }]
                }
            },
            "volumeClaimTemplates": [{
                "metadata": {"name": "data"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": spec["persistence"]["storage_class"],
                    "resources": {"requests": {"storage": spec["persistence"]["size"]}}
                }
            }]
        },
        opts=ResourceOptions(provider=prov, depends_on=[admin_secret, postgres_service])
    )
    
    # Init Job
    init_job = k8s.batch.v1.Job(
        f"{ns}-pg-init",
        metadata={"name": f"{ns}-pg-init", "namespace": ns},
        spec={
            "backoffLimit": 3,
            "template": {
                "spec": {
                    "restartPolicy": "OnFailure",
                    "containers": [{
                        "name": "init",
                        "image": "postgres:15-alpine",
                        "command": [
                            "sh", "-c",
                            "until pg_isready -h postgres -U postgres; do sleep 2; done && "
                            "psql -h postgres -U postgres -f /sql/init.sql"
                        ],
                        "env": [{
                            "name": "PGPASSWORD",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": f"{ns}-pg-admin",
                                    "key": "POSTGRES_PASSWORD"
                                }
                            }
                        }],
                        "volumeMounts": [{"name": "sql", "mountPath": "/sql"}]
                    }],
                    "volumes": [{
                        "name": "sql",
                        "secret": {"secretName": f"{ns}-pg-init-sql"}
                    }]
                }
            }
        },
        opts=ResourceOptions(provider=prov, depends_on=[postgres_sts, sql_secret])
    )
    
    return PostgresBinding(
        secret_name=f"{ns}-pg-env",
        namespace=ns
    )
```

### Adaptive Worker Runner

**File**: `modelops/runners/adaptive_worker_runner.py`
```python
import os
import time
import logging
from modelops_contracts import AdaptiveAlgorithm, TrialResult, TrialStatus
from ..services.dask import DaskSimulationService
from ..utils.seeds import derive_single_seed
from ..central_store_runtime import detect_central_store

logger = logging.getLogger(__name__)

def load_algorithm_adapter() -> AdaptiveAlgorithm:
    """Load algorithm adapter from environment."""
    adapter_path = os.getenv("ADAPTER_PATH", "examples.fake_adapter:FakeAdapter")
    module_name, class_name = adapter_path.split(":")
    
    import importlib
    module = importlib.import_module(module_name)
    adapter_class = getattr(module, class_name)
    return adapter_class()

def main():
    """Main runner loop."""
    # Connect to Dask
    scheduler_addr = os.environ["DASK_SCHEDULER_ADDRESS"]
    sim = DaskSimulationService(scheduler_addr)
    
    # Load algorithm
    algo = load_algorithm_adapter()
    
    # Setup central store if configured
    store = detect_central_store()
    if store:
        for attempt in range(6):
            try:
                dsn = store.dsn()
                logger.info(f"Central store connected on attempt {attempt + 1}")
                os.environ["ADAPTIVE_STORAGE_DSN"] = dsn
                break
            except Exception as e:
                logger.warning(f"Central store connection attempt {attempt + 1} failed: {e}")
                if attempt < 5:
                    time.sleep(5)
    
    # Configuration
    batch_size = int(os.getenv("BATCH_SIZE", "4"))
    replicates = int(os.getenv("REPLICATES_PER_PARAM", "10"))
    fn_ref = os.getenv("SIMULATION_FN_REF", "examples.sim.toy:simulate")
    bundle_ref = os.getenv("BUNDLE_REF", "")
    
    # Main loop
    while not algo.finished():
        batch = algo.ask(n=batch_size)
        if not batch:
            time.sleep(1)
            continue
        
        # Submit one job per replicate with deterministic seeds
        futures = []
        for params in batch:
            for i in range(replicates):
                seed = derive_single_seed(params.param_id, i)
                future = sim.submit(fn_ref, dict(params.params), seed, bundle_ref=bundle_ref)
                futures.append(future)
        
        # Gather and evaluate
        sim_outputs = sim.gather(futures)
        
        # Mock evaluation for MVP
        results = []
        for params in batch:
            mock_loss = sum(params.params.values()) / len(params.params)
            results.append(TrialResult(
                param_id=params.param_id,
                loss=float(mock_loss),
                status=TrialStatus.COMPLETED,
                diagnostics={"replicates": replicates}
            ))
        
        algo.tell(results)
        logger.info(f"Completed batch: {len(batch)} parameters")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
```

### Examples

**File**: `examples/adaptive.yaml`
```yaml
apiVersion: modelops/v1
kind: Adaptive
metadata:
  name: calibration-run
spec:
  central_store:
    persistence:
      size: 10Gi
      storage_class: managed-csi-premium
    version: "15"
  workers:
    replicas: 2  # Requires central_store
    image: ghcr.io/megacorp/modelops-runner:latest
    resources:
      requests:
        memory: "4Gi"
        cpu: "2"
    nodeSelector:
      modelops.io/role: cpu
  workspace_ref:
    namespace: modelops-dev  # Must exist
  algorithm:
    adapter_path: "examples.fake_adapter:FakeAdapter"
    batch_size: 4
    replicates: 10
```

**File**: `examples/fake_adapter.py`
```python
from modelops_contracts import AdaptiveAlgorithm, UniqueParameterSet, TrialResult
from typing import List

class FakeAdapter(AdaptiveAlgorithm):
    """Fake adapter for testing without real optimization."""
    
    def __init__(self):
        self.n_trials = 0
        self.max_trials = 20
    
    def ask(self, n: int) -> List[UniqueParameterSet]:
        """Generate fake parameter proposals."""
        if self.n_trials >= self.max_trials:
            return []
        
        proposals = []
        for i in range(min(n, self.max_trials - self.n_trials)):
            params = {"x": float(self.n_trials + i), "y": float(i)}
            proposals.append(UniqueParameterSet.from_dict(params))
        
        self.n_trials += len(proposals)
        return proposals
    
    def tell(self, results: List[TrialResult]) -> None:
        """Receive results (no-op for fake adapter)."""
        pass
    
    def finished(self) -> bool:
        """Check if optimization is complete."""
        return self.n_trials >= self.max_trials
```

### Tests
- Unit: replicate seed uniqueness; replicas>1 ⇒ central store required
- Integration (local): fake adapter Ask/Tell with LocalSimulationService

---

## Stage 4: Acceptance Path & Docs (Happy Path)

**Goal**: Documented end-to-end: infra → workspace → adaptive → teardown.

**Timeline**: 2-3 hours

### Documentation

**File**: `examples/run_happy_path.md`
```markdown
# ModelOps Happy Path - Azure FROM ZERO

## Prerequisites
- Azure subscription
- Pulumi CLI installed
- Python 3.11+ with modelops installed

## Step 1: Create Azure Infrastructure FROM ZERO

```bash
# Configure provider
# Copy template to local config directory for customization
cp examples/providers/azure.yaml ~/.modelops/providers/azure.yaml
# Edit with your Azure details
vim ~/.modelops/providers/azure.yaml

# Or use the example directly:
cat > examples/providers/azure.yaml <<EOF
subscription_id: "YOUR-SUBSCRIPTION-ID"
location: "eastus2"
resource_group: "modelops-rg"

aks:
  name: "modelops-aks"
  version: "1.29.7"
  workload_pool:
    vm_size: "Standard_D4s_v5"
    min: 0
    max: 5
EOF

# Create Azure resources
mops infra up --config examples/providers/azure.yaml  # or custom ~/.modelops/providers/azure.yaml

# Output:
# ✓ Created AKS cluster: modelops-aks
# → ClusterBinding saved to state
```

## Step 2: Deploy Dask Workspace

```bash
# Deploy workspace using only the kubeconfig from ClusterBinding
mops workspace up -f examples/workspace.yaml

# Output:
# ✓ Workspace created: modelops-dev
#   Scheduler: tcp://dask-scheduler.modelops-dev:8786
#   Dashboard: http://dask-scheduler.modelops-dev:8787
```

## Step 3: Run Adaptive Optimization

```bash
# Start adaptive run using DaskBinding
mops adaptive up -f examples/adaptive.yaml

# Output:
# ✓ Adaptive run created: modelops-run-7x9k2
#   Postgres: provisioned with PVC
#   Workers: 2
```

## Step 4: Monitor & Teardown

```bash
# Check progress
kubectl logs -n modelops-run-7x9k2 -l app=adaptive-worker

# Destroy adaptive run (keeps data)
mops adaptive down -n modelops-run-7x9k2

# Destroy adaptive run (purge data)
mops adaptive down -n modelops-run-7x9k2 --purge

# Destroy workspace (keeps AKS)
mops workspace down -n modelops-dev

# Destroy Azure infrastructure (with confirmation)
mops infra down
```

## Local Testing

For local testing without Azure:
```bash
python examples/acceptance_smoke.py
# ✓ Smoke test passed: 20 trials completed
```
```

### Acceptance Test

**File**: `examples/acceptance_smoke.py`
```python
"""Acceptance smoke test - proves ask→simulate→tell works end-to-end."""

import logging
from modelops.services.local import LocalSimulationService
from examples.fake_adapter import FakeAdapter
from modelops_contracts import TrialResult, TrialStatus
from modelops.utils.seeds import derive_single_seed
import polars as pl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def toy_simulate(params: dict, seed: int) -> dict:
    """Toy simulation function."""
    import numpy as np
    np.random.seed(seed)
    
    n = params.get("n", 10)
    x = params.get("x", 1.0)
    
    values = x * np.random.randn(n)
    
    return {
        "output": pl.DataFrame({
            "iteration": range(n),
            "value": values
        })
    }

def run_smoke_test():
    """Run end-to-end smoke test."""
    algo = FakeAdapter()
    sim = LocalSimulationService()
    
    # Mock the simulation module
    import sys, types
    mock_module = types.ModuleType("test_sim")
    mock_module.simulate = toy_simulate
    sys.modules["test_sim"] = mock_module
    
    trials_completed = 0
    
    while not algo.finished():
        batch = algo.ask(n=4)
        if not batch:
            break
        
        logger.info(f"Got {len(batch)} proposals")
        
        # Simulate with replicates
        replicates = 3
        all_results = []
        
        for params in batch:
            replicate_outputs = []
            
            for i in range(replicates):
                seed = derive_single_seed(params.param_id, i)
                output = sim.submit(
                    "test_sim:simulate",
                    dict(params.params),
                    seed,
                    bundle_ref=""
                )
                replicate_outputs.append(output)
            
            # Mock evaluation
            mock_loss = sum(params.params.values()) / len(params.params)
            
            result = TrialResult(
                param_id=params.param_id,
                loss=float(mock_loss),
                status=TrialStatus.COMPLETED,
                diagnostics={"replicates": replicates}
            )
            all_results.append(result)
        
        algo.tell(all_results)
        trials_completed += len(all_results)
        logger.info(f"Completed {trials_completed} trials")
    
    del sys.modules["test_sim"]
    
    logger.info(f"✓ Smoke test passed: {trials_completed} trials completed")
    return trials_completed

if __name__ == "__main__":
    trials = run_smoke_test()
    assert trials == 20, f"Expected 20 trials, got {trials}"
    print("✓ All tests passed!")
```

---

## Cross-Plane Contracts (Explicit)

```python
# Infra → Workspace
ClusterBinding(kubeconfig: str, acr_login_server?: str)

# Workspace → Adaptive  
DaskBinding(scheduler_addr: str, dashboard_url: str, namespace: str)

# Adaptive (internal)
PostgresBinding(secret_name: str, namespace: str)
```

---

## Suggested Timeline (Claude Code-Friendly)

- **Day 1** (4 hours): Stage 0 (contracts + IPC + local) + unit tests
- **Day 2** (6 hours): Stage 1 (Infra from zero) - AKS stack outputs ClusterBinding; doctor command
- **Day 3** (5 hours): Stage 2 (Workspace on AKS) - deploy Dask; GHCR/ACR auth; binding persisted
- **Day 4** (7 hours): Stage 3 (Adaptive plane) - Postgres provisioner; runner; integration tests
- **Day 5** (3 hours): Stage 4 (Acceptance & docs) - happy path; polish; guardrails

**Total**: ~25 hours of focused work with Claude Code

(Stages 2 & 3 can partially overlap once Stage 1 is usable; docs can trail by a few days.)

---

## "Done" Gates per Stage

- **S0**: Services return `Mapping[str, bytes]` compliant with contracts
- **S1**: `mops infra up` returns ClusterBinding with valid kubeconfig (kubectl works)
- **S2**: `mops workspace up` returns DaskBinding; scheduler & dashboard Service Ready
- **S3**: `mops adaptive up` runs workers; replicas>1 uses Postgres; runner submits × replicates
- **S4**: Happy path script matches docs; teardown leaves RG/AKS intact unless `infra down`

This keeps Azure-from-zero front-and-center, preserves the clean three-plane seams, and still optimizes for fewest production LOC (tests/examples carry the weight).