# ModelOps — Azure‑Only MVP
**Design & Architecture Sketch + Detailed Implementation Plan**  
Status: Ready to implement • Scope: MVP • Cloud: **Azure only** • Runtime: **Kubernetes/AKS**

---

NOTE TO CODING AGENTS LIKE CLAUDE CODE: every time a placeholder is put in
code, it must be marked as TODO/PLACEHOLDER.

# ModelOps Documentation

## What we're building (read this first)

**ModelOps** is a small, typed, cloud-native system for **running computational
models in the cloud** with clean seams between simulation execution and
adaptive calibration/search. The MVP targets **Azure AKS** and lets a user go
from zero to a working run with a few CLI commands.

## The happy path (CLIs you'll use)

```bash
# 1) Provision Azure infrastructure from zero (Stack 1)
mops infra up --config examples/providers/azure.yaml

# 2) Deploy Dask workspace using infrastructure from Stack 1 (Stack 2)
mops workspace up --infra-stack modelops-infra-dev

# 3) Run adaptive optimization using Stacks 1 & 2 (Stack 3)
mops adaptive up optuna-config.yaml --run-id exp-001

# 4) Check status and manage resources
mops infra status
mops workspace status --env dev
mops adaptive status exp-001
```

**Three-Stack Architecture:**
- **Stack 1 (Infrastructure)**: Creates Azure resources (RG, AKS, optional ACR)
- **Stack 2 (Workspace)**: Deploys Dask scheduler/workers using StackReference to Stack 1
- **Stack 3 (Adaptive)**: Runs optimization jobs using StackReferences to Stacks 1 & 2

## Two planes, one contract

- **Workspace plane (shared):** a **Dask** scheduler + workers that execute
  simulation code. Exposed via a typed SimulationService binding (address +
  dashboard)

- **Adaptive plane (per run):** **adaptive workers** that drive the
  **Ask/Tell** loop, submit sims to Dask, evaluate losses, and record results.
  Many adapters prefer a **central store**; for MVP we ship an **in-cluster
  Postgres (PVC-backed)** for the Optuna adapter

All inputs/outputs are defined by **../modelops-contracts** — the shared contracts for:
- SimulationService (submit/gather)
- AdaptiveAlgorithm (ask/tell/finished)
- UniqueParameterSet, TrialResult, TrialStatus, diagnostics caps, etc.

## Calabaria, adapters, and generality

- **../calabaria** contains more science-y research-facing code (e.g. the nice
  UX parts) and is the **adapter hub**: it **owns the specs and Ask/Tell
  implementations** for multiple calibration/search algorithms (e.g., Optuna)
  **and the infra they may need**. It's where the science enters
  (researcher-facing UX for calibration / simulation task specification
  generation, it has a model class that's a standard interface to agent-based
  simulation models too).

- An adapter is "just" an Ask/Tell implementation plus a small infra spec. That
  means we can support **any external calibration algorithm** by adding a
  Calabaria adapter and mapping its infra (e.g., DBs, queues) into our
  component provisioners

- The **Postgres** dependency in MVP is **only for the Optuna adapter**. The
  architecture is intentionally **general**: other adapters can bring different
  components later (Redis, object stores, GPU pools, message queues) without
  changing core ModelOps code


## Model bundles: code + data you can move around

**../modelops-bundle** is our packaging layer: a **hybrid** of an **OCI
artifact** (for a file manifest with references to code and small assets) and a
**blob data store** for larger files. It **mirrors a user's local working
directory** so simulation code, config, and datasets can be referenced by a
stable BUNDLE_REF in task JSON and pulled on the cluster.

## Provision-from-zero on Azure (infra + runtime)

- **Infra stack (Pulumi/azure-native):** create or reuse **Resource Group**,
  optional **ACR**, and an **AKS cluster** with a **system pool** and a
  **workload CPU pool** (labeled modelops.io/role=cpu). The stack **exports
  kubeconfig** for downstream steps

- **Runtime stacks (Pulumi/kubernetes):** using that kubeconfig:

  - **Workspace plane:** namespace, Dask scheduler & workers → returns a typed
    **DaskBinding**

  - **Adaptive plane:** in-cluster Postgres (StatefulSet + PVC + Secret +
    Service, when required by the adapter) and the adaptive worker Deployment →
    returns a typed **PostgresBinding** and worker Deployment name

## What runs where (MVP defaults)

- **Images:** built & pushed via **Makefile** (GHCR or ACR). The CLI can call make images or verify tags exist
- **Secrets:** generated at apply-time; never in YAML. For MVP we use **K8s Secrets** (Key Vault CSI is a post-MVP option)
- **Networking:** no external ingress; Services are **ClusterIP**. In-cluster Postgres avoids egress/firewall complexity
- **Durability:** Postgres uses a **PVC** (Azure managed disk). Runs are finite (bring-up → calibrate → tear-down); backups are manual in MVP

## Summary

That's the mental model: **ModelOps** orchestrates Azure + AKS + K8s resources,
**Calabaria** provides science-facing UX code, model class interface to
simulation engines, and adapters (Ask/Tell + infra spec) for calibration
algorithms, and **modelops-contracts** ensures everything speaks the same
language.

## MVP Assumptions

1. **Single user, single workspace** (single tenant/namespace).  
   *Impact:* No multi-tenant RBAC, simple Secrets + one ServiceAccount, simplified networking.

2. **Provision-from-zero on Azure**.  
   *Impact:* `mops workspace up` creates or reuses Azure **Resource Group**, **ACR** (optional), **AKS** cluster and **node pools** if missing; specs remain K8s-only.

3. **Images built & pushed via Makefile**.  
   *Impact:* No CI pipeline required for MVP; CLI can invoke `make images` or verify that images exist (GHCR/ACR).

4. **Finite batch runs** (bring up → calibrate → tear down).  
   *Impact:* Modest durability needs; **in-cluster Postgres with PVC** acceptable; backups are manual at teardown.

5. **In-cluster Postgres** (StatefulSet + PVC) default.  
   *Impact:* No egress complexity, simpler bootstrap; single-replica (PVC is RWO).

6. **CPU-only**.  
   *Impact:* One workload node pool labeled `modelops.io/role=cpu`; no GPU scheduling/drivers in MVP.

7. **Moderate scale** (tens of pods).  
   *Impact:* Grouped-replicate strategy fine; HPA/pgbouncer deferred.

8. **No external HTTP ingress**.  
   *Impact:* All Services are ClusterIP; internal comms only.

9. **Adapters create schema on first use**.  
   *Impact:* No separate DB migration service; initdb scripts create DB/user; adapters create tables.

10. **Security: minimal but sane**.  
    *Impact:* K8s Secrets (MVP), NetworkPolicy, read-only root FS, `automountServiceAccountToken: false` on worker Pods.

---

## MVP Expectations

### What the user provides
- **Provider config** (`~/.modelops/providers/azure.yaml`):
  - `subscription_id`, `tenant_id`, `location`
  - `resource_group` (name; created if missing)
  - `aks`: name, version (required; default pinned by `mops infra doctor`, e.g., `1.29.7`)
  - optional `acr`: `enabled`, `name`
  - optional `auth`: `use_azure_cli: true` (default) or a **service principal** block (`client_id`, `client_secret`)
  - optional `ssh`: mode (`ephemeral`|`path`|`env`), defaults to `ephemeral`
- **Registry credentials** (GHCR PAT or ACR login via `az acr login`)
- **Images or Makefile** to build/push them

### What `mops infra bootstrap` does (one-time setup)
1. **Creates Pulumi state backend**: Separate resource group for state storage
2. **Generates ephemeral SSH key**: Creates public key, stores in stack config, discards private key
```python
# CLI layer (using Pulumi Automation API)
from pulumi import automation as auto
import subprocess
import tempfile
import os

def bootstrap_infra(stack_name: str):
    # Generate ephemeral SSH key once
    with tempfile.NamedTemporaryFile(suffix=".pub", delete=False) as tmp:
        result = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "modelops-ephemeral", "-f", tmp.name[:-4]],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise ValueError(f"Failed to generate SSH key: {result.stderr}")
        with open(tmp.name) as f:
            ssh_pubkey = f.read().strip()
        # Clean up both files - we only keep the public key in config
        os.unlink(tmp.name[:-4])  # private key (never stored)
        os.unlink(tmp.name)       # public key file (value stored in config)
    
    # Store public key as secret in stack config
    stack = auto.select_stack(stack_name, "modelops-infra", program=None)
    stack.set_config("aks:sshPubKey", auto.ConfigValue(value=ssh_pubkey, secret=True))
    print("Ephemeral SSH public key stored in stack config (private key discarded)")
```

### What `mops workspace up` does
1. **Azure bootstrap (Pulumi/azure‑native)**:
   - Create/reuse **Resource Group**
   - Create/reuse **ACR** (optional) and attach to AKS for image pulls
   - Create/reuse **AKS** with:
     - 1× **system** pool (e.g., `Standard_D4s_v5`, min=1, max=3)
     - 1× **workload-cpu** pool (label `modelops.io/role=cpu`, min=0, max=5, autoscaler on)
     - Azure CNI (default), RBAC on
   - Export **kubeconfig** dynamically to drive K8s provisioning
2. **Workspace plane (Pulumi/kubernetes provider)**:
   - Create **Namespace**
   - Deploy **Dask scheduler** + **Dask workers**
   - Return `DaskBinding` (scheduler address + dashboard URL)

### What `mops adaptive up` does
<!-- Problem: Namespace collisions - multiple runs could overwrite each other's resources
     Solution: Generate unique namespace per run with slug (e.g., modelops-run-abc123)
     Why: Ensures isolation between runs and prevents resource conflicts -->
- **Creates per-run namespace** `modelops-<run-slug>` and suffixes all object names with `<run-slug>`
  - CLI generates unique namespace: `modelops-<run-slug>` (e.g., `modelops-run-7x9k2`)
  - All resources suffixed with `-<run-slug>` for uniqueness
  - `adaptive.yaml`'s namespace field is ignored if present (override for isolation)
  - Namespace passed through all provisioning functions
- **Central store (in-cluster Postgres)**:
  - Generate password Secret
  - StatefulSet with PVC + ClusterIP Service
  - Init SQL to create database and runtime user
- **Adaptive workers**:
  - Deployment pinned to CPU nodes
  - `envFrom` PG Secret + `DASK_SCHEDULER_ADDRESS`
  - Grouped-replicate execution loop

### Defaults & scale
<!-- PROBLEM: Using "latest stable" AKS version is unpredictable and can break existing clusters -->
<!-- SOLUTION: Pin to a specific stable version (e.g., 1.29) with explicit upgrade strategy -->
<!-- WHY: AKS auto-upgrades can introduce breaking changes; pinning ensures predictable behavior -->
- **AKS version**: Pin to specific patch version (e.g., `1.29.7`) - no wildcards
  - Exact version stored in Pulumi stack config for reproducibility
  - TODO: Define upgrade testing process before moving versions
  - Current pinned version = `1.29.7`
- **Node pool**: `Standard_D4s_v5` with autoscale (workload: 0–5)
- **Dask**: scheduler 500m/1Gi, workers requests 1CPU/6Gi, limits 2CPU/8Gi
- **Adaptive**: requests 1CPU/2Gi, limits 2CPU/4Gi
- **Postgres**: 10Gi PVC (`managed-csi-premium` StorageClass)

### Cleanup & cost
- `mops adaptive down`: removes workers + PG (PVC retained unless `--purge`)
- `mops workspace down`: removes Dask plane, **keeps** AKS/ACR/RG
- `mops destroy --all`: tears down Dask + Adaptive + **AKS/ACR/RG** with confirmation

### Out of scope (MVP)
GPUs, HPA, managed Postgres, Key Vault CSI, private endpoints, Prometheus/Grafana, multi-tenant RBAC, cross-cloud.

---

## 0) Goals, Non‑Goals, and Ground Rules

### Goals (MVP)

- **Azure‑only**, **provision-from-zero** deployment path (AKS + node pools created by CLI if missing).
- Two execution planes:
  - **Workspace plane**: Dask scheduler + workers for simulations.
  - **Adaptive plane**: generic adaptive workers that speak **Ask/Tell** and may use a **central store**.
- **Central store**: **in-cluster Postgres (PVC-backed)** for MVP, avoiding egress complexity; Optuna adapter supported.
- **Strict types** via Pydantic for specs, bindings, and results.
- **Pulumi Automation API** for both **Azure infra** and **K8s** provisioning; **no secrets in YAML**.
- **Crisp seams** honoring `modelops-contracts` (AdaptiveAlgorithm, SimulationService, TrialResult, etc.).

### Non‑Goals (MVP)
- No local-only mode (no Orbstack)
- No GPU workers
- No cross‑cloud abstraction (Azure only)
- No external ingresses; all Services are ClusterIP
- No managed Postgres (parsed but not implemented in MVP)

### Ground Rules
- Provider details live in `~/.modelops/providers/azure.yaml` (outside specs).
- Secrets generated at apply-time; only in K8s Secrets (MVP).
- Keep resources “boring” and explicit; prefer Pulumi + native Azure & K8s.

---

## 1) High‑Level Architecture

```
+--------------------+            +------------------+
|  Adaptive plane    |            |  Workspace plane |
|  (per run)         |            |  (shared/MVP)    |
|                    |            |                  |
|  +-------------+   |  Ask/Tell  |  +------------+  |
|  | Adaptive    |<---------------|  | Simulation |  |
|  | worker(s)   |   (contracts)  |  | Service    |  |
|  +-------------+   |            |  | (Dask)     |  |
|        |           |            |+---------------+ |
|     (opt) DSN      |                     ^
|        v           |         Bindings (typed, K8s Secrets/env) 
|  +-------------+   |
|  | Postgres    | (in-cluster, MVP; managed post-MVP)
|  +-------------+   |
+--------------------+          
              ^                                
         Bindings (typed, K8s Secrets/env) 
```

---

## 1.5) Abstraction Stack & Typed Bindings

The system follows a strict three-plane architecture with typed bindings flowing between layers:

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INFRA PLANE                                 │
│                  (Azure-only, Pulumi azure-native)                  │
│                                                                      │
│  Creates: Resource Group → ACR (optional) → AKS cluster            │
│           - System node pool (always-on)                           │
│           - Workload node pool (labeled modelops.io/role=cpu)      │
│                                                                      │
│  Command: mops infra up                                            │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │   ClusterBinding     │
                    │  ─────────────────   │
                    │  kubeconfig: str     │
                    │  acr_login_server?   │
                    └──────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        WORKSPACE PLANE                              │
│                  (Kubernetes-only, Pulumi kubernetes)               │
│                                                                      │
│  Creates: Namespace → Dask Scheduler → Dask Workers                │
│           - Uses ClusterBinding.kubeconfig for k8s.Provider        │
│           - No Azure SDK calls                                     │
│                                                                      │
│  Command: mops workspace up                                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │    DaskBinding       │
                    │  ─────────────────   │
                    │  scheduler_addr: str │
                    │  dashboard_url: str  │
                    │  namespace: str      │
                    └──────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        ADAPTIVE PLANE                               │
│                  (Kubernetes-only, Pulumi kubernetes)               │
│                                                                      │
│  Creates: Per-run Namespace → Postgres? → Adaptive Workers         │
│           - Uses DaskBinding.scheduler_addr for connection         │
│           - Provisions PostgresBinding if replicas > 1            │
│                                                                      │
│  Command: mops adaptive up                                         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │    Run Results       │
                    │  ─────────────────   │
                    │  trials_completed    │
                    │  best_loss          │
                    └──────────────────────┘
```

### Typed Binding Definitions

```python
# Infra → Workspace
class ClusterBinding(BaseModel):
    """Minimal contract from infra to workspace plane."""
    kubeconfig: str                      # Secret - the ONLY required field
    acr_login_server: Optional[str]      # If ACR enabled
    cluster_name: Optional[str]          # Metadata only

# Workspace → Adaptive  
class DaskBinding(BaseModel):
    """Connection info for Dask cluster."""
    scheduler_addr: str  # tcp://dask-scheduler.modelops-dev:8786
    dashboard_url: str   # http://dask-scheduler.modelops-dev:8787
    namespace: str       # Where Dask lives

# Adaptive internal (when replicas > 1)
class PostgresBinding(BaseModel):
    """Connection info for central store."""
    secret_name: str     # K8s secret with PG* env vars
    namespace: str       # Where Postgres lives
```

### Command Flow with Stack References

```bash
# Step 1: Create Azure infrastructure from zero
$ mops infra up --config ~/.modelops/providers/azure.yaml
✓ Created Stack: modelops-infra
✓ Created AKS cluster: modelops-aks
→ Outputs saved to stack (query with: pulumi stack output --stack modelops-infra)

# Step 2: Deploy Dask using StackReference to get kubeconfig
$ mops workspace up -f workspace.yaml
✓ Created Stack: modelops-workspace
✓ Referenced: modelops-infra (via StackReference)
✓ Deployed Dask to namespace: modelops-dev
→ Outputs saved to stack (query with: pulumi stack output --stack modelops-workspace)

# Step 3: Run optimization using StackReferences
$ mops adaptive up -f adaptive.yaml --run-id abc123
✓ Created Stack: modelops-adaptive-abc123
✓ Referenced: modelops-workspace (for scheduler_addr)
✓ Referenced: modelops-infra (for kubeconfig)
✓ Started adaptive workers
→ Outputs saved to stack (query with: pulumi stack output --stack modelops-adaptive-abc123)

# Check status anytime
$ pulumi stack output --stack modelops-workspace
scheduler_addr: tcp://dask-scheduler.modelops-dev:8786
dashboard_url: http://dask-scheduler.modelops-dev:8787
namespace: modelops-dev
```

### Why This Architecture Works

1. **Clear Responsibility**: 
   - Infra plane: Azure concerns (VM sizes, AKS versions, role assignments)
   - Workspace plane: Kubernetes Dask deployment only
   - Adaptive plane: Kubernetes optimization workers only

2. **Minimal Contract**: 
   - Workspace needs ONLY `kubeconfig` string from infra
   - Adaptive needs ONLY `scheduler_addr` from workspace
   - No plane knows internal details of planes below

3. **State Isolation**: 
   - Three separate Pulumi stacks: `modelops-infra`, `modelops-workspace`, `modelops-adaptive`
   - Can destroy adaptive without touching workspace
   - Can destroy workspace without touching Azure infra

4. **Swappable Infrastructure**:
   ```python
   # Skip Azure entirely - use existing cluster
   binding = ClusterBinding(
       kubeconfig=Path("~/.kube/config").read_text()
   )
   # workspace up works unchanged!
   ```

5. **Testability**:
   ```python
   # Mock any plane for testing
   mock_cluster = ClusterBinding(kubeconfig=KIND_KUBECONFIG)
   mock_dask = DaskBinding(scheduler_addr="tcp://localhost:8786", ...)
   ```

This means the workspace and adaptive planes are **built on but not entangled with** Azure. Only the infra plane knows about Azure; everything above speaks pure Kubernetes.

---

## 1.6) ComponentResources and Bindings: Clean Separation

The system uses **Pulumi ComponentResources** for infrastructure provisioning and **simple dataclass bindings** as runtime DTOs (Data Transfer Objects). Bindings are NOT persisted - they're just typed containers for passing data within a program.

### ComponentResources: Infrastructure Provisioning

ComponentResources encapsulate cloud provisioning complexity and register outputs to the Pulumi stack:

```python
# Infrastructure provisioning with ComponentResource
class ModelOpsCluster(pulumi.ComponentResource):
    """Encapsulates all Azure infrastructure from zero."""
    def __init__(self, name: str, config: dict, opts=None):
        super().__init__("modelops:infra:cluster", name, None, opts)
        
        # Creates child resources:
        # - ResourceGroup, ACR, AKS with node pools
        # Pulumi manages state, dependencies, rollbacks
        
        # Register outputs to STACK (not custom state!)
        self.register_outputs({
            "kubeconfig": pulumi.Output.secret(kubeconfig),
            "cluster_endpoint": aks.fqdn,
            "acr_login_server": acr.login_server
        })
```

### Bindings: Runtime DTOs Only

Bindings are simple data containers used WITHIN a Pulumi program to pass data
between functions. They are NOT saved anywhere:

```python
# Simple dataclasses for type safety and clarity
from dataclasses import dataclass

@dataclass(frozen=True)
class ClusterBinding:
    """Runtime DTO for cluster connection info."""
    kubeconfig: str  # Just the string value
    
@dataclass(frozen=True)  
class DaskBinding:
    """Runtime DTO for Dask connection info."""
    scheduler_addr: str  # tcp://dask-scheduler.namespace:8786
    dashboard_url: str   # http://dask-scheduler.namespace:8787
    namespace: str

@dataclass(frozen=True)
class PostgresBinding:
    """Runtime DTO for Postgres connection info."""
    secret_name: str  # K8s secret name
    namespace: str
```

### The Flow: Stack Outputs → Bindings → Functions

```python
# Inside a Pulumi program (e.g., workspace up)
def program():
    # 1. Get outputs from another stack
    infra = pulumi.StackReference("modelops-infra")
    kubeconfig = infra.get_output("kubeconfig")
    
    # 2. Create binding DTO (temporary, in-memory only)
    binding = ClusterBinding(kubeconfig=kubeconfig)
    
    # 3. Pass binding to functions that need it
    k8s_provider = create_k8s_provider(binding)
    workspace = create_dask_workspace(k8s_provider)
    
    # 4. Register outputs to THIS stack
    pulumi.export("scheduler_addr", workspace.scheduler_addr)
    pulumi.export("dashboard_url", workspace.dashboard_url)
    # Note: binding is NOT saved - only outputs are!
```

### Why Bindings as DTOs?

1. **Type safety**: Functions know exactly what data they need
2. **Testing**: Easy to create mock bindings for tests
3. **Clarity**: Explicit contracts between functions
4. **No persistence**: Bindings are ephemeral - only stack outputs persist

### Stack Outputs Are The Only State

```python
# Getting connection info - always from stack outputs
def get_dask_connection():
    """Get Dask connection from stack outputs."""
    stack = auto.select_stack("modelops-workspace")
    outputs = stack.outputs()
    
    # Create binding DTO from outputs (for type safety)
    return DaskBinding(
        scheduler_addr=outputs["scheduler_addr"].value,
        dashboard_url=outputs["dashboard_url"].value,
        namespace=outputs["namespace"].value
    )

# Using in adaptive plane
def create_adaptive_workers():
    # Get binding from stack outputs
    dask = get_dask_connection()
    
    # Use binding to configure workers
    env = {
        "DASK_SCHEDULER_ADDRESS": dask.scheduler_addr
    }
    # Deploy workers...
```

### No StateManager, No state.json

The old approach with StateManager is completely replaced:

```python
# OLD (removed):
state = StateManager()
state.save_binding("infra", binding.to_dict())  # ❌ No more!

# NEW (Pulumi-native):
self.register_outputs({
    "kubeconfig": pulumi.Output.secret(kubeconfig),  # ✓ Saved to stack
    "cluster_endpoint": cluster_endpoint             # ✓ Queryable anytime
})
```

### Summary

- **ComponentResources** → Create infrastructure, save outputs to Pulumi stack
- **Stack Outputs** → The ONLY persistent state (managed by Pulumi)
- **Bindings** → Temporary DTOs for passing data within programs
- **StackReferences** → How stacks read each other's outputs
- **No custom state** → Everything flows through Pulumi stacks

---

## 1.7) Stack Architecture - How Information Flows

The system uses **independent Pulumi stacks** that reference each other's outputs. No custom state management needed - Pulumi handles it all.

### The Four-Stack Pattern with Container Registry

```
Stack 1: modelops-infra
├── Creates: Azure resources (RG, AKS)
├── Component: ModelOpsCluster
└── Outputs: kubeconfig, cluster_endpoint, resource_group

Stack 2: modelops-registry  
├── Creates: Container Registry (ACR/ECR/GCR)
├── Component: ContainerRegistry
├── Independent lifecycle (can be shared across environments)
└── Outputs: login_server, registry_name, requires_auth

Stack 3: modelops-workspace  
├── Creates: Dask on Kubernetes
├── Component: DaskWorkspace
├── Depends on: Stack 1's kubeconfig + Stack 2's registry (via StackReferences)
└── Outputs: scheduler_addr, dashboard_url, namespace

Stack 4: modelops-adaptive-{run-id}
├── Creates: Adaptive workers, Postgres
├── Component: AdaptiveRun  
├── Depends on: Stack 3's scheduler_addr (via StackReference)
└── Outputs: run_status, results_location
```

### Why Separate Container Registry?

The Container Registry is intentionally separated from the infrastructure stack
for several engineering reasons:

1. **Single Responsibility Principle**: The registry manages container images,
   while the cluster manages compute. Clear separation of concerns.

2. **Reusability**: A single registry can serve multiple clusters/environments,
   avoiding image duplication and reducing storage costs.

3. **Independent Lifecycle**: Registries often outlive clusters. You can
   destroy/recreate clusters without losing your image history.

4. **Provider Flexibility**: Easy to use external registries (DockerHub, GHCR)
   or switch between providers without touching cluster code.

5. **Cost Optimization**: Share one registry across dev/staging/prod instead of
   one per environment.

6. **Security Boundary**: Registry access control is independent from cluster
   access, allowing fine-grained permissions.

### How StackReferences Connect Stacks

```python
# Stack 1: Infrastructure (standalone)
class ModelOpsCluster(pulumi.ComponentResource):
    def __init__(self, name: str, config: dict):
        super().__init__("modelops:infra:cluster", name, None)
        
        # Creates Azure resources
        aks = azure.containerservice.ManagedCluster(...)
        
        # Get kubeconfig
        creds = azure.containerservice.list_managed_cluster_user_credentials_output(...)
        kubeconfig = creds.kubeconfigs[0].value.apply(lambda b: base64.b64decode(b))
        
        # Register outputs for OTHER stacks to reference
        self.register_outputs({
            "kubeconfig": pulumi.Output.secret(kubeconfig),
            "cluster_name": aks.name,
            "resource_group": rg.name
        })

# Stack 2: Container Registry (independent)
class ContainerRegistry(pulumi.ComponentResource):
    def __init__(self, name: str, config: dict):
        super().__init__("modelops:infra:registry", name, None)
        
        # Creates registry (ACR/ECR/GCR/external)
        if config["provider"] == "azure":
            acr = azure.containerregistry.Registry(...)
            self.login_server = acr.login_server
        
        # Register outputs for workspace stack to reference
        self.register_outputs({
            "login_server": self.login_server,
            "registry_name": self.registry_name
        })

# Stack 3: Workspace (depends on Stack 1 and 2)
class DaskWorkspace(pulumi.ComponentResource):
    def __init__(self, name: str, infra_stack_ref: str):
        super().__init__("modelops:workspace:dask", name, None)
        
        # Reference Stack 1 to get kubeconfig
        infra = pulumi.StackReference(infra_stack_ref)
        kubeconfig = infra.get_output("kubeconfig")  # This is the connection!
        
        # Create K8s provider using kubeconfig from Stack 1
        k8s_provider = pulumi_kubernetes.Provider(
            "k8s-provider",
            kubeconfig=kubeconfig  # Using Stack 1's output
        )
        
        # Now create Dask resources on that cluster
        namespace = k8s.core.v1.Namespace(
            "dask-namespace",
            opts=pulumi.ResourceOptions(provider=k8s_provider)
        )
        
        # Deploy Dask scheduler and workers...
        
        # Register outputs for Stack 3 to reference
        self.register_outputs({
            "scheduler_addr": f"tcp://dask-scheduler.{namespace.metadata.name}:8786",
            "dashboard_url": f"http://dask-scheduler.{namespace.metadata.name}:8787",
            "namespace": namespace.metadata.name
        })

# Stack 3: Adaptive (depends on Stack 2)
class AdaptiveRun(pulumi.ComponentResource):
    def __init__(self, name: str, workspace_stack_ref: str, run_config: dict):
        super().__init__("modelops:adaptive:run", name, None)
        
        # Reference Stack 2 to get Dask connection info
        workspace = pulumi.StackReference(workspace_stack_ref)
        scheduler_addr = workspace.get_output("scheduler_addr")
        namespace = workspace.get_output("namespace")
        
        # Also need kubeconfig - get from infra stack
        infra_stack_ref = workspace_stack_ref.replace("-workspace", "-infra")
        infra = pulumi.StackReference(infra_stack_ref)
        kubeconfig = infra.get_output("kubeconfig")
        
        # Create adaptive workers that connect to Dask
        # ... deployment logic ...
        
        self.register_outputs({
            "run_id": name,
            "status": "running",
            "trials_completed": 0
        })
```

### Where Pulumi Stores Stack State

```bash
# Local file backend (default for MVP)
~/.modelops/pulumi/backend/
├── modelops-infra/
│   └── .pulumi/
│       └── stacks/
│           └── modelops-infra.json     # Stack 1 state & outputs
├── modelops-workspace/
│   └── .pulumi/
│       └── stacks/
│           └── modelops-workspace.json  # Stack 2 state & outputs
└── modelops-adaptive-abc123/
    └── .pulumi/
        └── stacks/
            └── modelops-adaptive-abc123.json  # Stack 3 state

# Each stack's JSON contains:
{
  "outputs": {
    "kubeconfig": {
      "value": "apiVersion: v1\nkind: Config\n...",
      "secret": true  # Encrypted at rest
    },
    "cluster_endpoint": {
      "value": "modelops-aks.eastus2.azure.com"
    }
  },
  "resources": [...],  # All managed resources
  "version": 3         # Stack format version
}
```

### Complete Flow: From Zero to Running

```
Step 1: Create Infrastructure
────────────────────────────
$ mops infra up --config azure.yaml

→ Creates Stack: modelops-infra
→ Runs: ModelOpsCluster component
→ Creates: AKS cluster in Azure
→ Stores outputs in: ~/.modelops/pulumi/backend/modelops-infra/

Step 2: Deploy Workspace
────────────────────────
$ mops workspace up

→ Creates Stack: modelops-workspace  
→ References: modelops-infra via StackReference("file://~/.modelops/pulumi/backend/modelops-infra")
→ Gets: kubeconfig from Stack 1
→ Deploys: Dask on the AKS cluster
→ Stores outputs in: ~/.modelops/pulumi/backend/modelops-workspace/

Step 3: Run Adaptive Job
────────────────────────
$ mops adaptive up --run-id abc123

→ Creates Stack: modelops-adaptive-abc123
→ References: modelops-workspace for scheduler_addr
→ References: modelops-infra for kubeconfig  
→ Deploys: Adaptive workers that connect to Dask
→ Stores outputs in: ~/.modelops/pulumi/backend/modelops-adaptive-abc123/
```

### Querying Stack Information

```bash
# Check infrastructure status
$ pulumi stack output --stack modelops-infra
cluster_endpoint: modelops-aks.eastus2.azure.com
acr_login_server: modelopsacr.azurecr.io

# Get workspace connection info
$ pulumi stack output --stack modelops-workspace
scheduler_addr: tcp://dask-scheduler.modelops:8786
dashboard_url: http://dask-scheduler.modelops:8787
namespace: modelops

# Check run status
$ pulumi stack output --stack modelops-adaptive-abc123
run_id: abc123
status: running
trials_completed: 42

# Get kubeconfig for kubectl
$ pulumi stack output kubeconfig --stack modelops-infra --show-secrets > kubeconfig.yaml
$ export KUBECONFIG=./kubeconfig.yaml
$ kubectl get pods -n modelops

# Port-forward to Dask dashboard
$ kubectl port-forward -n modelops svc/dask-scheduler 8787:8787
```

### Using Automation API in Python

```python
from pulumi import automation as auto

# Get infrastructure info
infra_stack = auto.select_stack("modelops-infra")
outputs = infra_stack.outputs()
cluster_endpoint = outputs["cluster_endpoint"].value

# Get workspace info
workspace_stack = auto.select_stack("modelops-workspace")  
outputs = workspace_stack.outputs()
scheduler = outputs["scheduler_addr"].value
print(f"Dask scheduler at: {scheduler}")

# List all stacks
ws = auto.LocalWorkspace()
stacks = ws.list_stacks()
for stack in stacks:
    print(f"Stack: {stack.name}, Last updated: {stack.last_update}")
```

### Key Benefits of Stack-Based Architecture

1. **No custom state management** - Pulumi handles everything
2. **Encrypted secrets** - kubeconfig automatically encrypted
3. **Version history** - `pulumi stack history` shows all changes
4. **Easy rollback** - `pulumi stack export/import` for disaster recovery
5. **Clean dependencies** - StackReferences make dependencies explicit
6. **Independent lifecycle** - Each stack can be updated/destroyed independently

### Destroying Stacks (Reverse Order)

```bash
# Destroy run (leaves Dask running)
$ mops adaptive down --run-id abc123
→ Destroys Stack: modelops-adaptive-abc123

# Destroy workspace (leaves cluster running)  
$ mops workspace down
→ Destroys Stack: modelops-workspace

# Destroy infrastructure (removes everything)
$ mops infra down
→ Destroys Stack: modelops-infra
```

---

## 2) Implementation Stages

### Stage 1: Local/Dask Runtime (Start Here)
**Goal**: Get simulation services working locally without cloud complexity
- **Scope**: LocalSimulationService, DaskSimulationService
- **CLI**: Minimal - just `mops version` and `mops config`
- **Testing**: IPC roundtrip, local service tests
- **No cloud dependencies**: Can run entirely on laptop

### Stage 2: Azure Infrastructure (MVP Focus)
**Goal**: Deploy Dask clusters and Postgres on AKS
- **Scope**: Azure provider, workspace provisioning, component provisioners
- **CLI**: Add `mops workspace up/down` and `mops adaptive up/down`
- **Components**: PostgresSpec → provision_postgres() → PostgresBinding
- **Single provider**: Azure only, direct implementation (no registry)

### Stage 3: Multi-Component & Multi-Cloud (Post-MVP)
**Goal**: Support multiple components and cloud providers
- **Components**: Add Redis, ObjectStore, Queue
- **Providers**: Add AWS (EKS), GCP (GKE)
- **Registry pattern**: Introduce when genuinely needed
- **Advanced features**: Managed services, GPU pools, autoscaling

> **Key principle**: Each stage should be independently useful. Stage 1 gives you local testing, Stage 2 gives you cloud deployment, Stage 3 gives you flexibility.

## 3) Images & Build Process

### Makefile (MVP)
```makefile
# Variables
REGISTRY ?= ghcr.io/megacorp
VERSION ?= latest
IMAGES := dask-scheduler dask-worker adaptive-worker

.PHONY: build push images

build:
	@for img in $(IMAGES); do \
		docker build -t $(REGISTRY)/$$img:$(VERSION) -f docker/$$img.Dockerfile .; \
	done

push:
	@for img in $(IMAGES); do \
		docker push $(REGISTRY)/$$img:$(VERSION); \
	done

images: build push
```

**CLI integration**
```bash
# Reuse Makefile
mops workspace up --build   # calls `make images` if configured

# Or pre-push images
mops workspace up           # fails fast if images missing
```

---

## 3) Specs & Types

### 3.1 Workspace spec — `workspace.yaml`
```yaml
version: 1
namespace: modelops  # Used for workspace plane (stable)

dask:
  scheduler:
    image: ghcr.io/you/dask-scheduler:mvp
    node_selector: { modelops.io/role: cpu }
    resources: { cpu: "500m", memory: "1Gi" }

  workers:
    image: ghcr.io/you/dask-worker:mvp
    replicas: 6
    node_selector: { modelops.io/role: cpu }
    resources:
      requests: { cpu: "1", memory: "6Gi" }
      limits:   { cpu: "2", memory: "8Gi" }
```

### 3.2 Adaptive spec — `adaptive.yaml`
```yaml
version: 1
# namespace field is optional and ignored - CLI generates modelops-<run-slug>

central_store:
  kind: postgres
  mode: in-cluster
  persistence:
    enabled: true
    size: 10Gi
    storageClass: managed-csi-premium  # Correct AKS StorageClass name
  database: optuna
  user: optuna_user

workers:
  image: ghcr.io/you/adaptive-worker:mvp
  replicas: 4
  node_selector: { modelops.io/role: cpu }
  resources:
    requests: { cpu: "1", memory: "2Gi" }
    limits:   { cpu: "2", memory: "4Gi" }

workspace_ref:
  namespace: modelops  # Reference to stable workspace namespace
```

### 3.3 Directory Structure and Implementation Stages

**Stage 1: Runtime Services (Local/Dask) - Start Here**
```
modelops/
├── pyproject.toml                   # console_scripts: mops=modelops.cli.app:main
├── modelops/
│  ├── __init__.py                   # exports versions & key classes
│  ├── __main__.py                   # python -m modelops
│  │
│  ├── cli/                          # Minimal CLI for Stage 1
│  │  ├── __init__.py
│  │  ├── app.py                     # Typer root: version, config
│  │  └── config_cmd.py              # 'mops config' (paths/status)
│  │
│  ├── services/                     # **Stage 1 focus: runtime only**
│  │  ├── __init__.py                # export LocalSimulationService, DaskSimulationService
│  │  ├── ipc.py                     # Arrow IPC helpers (bytes in/out)
│  │  ├── local.py                   # LocalSimulationService (wraps fn_ref, calls ipc.to_ipc)
│  │  └── dask.py                    # DaskSimulationService + _worker_run_sim uses ipc.to_ipc
│  │
│  ├── state/                        # DEPRECATED - Use Pulumi stack outputs
│  │  ├── __init__.py                # (Will be removed in refactor)
│  │  ├── manager.py                 # (Legacy - replaced by stack outputs)
│  │  └── models.py                  # (Legacy - replaced by stack outputs)
│  │
│  └── versions.py                   # DASK/Python pin + doctor helper
│
├── examples/
│  ├── sim/
│  │  └── toy.py                     # returns dict[str, DataFrame]; service converts to IPC
│  └── simulation_task.json          # example payload for submit (local-only use)
│
└── tests/
   ├── unit/
   │  ├── test_contracts_usage.py    # UniqueParameterSet, TrialResult invariants
   │  ├── test_ipc_roundtrip.py      # pandas/polars -> ipc bytes -> back
   │  └── test_local_service.py      # submit/gather returns dict[str, bytes]
   └── conftest.py
```

**Stage 2: Add Azure Infrastructure & Components**
```
modelops/
├── modelops/
│  ├── cli/                          # Extend with workspace commands
│  │  ├── workspace_cmd.py           # 'mops workspace up/down/ls'
│  │  └── adaptive_cmd.py            # 'mops adaptive up/down'
│  │
│  ├── components/                   # Component specs and provisioners
│  │  ├── __init__.py
│  │  ├── specs.py                   # PostgresSpec (only Postgres for MVP)
│  │  ├── bindings.py                # PostgresBinding
│  │  └── provisioners/
│  │      └── postgres.py            # Direct provisioner, no registry yet
│  │
│  ├── infra/                        # Cloud infrastructure
│  │  ├── __init__.py
│  │  ├── providers/
│  │  │  └── azure.py                # Azure/AKS provisioning with Pulumi
│  │  └── workspace.py               # Workspace orchestration
│  │
│  └── runners/                      # Adaptive plane runners
│     └── adaptive_worker_runner.py  # Ask/Tell loop implementation
│
├── examples/
│  ├── workspace.yaml                # Dask cluster specification
│  ├── adaptive.yaml                 # Adaptive plane specification
│  └── providers/
│     └── azure.yaml                 # Azure provider config example
```

**Stage 3: Post-MVP Extensions**
```
modelops/
├── modelops/
│  ├── components/
│  │  ├── specs.py                   # + RedisSpec, ObjectStoreSpec, QueueSpec
│  │  ├── registry.py                # Component registry (when >1 component)
│  │  └── provisioners/
│  │      ├── postgres.py
│  │      ├── redis.py               # New component provisioners
│  │      └── objectstore.py
│  │
│  └── infra/
│     └── providers/
│        ├── azure.py
│        ├── aws.py                  # Multi-cloud support
│        └── gcp.py
```

### 3.4 Component Architecture (Conceptual Model)

**Component → Provisioner → Binding Pattern:**
- **Component**: What you need (e.g., PostgresSpec) - cloud-agnostic requirement
- **Provisioner**: How to create it (K8s implementation) - platform-specific
- **Binding**: How to connect to it (PostgresBinding) - runtime configuration

**MVP Implementation (Direct, No Registry):**
```python
# modelops/components/specs.py
from pydantic import BaseModel
from typing import Literal

class PostgresSpec(BaseModel):
    kind: Literal["postgres"] = "postgres"
    mode: Literal["in-cluster"] = "in-cluster"  # Only one mode for MVP
    database: str = "optuna"
    user: str = "optuna_user"
    persistence_size: str = "10Gi"
    storage_class: str = "managed-csi-premium"

# modelops/components/provisioners/postgres.py
def provision_postgres(k8s_provider, spec: PostgresSpec, namespace: str) -> PostgresBinding:
    """Direct provisioner implementation for MVP - no registry pattern yet."""
    # Create StatefulSet, Service, Secrets, etc.
    # Return PostgresBinding with connection info
    pass
```

**Post-MVP (With Registry - Deferred):**
```python
# Only add registry when you have 2+ components and need the flexibility
# modelops/components/registry.py (FUTURE)
REGISTRY: Dict[str, ProvisionerFn] = {}

def register(kind: str, fn: ProvisionerFn):
    REGISTRY[kind] = fn

def provision(kind: str, provider_ctx, spec, namespace: str):
    return REGISTRY[kind](provider_ctx, spec, namespace)
```

> **Why defer the registry?** YAGNI principle - with only Postgres in MVP, a registry adds complexity without benefit. Add it when you genuinely need multiple components or provider-specific implementations.

### 3.5 Pydantic types (selected)
```python
# modelops/components/specs.py (Component specifications)
from pydantic import BaseModel, Field
from typing import Optional, Dict, Literal

class Quantity(BaseModel):
    cpu: Optional[str] = None
    memory: Optional[str] = None

class ContainerResources(BaseModel):
    requests: Quantity = Field(default_factory=Quantity)
    limits: Quantity = Field(default_factory=Quantity)

class WorkerGroupSpec(BaseModel):
    image: str
    replicas: int = 1
    node_selector: Dict[str, str] = Field(default_factory=dict)
    resources: ContainerResources = Field(default_factory=ContainerResources)

class DaskSpec(BaseModel):
    scheduler: WorkerGroupSpec
    workers: WorkerGroupSpec

class WorkspaceSpec(BaseModel):
    version: int
    namespace: str
    dask: DaskSpec

class PostgresPersistence(BaseModel):
    # Problem: 'managed-premium' doesn't exist in AKS, causes StatefulSet to hang
    # Solution: Use 'managed-csi-premium' which is the correct AKS StorageClass name
    # Why: AKS uses CSI drivers, old in-tree drivers are deprecated
    enabled: bool = True
    size: str = "10Gi"
    storageClass: str = "managed-csi-premium"  # Correct AKS StorageClass

class PostgresSpec(BaseModel):
    kind: Literal["postgres"] = "postgres"
    mode: Literal["in-cluster","managed","external"] = "in-cluster"
    persistence: Optional[PostgresPersistence] = PostgresPersistence()
    database: str = "optuna"
    user: str = "optuna_user"

class WorkspaceRef(BaseModel):
    namespace: str

class AdaptiveSpec(BaseModel):
    version: int
    namespace: Optional[str] = None  # Auto-generated if not provided
    central_store: Optional[PostgresSpec] = None
    workers: WorkerGroupSpec
    workspace_ref: WorkspaceRef
```

---

## 4) Bindings & Runners

### 4.1 Typed bindings
```python
# modelops/components/bindings.py (Runtime bindings)
from pydantic import BaseModel, HttpUrl
from typing import Optional

class DaskBinding(BaseModel):
    scheduler_addr: str
    dashboard_url: Optional[HttpUrl] = None

class PostgresBinding(BaseModel):
    secret_name: str
    host: str
    port: int
    database: str
    user: str
```

### 4.2 Central store runtime
```python
# central_store_runtime.py (selected)
import os
from typing import Protocol, Optional, List

class CentralStore(Protocol):
    @classmethod
    def from_env(cls) -> Optional["CentralStore"]: ...
    def dsn(self) -> str: ...

class PostgresStore:
    @classmethod
    def from_env(cls) -> Optional["PostgresStore"]:
        need = ("PGHOST","PGUSER","PGPASSWORD","PGDATABASE")
        return cls() if all(os.getenv(k) for k in need) else None
    def dsn(self) -> str:
        host = os.environ["PGHOST"]; user = os.environ["PGUSER"]
        pw   = os.environ["PGPASSWORD"]; db = os.environ["PGDATABASE"]
        port = os.getenv("PGPORT","5432")
        sslm = os.getenv("PGSSLMODE","disable")  # in-cluster default
        return f"postgresql://{user}:{pw}@{host}:{port}/{db}?sslmode={sslm}"

REGISTRY: List[type[CentralStore]] = [PostgresStore]

def detect_central_store() -> Optional[CentralStore]:
    for cls in REGISTRY:
        cs = cls.from_env()
        if cs: return cs
    return None
```

### 4.3 Runner (grouped replicates)
```python
# runners/adaptive_worker_runner.py (selected)
import os, time, logging
from modelops_contracts import AdaptiveAlgorithm, TrialResult
from central_store_runtime import detect_central_store
from sim.dask import DaskSimulationService

logger = logging.getLogger(__name__)

def load_algorithm_adapter_from_entrypoint():
    """Load the algorithm adapter from entrypoints or config.
    
    TODO/PLACEHOLDER: Implement proper entrypoint discovery
    For now, returns a hardcoded Optuna adapter example
    """
    # Future: use pkg_resources or importlib.metadata to load from entrypoint group
    # Example: entry_points = {"modelops.adapters": ["optuna = calabaria.optuna_adapter:OptunaAdapter"]}
    from calabaria.optuna_adapter import OptunaAdapter  # PLACEHOLDER import
    return OptunaAdapter()

def evaluate_with_calabaria(batch, simouts):
    """Bridge simulation outputs to TrialResult using Calabaria evaluation.
    
    TODO/PLACEHOLDER: Implement proper evaluation bridge
    Currently returns mock results for testing
    """
    # Future: use Calabaria's Target/AlignmentStrategy/EvaluationStrategy
    # from calabaria.evaluator import evaluate
    # return evaluate(batch, simouts)
    
    # Mock implementation for MVP testing
    from modelops_contracts import TrialResult, TrialStatus
    results = []
    for params, sim_out in zip(batch, simouts):
        # TODO: Real evaluation logic here
        results.append(TrialResult(
            trial_id=params.param_id,
            status=TrialStatus.COMPLETED,
            loss=0.5,  # PLACEHOLDER loss value
            diagnostics={}
        ))
    return results

def main():
    sim = DaskSimulationService(os.environ["DASK_SCHEDULER_ADDRESS"])
    algo: AdaptiveAlgorithm = load_algorithm_adapter_from_entrypoint()

    # Basic retry loop for Postgres connection (MVP)
    store = None
    for attempt in range(6):  # 30 seconds total
        store = detect_central_store()
        if store:
            try:
                # Verify connection works
                test_dsn = store.dsn()
                logger.info(f"Central store connected on attempt {attempt + 1}")
                break
            except Exception as e:
                logger.warning(f"Central store connection attempt {attempt + 1} failed: {e}")
                if attempt < 5:
                    time.sleep(5)
        else:
            logger.info("No central store configured, proceeding without it")
            break
    
    if store:
        os.environ["ADAPTIVE_STORAGE_DSN"] = store.dsn()  # ephemeral; do not log

    GROUP_SIZE = int(os.getenv("REPLICATES_PER_PARAM", "10"))
    while not algo.finished():
        batch = algo.ask(n=4)
        if not batch: continue
        # Generate deterministic seeds for each replicate
        import hashlib
        futures = []
        for p in batch:
            for i in range(GROUP_SIZE):
                # Derive seed from param_id and replicate index
                seed_bytes = hashlib.blake2b(
                    f"{p.param_id}:{i}".encode(), digest_size=8
                ).digest()
                seed = int.from_bytes(seed_bytes, 'little') & ((1 << 64) - 1)
                futures.append(
                    sim.submit("pkg.mod:simulate", p.params, seed,
                               bundle_ref=os.environ.get("BUNDLE_REF",""))
                )
        simouts = sim.gather(futures)
        results: list[TrialResult] = evaluate_with_calabaria(batch, simouts)
        algo.tell(results)
```

---

## 5) Provisioning Pattern **(+ Azure from zero)**

### 5.0 Azure Infrastructure Provisioning (from zero)

**Overview.** The CLI uses Pulumi (azure‑native) to create or reuse:
- **Resource Group** (e.g., `modelops-rg`)
- **Azure Container Registry (ACR)** *(optional in MVP)*
- **AKS cluster** with:
  - **system** node pool (always-on)
  - **workload-cpu** node pool labeled `modelops.io/role=cpu` (autoscale 0–5)
- It then **exports kubeconfig** (as a Pulumi output) to configure the Pulumi **kubernetes** provider, which applies the workspace/adaptive K8s resources.

<!-- PROBLEM: Pulumi state stored in same resource group as workloads can be accidentally deleted -->
<!-- SOLUTION: Require separate resource group for Pulumi state backend with strict access controls -->
<!-- WHY: State loss means losing all infrastructure references; separate RG prevents accidental deletion -->

**Pulumi State Backend Requirements:**
- **Separate Resource Group**: Create `modelops-pulumi-state-rg` distinct from workload RG
  - TODO: Add terraform or script to bootstrap this RG with proper RBAC
  - PLACEHOLDER: Storage account name = `modelopspulumistate<random>`
- **Backend Configuration**: Use `azblob://` with container per environment
- **Access Control**: Service Principal needs Storage Blob Data Contributor on state RG only
- **Lock Policy**: Enable resource locks on state RG to prevent deletion

**Provider config (`~/.modelops/providers/azure.yaml`).**
```yaml
subscription_id: "00000000-0000-0000-0000-000000000000"
tenant_id: "11111111-1111-1111-1111-111111111111"
location: "eastus2"
resource_group: "modelops-rg"  # Base name - will become modelops-rg-{username}

# Per-User Resource Groups for MVP
# - Each user gets own RG: modelops-rg-{username}
# - Prevents conflicts on shared subscriptions
# - Username from environment (USER/USERNAME) or config
# - Example: modelops-rg-alice, modelops-rg-bob
# username: alice  # Optional - defaults to $USER env var

auth:
  use_azure_cli: true  # or false with service_principal below
  # service_principal:
  #   client_id: "..."
  #   client_secret: "..."

aks:
  name: "modelops-aks"
  version: "1.29.7"    # Required; pinned version (use mops infra doctor for recommendations)
  workload_pool:
    vm_size: "Standard_D4s_v5"
    min: 0
    max: 5

acr:
  enabled: false
  name: "modelopsacr"  # required if enabled

ssh:
  mode: ephemeral      # Default; generates and discards private key
  # OR for debugging with Bastion:
  # mode: path
  # pubkey_path: "~/.ssh/id_ed25519.pub"
  # OR for CI/CD:
  # mode: env
  # pubkey_env: "MODELOPS_SSH_PUBKEY"
```

**Pulumi stacks & state.**
- Three-stack architecture:
  - Stack 1: `modelops-infra-<env>` (Resource Group, ACR, AKS cluster)
  - Stack 2: `modelops-workspace-<env>` (Dask scheduler/workers, references Stack 1)
  - Stack 3: `modelops-adaptive-<run_id>` (Optimization runs, references Stacks 1 & 2)
- State backend: Local file backend at `~/.modelops/pulumi/backend/{azure|workspace|adaptive}/`
- Cross-stack communication: Pulumi StackReferences for passing kubeconfig, endpoints, etc.
- **IMPORTANT**: All state managed through Pulumi outputs - no custom StateManager or state.json
- Per-user resource groups: `modelops-rg-<username>` for multi-user isolation on shared subscriptions
- Resource protection: Resource groups use `protect=True` and `retain_on_delete=True` flags

**Pulumi program (infra, sketch).**
```python
# infra/azure.py (sketch)
import os
import base64
import pulumi
import pulumi_azure_native as azure
from pulumi import Config, ResourceOptions

# Standard tags for governance and cost tracking
tags = {
    "project": "modelops",
    "env": cfg.env or "dev",
    "owner": cfg.owner or "team",
    "costCenter": cfg.cost_center or "engineering",
    "managedBy": "pulumi"
}

rg = azure.resources.ResourceGroup(
    "rg", 
    resource_group_name=cfg.rg, 
    location=cfg.location,
    tags=tags
)

acr = None
if cfg.acr_enabled:
    acr = azure.containerregistry.Registry(
        "acr",
        resource_group_name=rg.name,
        sku=azure.containerregistry.SkuArgs(name="Basic"),
        admin_user_enabled=False,
        registry_name=cfg.acr_name,
        location=rg.location,
        tags=tags
    )

aks = azure.containerservice.ManagedCluster(
    "aks",
    resource_group_name=rg.name,
    location=rg.location,
    tags=tags,
    dns_prefix=f"{cfg.name}-dns",
    kubernetes_version=cfg.version or None,
    identity=azure.containerservice.ManagedClusterIdentityArgs(type="SystemAssigned"),
    agent_pool_profiles=[
        azure.containerservice.ManagedClusterAgentPoolProfileArgs(
            name="system",
            mode="System",
            vm_size="Standard_D4s_v5",
            count=1,
            os_type="Linux",
            type="VirtualMachineScaleSets",
            # Best practice: Taint system pool to ensure only critical addons run here
            # This prevents app pods from accidentally landing on system nodes
            # node_taints=["CriticalAddonsOnly=true:NoSchedule"],  # Uncomment for production
        ),
        azure.containerservice.ManagedClusterAgentPoolProfileArgs(
            name="workcpu",
            mode="User",
            vm_size=cfg.vm_size,
            min_count=cfg.min_count,
            max_count=cfg.max_count,
            enable_auto_scaling=True,
            os_type="Linux",
            type="VirtualMachineScaleSets",
            node_labels={"modelops.io/role": "cpu"},
        ),
    ],
    # SSH key handling (AKS requirement)
    # AKS requires a Linux ssh.publicKeys[0].keyData at create time.
    # IMPORTANT: Key generation happens in CLI layer (once) to ensure stability
    # The public key is stored as Pulumi stack config and reused across runs
    # This prevents cluster replacement on every `pulumi up`
    
    ssh_mode = cfg.get("ssh", {}).get("mode", "ephemeral")
    
    if ssh_mode == "ephemeral":
        # Default: Use ephemeral key from stack config (generated once by CLI)
        # CLI generates key, stores public in config, discards private
        # This ensures the same public key is used for the cluster lifetime
        config = pulumi.Config("aks")
        ssh_pubkey = config.require_secret("sshPubKey")
        pulumi.log.info("Using ephemeral SSH key from stack config (no private key stored)")
    
    elif ssh_mode == "path":
        # Opt-in: Use existing key from path
        ssh_pubkey_path = cfg.get("ssh", {}).get("pubkey_path", "~/.ssh/id_ed25519.pub")
        ssh_pubkey_path = os.path.expanduser(ssh_pubkey_path)
        if not os.path.exists(ssh_pubkey_path):
            raise ValueError(f"SSH public key not found at {ssh_pubkey_path}")
        with open(ssh_pubkey_path) as f:
            ssh_pubkey = f.read().strip()
        pulumi.log.info(f"Using SSH public key from {ssh_pubkey_path}")
    
    elif ssh_mode == "env":
        # Opt-in: Use key from environment variable
        ssh_pubkey_env = cfg.get("ssh", {}).get("pubkey_env", "MODELOPS_SSH_PUBKEY")
        ssh_pubkey = os.environ.get(ssh_pubkey_env)
        if not ssh_pubkey:
            raise ValueError(f"SSH public key not found in environment variable {ssh_pubkey_env}")
        pulumi.log.info(f"Using SSH public key from ${ssh_pubkey_env}")
    
    else:
        raise ValueError(f"Invalid SSH mode: {ssh_mode}. Use 'ephemeral', 'path', or 'env'")
    
    linux_profile=azure.containerservice.ContainerServiceLinuxProfileArgs(
        admin_username="modelops",
        ssh=azure.containerservice.ContainerServiceSshConfigurationArgs(
            public_keys=[azure.containerservice.ContainerServiceSshPublicKeyArgs(key_data=ssh_pubkey)]
        ),
    ),
    network_profile=azure.containerservice.ContainerServiceNetworkProfileArgs(
        network_plugin="azure",
        outbound_type="loadBalancer",
    ),
    addon_profiles=None,
)

# If ACR is enabled, grant AKS kubelet identity AcrPull permission
if acr:
    # Get the kubelet identity principal ID
    kubelet_identity = aks.identity_profile.apply(
        lambda profile: profile["kubeletidentity"]["object_id"] if profile else None
    )
    
    # Create role assignment for AcrPull
    acr_pull_role = azure.authorization.RoleAssignment(
        "aks-acr-pull",
        principal_id=kubelet_identity,
        principal_type="ServicePrincipal",
        role_definition_id=f"/subscriptions/{cfg.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d",  # TODO/PLACEHOLDER: AcrPull role ID
        scope=acr.id,
    )

# Export kubeconfig
creds = azure.containerservice.list_managed_cluster_user_credentials_output(
    resource_group_name=rg.name, resource_name=aks.name
)
# CRITICAL: kubeconfig is base64-encoded, must decode properly
import base64
kubeconfig = creds.kubeconfigs[0].value.apply(lambda b64: base64.b64decode(b64).decode("utf-8"))
pulumi.export("kubeconfig", pulumi.secret(kubeconfig))  # Mark as secret for secure storage
if acr:
    pulumi.export("acr_login_server", acr.login_server)
```

The CLI (Pulumi Automation API) reads `kubeconfig` from this stack output,
instantiates a **kubernetes** provider, and immediately applies the
**workspace** program (Namespace, Dask scheduler/workers).

**CLI flow (high level).**
```text
mops workspace up
 ├─ select/create infra stack → pulumi up (RG/ACR/AKS) → kubeconfig out
 ├─ select/create workspace stack (k8s provider uses kubeconfig) → pulumi up (Namespace + Dask)
 └─ print DaskBinding (scheduler address, dashboard URL)
```

> **Why from-zero?** Ensures a reproducible, auditably-declared Azure footprint. Users get a working cluster with the right labels and autoscaling without manual portal steps.

### 5.1 Component provisioners (Postgres in-cluster)
```python
# modelops/components/provisioners/postgres.py (MVP - direct implementation)
import os
import base64
import pulumi
import pulumi_kubernetes as k8s
from pulumi_random import RandomPassword
from pulumi import ResourceOptions, ResourceError
from typing import TypedDict, Optional
from specs import AdaptiveSpec, PostgresSpec
from bindings import PostgresBinding

class AzureCtx(TypedDict):
    subscription_id: str
    tenant_id: str  # Added for consistency
    resource_group: str
    location: str
    aks_cluster: str
    kubeconfig: str  # Changed from kubeconfig_context for consistency

def provision_postgres(spec: AdaptiveSpec, azure_ctx: AzureCtx) -> PostgresBinding:
    cs = spec.central_store
    if not isinstance(cs, PostgresSpec):
        raise ValueError("central_store is missing or not PostgresSpec")
    
    if cs.mode != "in-cluster":
        raise NotImplementedError(f"Postgres mode '{cs.mode}' not supported in MVP")
    
    kubeconfig = azure_ctx["kubeconfig"]   # <- injected by the orchestrator
    k8s_provider = k8s.Provider("aks", kubeconfig=kubeconfig)
    
    # Generate secure passwords for admin and runtime user
    admin_password = pulumi_random.RandomPassword(
        f"{spec.namespace}-pg-admin-password",
        length=24,
        special=True,
    )
    
    runtime_password = pulumi_random.RandomPassword(
        f"{spec.namespace}-pg-runtime-password",
        length=24,
        special=True,
    )

    # Secret for Postgres container initialization (admin)
    postgres_init_secret = k8s.core.v1.Secret(
        f"{spec.namespace}-postgres-init",
        metadata={"name": f"{spec.namespace}-postgres-init", "namespace": spec.namespace},
        string_data={
            "POSTGRES_PASSWORD": admin_password.result,
            "POSTGRES_USER": "postgres",  # admin user
            "POSTGRES_DB": "postgres",     # default db
        },
        opts=pulumi.ResourceOptions(provider=k8s_provider),
    )

    # Secret for runtime clients (workers) - NOT used by DB pod
    pg_client_secret = k8s.core.v1.Secret(
        f"{spec.namespace}-pg-env",
        metadata={"name": f"{spec.namespace}-pg-env", "namespace": spec.namespace},
        string_data={
            "PGHOST": "postgres",
            "PGPORT": "5432",
            "PGDATABASE": cs.database,
            "PGUSER": cs.user,
            "PGPASSWORD": runtime_password.result,
            "PGSSLMODE": "disable",  # in-cluster default
        },
        opts=pulumi.ResourceOptions(provider=k8s_provider),
    )
    
    # Problem: Passwords in ConfigMap are visible in plain text and can leak in logs
    # Solution: Use Secret for SQL + Job pattern for initialization
    # Why: Secrets are encrypted at rest, Jobs don't expose passwords in pod spec
    
    # Secret with init SQL script (password is marked as secret)
    init_sql_secret = k8s.core.v1.Secret(
        f"{spec.namespace}-postgres-init-sql",
        metadata={"name": f"{spec.namespace}-postgres-init-sql", "namespace": spec.namespace},
        string_data={
            "init.sql": pulumi.Output.concat(
                "DO $$\n",
                "BEGIN\n",
                "  IF NOT EXISTS (SELECT FROM pg_user WHERE usename = '", cs.user, "') THEN\n",
                "    CREATE USER ", cs.user, " WITH PASSWORD '", pulumi.secret(runtime_password.result), "';\n",
                "  END IF;\n",
                "  IF NOT EXISTS (SELECT FROM pg_database WHERE datname = '", cs.database, "') THEN\n",
                "    CREATE DATABASE ", cs.database, ";\n",
                "  END IF;\n",
                "  GRANT ALL PRIVILEGES ON DATABASE ", cs.database, " TO ", cs.user, ";\n",
                "END $$;\n"
            )
        },
        opts=pulumi.ResourceOptions(provider=k8s_provider),
    )
    
    # Validate StorageClass exists (apply-time check) - MUST BE BEFORE STATEFULSET
    # This ensures the PVC won't hang waiting for a non-existent StorageClass
    def validate_storage_class(sc):
        if sc is None:
            raise pulumi.ResourceError(
                f"StorageClass '{cs.persistence.storageClass}' not found. "
                f"For AKS, use 'managed-csi-premium' or 'azurefile-csi-premium'"
            )
        return sc
    
    storage_class_check = k8s.storage.v1.StorageClass.get(
        cs.persistence.storageClass,
        cs.persistence.storageClass,
        opts=pulumi.ResourceOptions(provider=k8s_provider)
    ).apply(validate_storage_class)
    
    # StatefulSet with volumeClaimTemplate
    postgres_sts = k8s.apps.v1.StatefulSet(
        "postgres",
        metadata={"name": "postgres", "namespace": spec.namespace},
        spec={
            "serviceName": "postgres",
            "replicas": 1,
            "selector": {"matchLabels": {"app": "postgres"}},
            "template": {
                "metadata": {"labels": {"app": "postgres"}},
                "spec": {
                    "containers": [{
                        "name": "postgres",
                        "image": "postgres:15-alpine",
                        "ports": [{"containerPort": 5432}],
                        "envFrom": [{"secretRef": {"name": postgres_init_secret.metadata["name"]}}],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 999,  # postgres user
                            "fsGroup": 999,     # postgres group
                            "allowPrivilegeEscalation": False,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "volumeMounts": [
                            {"name": "data", "mountPath": "/var/lib/postgresql/data"}
                            # Init now handled by separate Job, not initdb.d
                        ],
                        "livenessProbe": {
                            "exec": {"command": ["pg_isready", "-U", "postgres"]},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                        },
                    }],
                    # No init volume needed - Job handles initialization
                },
            },
            "volumeClaimTemplates": [{
                "metadata": {"name": "data"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": cs.persistence.storageClass,
                    "resources": {"requests": {"storage": cs.persistence.size}},
                },
            }],
        },
        opts=pulumi.ResourceOptions(
            provider=k8s_provider,
            depends_on=[storage_class_check]  # Ensure StorageClass exists before creating PVC
        ),
    )
    
    # ClusterIP Service
    postgres_svc = k8s.core.v1.Service(
        "postgres",
        metadata={"name": "postgres", "namespace": spec.namespace},
        spec={
            "selector": {"app": "postgres"},
            "ports": [{"port": 5432, "targetPort": 5432}],
            "type": "ClusterIP",
        },
        opts=pulumi.ResourceOptions(provider=k8s_provider),
    )
    
    # Init Job that runs AFTER StatefulSet and Service are ready (idempotent DDL)
    # CRITICAL: Job must be created AFTER StatefulSet/Service to avoid undefined references
    postgres_init_job = k8s.batch.v1.Job(
        f"{spec.namespace}-postgres-init",
        metadata={"name": f"{spec.namespace}-postgres-init", "namespace": spec.namespace},
        spec={
            "backoffLimit": 3,
            "template": {
                "spec": {
                    "restartPolicy": "OnFailure",
                    "containers": [{
                        "name": "init",
                        "image": "postgres:15-alpine",
                        "command": ["psql"],
                        "args": [
                            "-h", "postgres",
                            "-U", "postgres",
                            "-f", "/sql/init.sql"
                        ],
                        "env": [
                            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {
                                "name": postgres_init_secret.metadata["name"],
                                "key": "POSTGRES_PASSWORD"
                            }}}
                        ],
                        "volumeMounts": [
                            {"name": "init-sql", "mountPath": "/sql"}
                        ]
                    }],
                    "volumes": [
                        {"name": "init-sql", "secret": {"secretName": init_sql_secret.metadata["name"]}}
                    ]
                }
            }
        },
        opts=pulumi.ResourceOptions(
            provider=k8s_provider,
            depends_on=[postgres_sts, postgres_svc, init_sql_secret]
        ),
    )

    return PostgresBinding(
        secret_name=pg_client_secret.metadata.apply(lambda m: m["name"]),
        host="postgres",
        port=5432,
        database=cs.database,
        user=cs.user,
    )
```

### 5.2 Orchestration functions (selected)
```python
def provision_workspace(spec, azure_ctx):
    # 1) Get kubeconfig from the *infra* stack
    infra = auto.select_stack("infra/dev", "modelops-infra", program=None)
    infra_out = infra.outputs()
    kubeconfig = infra_out["kubeconfig"].value  # already decrypted for this process

    # 2) Define the K8s program **that uses a provider** built from that kubeconfig
    import pulumi
    import pulumi_kubernetes as k8s

    def program():
        k8s_provider = k8s.Provider("aks", kubeconfig=kubeconfig)

        # All K8s resources must carry opts=ResourceOptions(provider=k8s_provider)
        ns = k8s.core.v1.Namespace(
            spec.namespace,
            metadata={"name": spec.namespace},
            opts=pulumi.ResourceOptions(provider=k8s_provider),
        )
        
        # <!-- PROBLEM: Image pull authentication paths are unclear and insecure -->
        # <!-- SOLUTION: Document both ACR (managed identity) and GHCR (PAT secret) paths clearly -->
        # <!-- WHY: Different registries need different auth methods; clear docs prevent auth failures -->
        
        # Image Pull Authentication Paths:
        # Option 1: ACR with Managed Identity (Recommended for production)
        #   - AKS automatically gets AcrPull role via RoleAssignment (see infra stack)
        #   - No secrets needed in pods
        #   - Images: myacr.azurecr.io/image:tag
        #   - NOTE: RoleAssignment can take 1-2 minutes to propagate
        #   - If pulls fail initially, add retry logic or wait before deploying pods
        
        # Option 2: GHCR with PAT (GitHub Personal Access Token)
        #   - Requires PAT with read:packages scope (create at github.com/settings/tokens)
        #   - Username MUST be your GitHub handle (not email)
        #   - Store in environment: export GITHUB_TOKEN="ghp_xxxx"
        #   - Creates K8s secret for imagePullSecrets
        
        # Check ALL images for GHCR usage (scheduler AND workers)
        # Create image pull secret if any images use GHCR
        images_to_check = [spec.dask.scheduler.image, spec.dask.workers.image]
        uses_ghcr = any("ghcr.io" in img for img in images_to_check)
        
        if uses_ghcr:
            # Get PAT from environment (required for GHCR)
            ghcr_pat = os.environ.get("GITHUB_TOKEN", "")
            if not ghcr_pat:
                raise ValueError(
                    "GITHUB_TOKEN environment variable required for GHCR image pulls. "
                    "Create a PAT with 'read:packages' scope at github.com/settings/tokens"
                )
            
            # Get username from environment (must be GitHub handle, not email)
            ghcr_username = os.environ.get("GITHUB_USERNAME", "")
            if not ghcr_username:
                raise ValueError(
                    "GITHUB_USERNAME environment variable required (your GitHub handle, not email)"
                )
            
            # Create dockerconfig JSON and mark as secret to prevent PAT exposure in logs
            dockerjson = pulumi.Output.json_dumps({
                "auths": {
                    "ghcr.io": {
                        "username": ghcr_username,
                        "password": ghcr_pat,
                        "auth": base64.b64encode(f"{ghcr_username}:{ghcr_pat}".encode()).decode()
                    }
                }
            })
            
            ghcr_secret = k8s.core.v1.Secret(
                "ghcr-creds",
                metadata={"name": "ghcr-creds", "namespace": spec.namespace},
                type="kubernetes.io/dockerconfigjson",
                string_data={
                    ".dockerconfigjson": pulumi.secret(dockerjson)  # CRITICAL: Mark as secret
                },
                opts=pulumi.ResourceOptions(provider=k8s_provider),
            )

        # Create Dask scheduler deployment with proper labels for NetworkPolicy
        scheduler_deployment = k8s.apps.v1.Deployment(
            "dask-scheduler",
            metadata={"name": "modelops-dask-scheduler", "namespace": spec.namespace},
            spec={
                "replicas": 1,
                "selector": {"matchLabels": {"app": "modelops-dask-scheduler"}},
                "template": {
                    "metadata": {"labels": {"app": "modelops-dask-scheduler"}},
                    "spec": {
                        "imagePullSecrets": [{"name": "ghcr-creds"}] if uses_ghcr else [],
                        "containers": [{
                            "name": "scheduler",
                            "image": spec.dask.scheduler.image,
                            "ports": [
                                {"containerPort": 8786, "name": "scheduler"},
                                {"containerPort": 8787, "name": "dashboard"}
                            ],
                            # TODO: Add resource limits, env vars, etc.
                        }]
                    }
                }
            },
            opts=pulumi.ResourceOptions(provider=k8s_provider),
        )
        
        # Create Dask scheduler service
        scheduler_service = k8s.core.v1.Service(
            "dask-scheduler-service",
            metadata={"name": "modelops-dask-scheduler", "namespace": spec.namespace},
            spec={
                "selector": {"app": "modelops-dask-scheduler"},
                "ports": [
                    {"name": "scheduler", "port": 8786, "targetPort": 8786},
                    {"name": "dashboard", "port": 8787, "targetPort": 8787}
                ],
                "type": "ClusterIP"
            },
            opts=pulumi.ResourceOptions(provider=k8s_provider),
        )
        
        # Create Dask worker deployment
        # IMPORTANT: Workers need writable /tmp and cache directories
        # Example worker deployment:
        # worker_deployment = k8s.apps.v1.Deployment(
        #     "dask-workers",
        #     spec={
        #         "template": {
        #             "spec": {
        #                 "imagePullSecrets": [{"name": "ghcr-creds"}] if uses_ghcr else [],
        #                 "containers": [{
        #                     "name": "worker",
        #                     "image": spec.dask.workers.image,
        #                     "securityContext": {
        #                         "readOnlyRootFilesystem": True,
        #                         "allowPrivilegeEscalation": False
        #                     },
        #                     "volumeMounts": [
        #                         {"name": "tmp", "mountPath": "/tmp"},
        #                         {"name": "cache", "mountPath": "/home/worker/.cache"}
        #                     ]
        #                 }],
        #                 "volumes": [
        #                     {"name": "tmp", "emptyDir": {}},
        #                     {"name": "cache", "emptyDir": {}}
        #                 ]
        #             }
        #         }
        #     }
        # )

        pulumi.export("scheduler_addr", "tcp://modelops-dask-scheduler:8786")
        pulumi.export("dashboard_url", "http://modelops-dask-scheduler:8787")

    # 3) Run the workspace stack with that program
    ws = auto.create_or_select_stack(f"workspace-{spec.namespace}", "modelops-workspace", program)
    res = ws.up()
    return {
        "scheduler_addr": res.outputs["scheduler_addr"].value,
        "dashboard_url": res.outputs["dashboard_url"].value,
    }
```

```python
# provision_adaptive.py (selected)
def provision_adaptive(spec: AdaptiveSpec, ws_binding: DaskBinding, azure_ctx: dict) -> dict:
    # Auto-generate namespace if not provided
    if not spec.namespace:
        import uuid
        spec.namespace = f"modelops-{uuid.uuid4().hex[:8]}"
    
    if spec.workers.replicas > 1 and not spec.central_store:
        raise ValueError("central_store is required when workers.replicas > 1")
    pg_binding = provision_postgres(spec, azure_ctx) if spec.central_store else None
    # Pulumi program creates Deployment for adaptive workers (env wiring below)
    return {"deployment": "adaptive-workers", "postgres_secret": pg_binding and pg_binding.secret_name}
```

### 5.3 Environment wiring (MVP)
```yaml
# Pod spec for adaptive workers (Dask workers similar)
env:
  - name: DASK_SCHEDULER_ADDRESS
    value: "{{ dask.scheduler_addr }}"
  - name: PGSSLMODE
    value: "disable"   # in-cluster
envFrom:
  - secretRef:
      name: "{{ postgres_binding.secret_name }}"
securityContext:
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  seccompProfile: { type: RuntimeDefault }
# Writable paths needed by Dask and libraries
# emptyDir volumes provide writable /tmp and cache directories
volumeMounts:
  - name: tmp
    mountPath: /tmp
  - name: cache
    mountPath: /home/worker/.cache
volumes:
  - name: tmp
    emptyDir: {}
  - name: cache
    emptyDir: {}
```

---

## 6) Decisions & Contracts (MVP)

- **D1 — Central store requirement.** If `workers.replicas > 1`, require a central store.
- **D2 — Pulumi state backend.** Use Azure Blob; never local for shared stacks.
- **D3 — AKS node labels contract.** Provision a `workcpu` pool with `modelops.io/role=cpu`.
- **D4 — Network posture (in-cluster).** ClusterIP-only; NetworkPolicy restricts to namespace + kube-dns.
- **D5 — Image pull auth.** GHCR PAT or ACR integration; fail fast if missing.
- **D6 — BUNDLE_REF.** From ConfigMap `${ns}-run`; optional if image-baked.
- **D7 — Diagnostics cap.** ≤ 64KB per trial; truncate with note.
- **D8 — Seeds.** Deterministic derivation when not provided by Calabaria.

---

## 7) Security Model (MVP)

- No secrets in YAML; generate at apply-time, store in K8s Secrets.
- Namespace-scoped Secret for Postgres; RBAC can be minimal (single user).
- NetworkPolicy egress: allow kube-dns and in-namespace Postgres/Dask only.
- Read-only root FS; disable privilege escalation on worker Pods.
- PVC-backed Postgres data for resilience to Pod restarts.

---

## 8) Developer Workflow

```bash
# 1) Azure auth
az login && az account set --subscription "<SUB_ID>"

# 2) Optional: build/push images
make images REGISTRY=ghcr.io/you VERSION=latest

# 3) Bring up infra + workspace plane
mops workspace up -f workspace.yaml

# 4) Bring up adaptive plane
mops adaptive up -f adaptive.yaml

# 5) Teardown (keep cluster)
mops adaptive down -n modelops
mops workspace down -n modelops

# 6) Destroy all Azure resources (danger)
# NOTE: mops destroy --all includes safety guard:
# - Refuses to run if target RG equals or contains Pulumi state RG/storage account
# - Requires explicit confirmation for production environments
mops destroy --all
```

---

## 9) Testing & Acceptance

- **Unit:** Spec validation; binding round-trips; DSN assembly; resource maps.
- **Integration:** Dask trivial compute; Postgres Secret present; workers start with DSN env available.
- **Acceptance:** End-to-end Ask/Tell with Optuna adapter; trials complete; cleanup commands work.

---

## 10) Future‑Proofing (post‑MVP)

### 10.1 Health and Reliability
**Health Probes:**
- Add liveness/readiness probes to all pods
- Postgres: `pg_isready` for health checks  
- Workers: HTTP endpoint or custom health script
- Dask: existing `/health` endpoint

**Connection Pooling:**
- PgBouncer sidecar for Postgres connection management
- Reduces connection overhead for high-concurrency workloads

**Backpressure Management:**
- Adaptive batch sizing based on queue depth
- Circuit breaker pattern for overloaded components
- Rate limiting at the Ask/Tell boundary

### 10.2 Data Management
**Backup Strategy:**
- Automated Postgres backups via CronJob + pg_dump
- Option to snapshot PVC (provider-dependent)
- WAL archiving for point-in-time recovery (managed Postgres)

**Migration to Managed Postgres:**
- Azure Database for PostgreSQL - Flexible Server
- Benefits: automated backups, HA, monitoring
- Migration path:
  1. Update spec: `mode: in-cluster` → `mode: managed`
  2. Provision Azure Flexible Server with SKU mapping
  3. Add bootstrap job for admin→runtime role separation
  4. Configure networking (firewall rules or private endpoint)
  5. Enable TLS (`sslmode=require`), update NetworkPolicy
  6. Optional: Switch to Azure Key Vault CSI driver

### 10.3 Security Enhancements
**Azure Key Vault Integration:**
- Replace K8s Secrets with Key Vault CSI Driver
- Centralized secret rotation
- Audit trail for secret access

**Workload Identity:**
- Pod-level Azure AD identities
- Eliminate long-lived storage keys
- Fine-grained RBAC for cloud resources

### 10.4 Scaling and Performance
**Autoscaling:**
- HPA for worker deployments based on CPU/memory
- KEDA for queue-based scaling (when queues are added)
- Cluster autoscaler for node pool scaling

**GPU Support:**
- Node selectors and tolerations for GPU pools
- NVIDIA device plugin configuration
- GPU-specific worker images

**Fanout Execution:**
- One task per replicate for better parallelism
- Result aggregation in central store
- Requires autoscaling to handle increased task count

### 10.5 Observability
**Metrics and Monitoring:**
- Prometheus metrics for all components
- Custom metrics: trials/sec, queue depth, success rate
- Azure Monitor integration

**Distributed Tracing:**
- OpenTelemetry instrumentation
- Request flow visualization
- Performance bottleneck identification

**Log Aggregation:**
- Structured logging with correlation IDs
- Fluentd/Fluent Bit for log shipping
- Azure Log Analytics queries

### 10.6 Developer Experience
**Local Development:**
- Kind/k3s profiles for laptop development
- Mocked cloud services for offline work
- Fast iteration with Tilt/Skaffold

**CI/CD Integration:**
- GitHub Actions for automated testing
- PR environments with ephemeral namespaces
- Automated rollback on failure

### 10.7 Advanced Features
**Queue Primitives:**
- Azure Service Bus for work distribution
- Dead letter queues for failed tasks
- Priority queues for urgent work

**Multi-Algorithm Coordination:**
- Hierarchical optimization workflows
- Algorithm chaining and pipelines
- Shared result caches

**Result Caching:**
- Content-addressed storage for simulation outputs
- Deduplication of identical parameter sets
- Cache-aware scheduling

---

## 11) Alignment With `modelops-contracts`

The MVP strictly honors the contracts from `modelops-contracts`:

- **`TrialStatus`**: Use `COMPLETED`, `FAILED`, `TIMEOUT` per provided code.
- **`UniqueParameterSet`**: Stable `param_id` → used verbatim in Ask/Tell runner.
- **`SimulationService`**: Our Dask implementation conforms exactly (fn_ref, params, seed, bundle_ref).
- **Diagnostics**: Remain capped (< 64KB) and never include secrets.
- **Seeds**: Deterministic derivation when not provided: `seed = stable_hash(param_id) + i`.
- **Immutability**: Pydantic models with `frozen=True` per modelops-contracts specification.

---

## 12) Known Limitations (MVP)

- **Single-tenant namespace**: No multi-tenant RBAC; one user per workspace.
- **No GPU support**: CPU-only node pool; GPU node selectors/taints not defined.
- **Postgres limitations**:
  - Single replica only (PVC uses ReadWriteOnce)
  - No high availability or read replicas
  - No automated backups (manual `pg_dump` required before teardown)
  - Mode `managed` parsed but not implemented
- **No autoscaling**: Worker replicas are fixed; manual scaling only.
- **No external networking**: All Services are ClusterIP; no ingress controllers.
- **Limited observability**: Basic logs only; no metrics or distributed tracing.
- **No cross-cloud support**: Azure-only implementation; provider abstraction deferred.

---

## 13) Appendix — Minimal K8s Snippets

**Adaptive workers Deployment (fragment).**
```yaml
spec:
  template:
    spec:
      automountServiceAccountToken: false
      nodeSelector: { modelops.io/role: cpu }
      containers:
        - name: worker
          image: ghcr.io/you/adaptive-worker:mvp
          env:
            - name: DASK_SCHEDULER_ADDRESS
              value: "tcp://modelops-dask-scheduler:8786"
            - name: PGSSLMODE
              value: "disable"
          envFrom:
            - secretRef:
                name: "modelops-pg-env"
          resources:
            requests: { cpu: "1", memory: "2Gi" }
            limits:   { cpu: "2", memory: "4Gi" }
```

**NetworkPolicy (egress only).**
```yaml
# Problem: Dask workers need to communicate on ephemeral ports, too restrictive policy breaks this
# Solution: Allow all egress within namespace + DNS + specific external services
# Why: Dask uses random ports for worker-to-worker communication
#
# IMPORTANT: NetworkPolicy only affects Pod-to-Pod traffic, NOT node-level operations
# Image pulls happen at the kubelet/node level and are not restricted by NetworkPolicy
# Only runtime Pod egress (e.g., downloading bundles from inside containers) is affected
kind: NetworkPolicy
apiVersion: networking.k8s.io/v1
metadata:
  name: adaptive-egress-policy-7x9k2
  namespace: modelops-run-7x9k2  # Per-run namespace generated by CLI
spec:
  podSelector: {}
  policyTypes: ["Egress"]
  egress:
    # Rule 1: Allow all egress within namespace (no port restrictions)
    # This covers adaptive worker-to-worker on ephemeral ports and Postgres
    - to:
        - podSelector: {}
    
    # Rule 2: Allow egress to Dask scheduler in workspace namespace
    # CRITICAL: Adaptive workers MUST reach the Dask scheduler across namespaces
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: modelops  # workspace namespace
          podSelector:
            matchLabels:
              app: modelops-dask-scheduler
      ports:
        - protocol: TCP
          port: 8786  # Scheduler port
        - protocol: TCP
          port: 8787  # Dashboard port
    
    # Rule 3: Allow DNS lookups to kube-system
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    
    # Rule 4: OPTIONAL - Allow HTTPS for bundle/image pulls (if needed)
    # Remove this rule if all images are pre-pulled and no external bundles needed
    # NOTE: This is commented out for maximum security - uncomment if bundles needed
    # - to: []  # Allow to any IP
    #   ports:
    #     - protocol: TCP
    #       port: 443
    # For production, restrict to specific endpoints:
    # - ghcr.io: 140.82.112.0/20, 143.55.64.0/20
    # - Azure Storage: Use Private Endpoints or Service Tags
    # - ACR: *.azurecr.io via Private Endpoints
```

---

## 14) Final Notes

This revision **updates the plan to provision Azure infra from zero** using
Pulumi (azure‑native) and wires that kubeconfig into the Pulumi **kubernetes**
programs that deploy the workspace and adaptive planes. The rest of the design
(types, bindings, runners, in-cluster Postgres, grouped execution) remains
intact and aligned with the MVP constraints.
