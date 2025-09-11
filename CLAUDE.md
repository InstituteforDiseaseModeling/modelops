# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ModelOps is a Kubernetes-native runtime for simulation-based methods, providing infrastructure orchestration for distributed machine learning experimentation and optimization. It implements a clean separation between science (Calabaria) and infrastructure through well-defined contracts.

**Core Philosophy:**
- **Single-user workspace**: One principal spins up a workspace (no multi-tenant scheduling for MVP)
- **Seams-first**: Keep the science (Calabaria) and infra (ModelOps) decoupled by strict contracts
- **Algorithms request infra**: Each algorithm specifies needs through declarative YAML spec
- **Ask/Tell everywhere**: Core optimization/evaluation handshake is same regardless of algorithm
- **Provider plugins**: Azure MVP now; AWS/GCP later via same compile/apply interface
- **Minimal surprises**: Secure-by-default secrets, deterministic seeds, idempotent ops

## Architecture

### Resource Naming Strategy

ModelOps uses centralized naming through the `StackNaming` class to ensure consistency across all resources:

**Container Registry (ACR)**:
- **Development**: Per-user registries using pattern `modelops{env}acr{username}` (e.g., `modelopsdevacrvsp`)
  - Provides isolation between developers
  - Avoids naming conflicts during experimentation
- **Production**: Org-level with random suffix `modelops{env}acr{random}` (e.g., `modelopsprodacr7x9k`)
  - Shared across team for cost efficiency
  - Random suffix ensures global uniqueness (ACR requirement)
- **Configuration**: Set `per_user_registry: false` to force org-level in dev

**Resource Groups**:
- Pattern: `modelops-{env}-rg-{username}` for per-user isolation
- Example: `modelops-dev-rg-vsb`
- Ensures developers don't interfere with each other's resources

**Why This Approach**:
1. **Global Uniqueness**: ACR names must be unique across ALL Azure subscriptions worldwide
2. **Developer Isolation**: Each developer gets independent resources in dev/staging
3. **Cost Optimization**: Production uses shared resources to minimize costs
4. **Flexibility**: Can override with explicit names when needed via config

All naming is centralized in `src/modelops/core/naming.py` to maintain consistency.

### Core Seams

The system follows a seams-first architecture with strict separation of concerns:

#### 1. Ask/Tell Protocol (from `modelops-contracts`)
```python
@runtime_checkable
class AdaptiveAlgorithm(Protocol):
    def ask(self, n: int) -> list[UniqueParameterSet]: ...
    def tell(self, results: list[TrialResult]) -> None: ...
    def finished(self) -> bool: ...
```
**Guarantees**: stable param_id, idempotent terminal writes, order-free tell  
**What it doesn't say**: anything about infra - algorithms are pure consumers/producers

#### 2. SimulationService Seam
```python
class SimulationService(Protocol):
    def submit(self, fn_ref: str, params: dict[str, Scalar], seed: int, *, bundle_ref: str) -> FutureLike: ...
    def gather(self, futures: list[FutureLike]) -> list[SimReturn]: ...
```
**Implementations**: Dask (MVP), Ray, local multiprocessing, threads  
**Binding**: Worker runner injects bound implementation into evaluation loop

#### 3. Lifecycle Seam (infra-agnostic bootstrap/teardown)
```python
@dataclass(frozen=True)
class RunContext:
    run_id: str
    env: dict[str, str]  # resolved env (secrets redacted), endpoints, etc.

class HasLifecycle(Protocol):
    def on_start(self, ctx: RunContext) -> None: ...
    def on_finish(self, ctx: RunContext) -> None: ...
```
**Use**: Databases, queues, Dask clusters, or any component needing init/finalization

#### 4. Target Evaluation Seam (Calabaria)
Calabaria evaluates simulation outputs against targets through a clean interface:
- Remains **infra-agnostic**: ModelOps only supplies SimulationService and worker pools
- Handles Target → AlignmentStrategy → EvaluationStrategy → Loss computation

### YAML → Compile → Apply Workflow

**Goal**: Algorithm's declarative infra needs are translated into provider-specific resources and fed back as **BindingOutputs** (connection strings, service URLs, kube service names).

1. YAML describes **primitives** (worker groups, databases, object stores) and **bindings**
2. Compiler emits **CompiledPlan** (cloud-agnostic) and **ProviderPlan** (cloud-specific)
3. Apply produces **BindingOutputs** used by runners (env, secrets, configmaps)

### Worker Runner Pattern

The Worker Runner unites all seams:
```python
# Pseudocode for the core loop
while not algo.finished():
    batch = algo.ask(n=batch_size)
    if not batch: continue
    futures = [sim.submit(fn_ref, p.params, seed_source(seed_i), bundle_ref=bundle) 
               for seed_i, p in enumerate(batch)]
    sim_results = sim.gather(futures)
    trial_results = evaluate_and_convert(sim_results)  # wraps Calabaria Target→loss
    algo.tell(trial_results)
```

## Development Commands

### Environment Setup
```bash
# Use direnv for automatic environment configuration
cp .envrc.template .envrc
direnv allow

# Or manually set Pulumi passphrase for local development
export PULUMI_CONFIG_PASSPHRASE=dev
```

### Build and Package
```bash
# Build package
uv build

# Install in development mode with uv
uv sync
```

### Testing
```bash
# Run tests with uv
uv run pytest

# Run specific test
uv run pytest tests/test_specific.py

# Run with coverage
uv run pytest --cov=modelops --cov-report=html
```

### Code Quality
```bash
# Type checking
uv run mypy src/

# Linting
uv run ruff check src/
uv run black src/
```

### CLI Commands
```bash
# Infrastructure provisioning (Azure)
uv run mops infra up --config ~/.modelops/providers/azure.yaml
uv run mops infra status
uv run mops infra down --config ~/.modelops/providers/azure.yaml

# Workspace management (Dask)
uv run mops workspace up --config workspace.yaml
uv run mops workspace status
uv run mops workspace down

# Adaptive runs (Optuna, etc.)
uv run mops adaptive up --config adaptive.yaml
uv run mops adaptive status --run-id <run-id>
uv run mops adaptive down --run-id <run-id>

# Configuration
uv run mops config
uv run mops version
```

## Configuration Management

### YAML Specification

Algorithms declare infrastructure needs via declarative YAML:

```yaml
apiVersion: modelops/v1
kind: Runtime
metadata:
  name: prod
spec:
  provider:
    name: azure
    subscription_id: "00000000-0000-0000-0000-000000000000"
    location: eastus2
    resource_group: modelops-rg
    aks:
      name: modelops-aks
      managed: true  # we create/update vs external: true (just bind)

  objectStore:
    name: results
    managed: true
    class: azure-blob
    bucket: calabaria-results

  databases:
    optuna:
      engine: postgres
      managed: true
      size: small  # tier/sku string

  workerGroups:
    default-cpu:
      kind: k8s-pool
      profile: cpu
      min: 2
      max: 6
      resources:
        cpu: "4"
        memory: "16Gi"
      image:
        ref: ghcr.io/org/calabaria-worker:main
    gpu-calib:
      kind: k8s-pool
      profile: gpu
      min: 0
      max: 2
      gpu:
        vendor: nvidia
        count: 1
        model: "A10"

  algorithms:
    optuna-calibration:
      type: optuna
      bindings:
        workers: default-cpu
        database: optuna
        objectStore: results
      parameters:
        sampler: tpe
        n_parallel: 16
      entrypoint:
        module: "calabaria.runners.optuna:main"
        bundleRef: "oci://ghcr.io/org/model-bundle:sha256:abcd"
```

### Binding Outputs

The compiler produces BindingOutputs containing:
- **env**: Environment variables (DATABASE_URL, DASK_SCHEDULER, OBJECT_STORE_URL)
- **secrets**: K8s Secret references for sensitive data
- **addresses**: Service hostnames, ports, and schemes

### State Management

All infrastructure and workspace state is managed through Pulumi stacks:
- **Stack Outputs**: The only persistent state (kubeconfig, endpoints, resource IDs)
- **StackReferences**: Allow stacks to read outputs from other stacks
- **Four-Stack Pattern**: 
  - `modelops-registry` → Container registry (ACR for Azure)
  - `modelops-infra` → Infrastructure (AKS, networking)
  - `modelops-workspace` → Dask deployment (reads infra outputs)
  - `modelops-adaptive-{run-id}` → Optimization runs (reads workspace outputs)
- **Query State**: Use `pulumi stack output` commands or Automation API
- **No Custom State**: No `~/.modelops/state.json` - everything flows through Pulumi

## Key Protocols and Interfaces

### Provider Plugin Interface
```python
class ProviderPlugin(Protocol):
    def compile(self, spec: RuntimeSpec) -> CompiledPlan: ...
    def apply(self, plan: CompiledPlan) -> dict[str, BindingOutputs]: ...
```
- **compile**: Resolve shapes (e.g., GPU ⇒ node taints, runtime class) into provider-native resources
- **apply**: Create/update infra and return finalized connection info/secrets

### Node Pool Concept (Cloud-Agnostic)

A **WorkerGroup** is our cloud-agnostic concept mapping to provider-specific scale sets:
- Azure → AKS **node pool** (agent pool) with taints/labels and K8s Deployment
- AWS → EKS **node group**
- GCP → GKE **node pool**

Attributes: CPU/GPU shape, min/max replicas, spot/priority, image constraints, tolerations

## Implementation Notes

### Core Guarantees
- **param_id stability**: Same parameters always produce same ID
- **Idempotent operations**: All infrastructure operations must be retry-safe
- **tell() idempotency**: Must be idempotent per trial_id
- **Deterministic seeds**: Seeds are uint64 range, deterministically derived
- **Size limits**: Diagnostics < 64KB (JSON-serialized)

### Cloud Provider Mapping (Azure MVP)
- **WorkerGroup** → AKS NodePool + K8s Deployment with nodeSelector/tolerations
- **Database** → Azure Flexible Server (or containerized Postgres for dev)
- **ObjectStore** → Storage Account + Blob Container
- **Images** → Pull from ACR or GHCR with optional pull secrets

### Development Workflow
- Use `direnv` for automatic environment setup
- Use `uv` for Python dependency management
- Azure for Kubernetes deployment
- Pulumi with passphrase provider for infrastructure as code

## Implementation Roadmap

### MVP (Azure-first)
1. **Spec & validator**: Finalize YAML + Pydantic models (RuntimeSpec, WorkerGroupCfg, DatabaseCfg)
2. **Azure compiler**: Generate AKS pools, Deployments/Jobs, Flexible Server, Storage Account
3. **Apply with Pulumi**: Idempotent create/update, export BindingOutputs
4. **Runner images**:
   - Dask SimulationService container (scheduler+workers)
   - Optuna runner bridging ask/tell with Calabaria evaluation
5. **Secrets & env**: Map BindingOutputs to K8s Secrets/ConfigMaps/EnvFrom
6. **Developer flow**: direnv + uv workflow with `mops workspace up`

### Near-term Hardening
- GPU worker groups with nvidia runtime and image pull secrets
- On-cluster Postgres via Helm (dev) vs Azure Flexible Server (prod)
- Workload identity for storage access (avoid long-lived keys)
- Observability: add run_id labels, minimal metrics (trial/sec, queue depth)

### Future Features
- AWS & GCP provider plugins (same plugin interface)
- Queue primitives (Azure Service Bus/SQS/PubSub) as first-class
- Artifact manifests and bundle integrity checks
- Multi-user tenancy and RBAC

## Dependencies

### Core Dependencies
- Python >=3.11 (specified in pyproject.toml)
- Kubernetes for container orchestration
- Pulumi for infrastructure as code
- Dask (2024.8.0) for distributed computation
- modelops-contracts (stable API contracts)

### Provider-Specific
- **Azure**: AKS for Kubernetes
- **Azure**: AKS, Flexible Server, Storage Accounts
- **Future**: AWS (EKS), GCP (GKE)

### Optional Components
- Optuna for hyperparameter optimization
- PostgreSQL for distributed coordination
- Azure Monitor for observability

## Dependency Rules

- **ModelOps**: May import from modelops_contracts only
- **Calabaria**: May import from modelops_contracts only
- **Contracts**: Zero heavy dependencies (no NumPy, Polars, Optuna, etc.)

### Contract Guarantees
- param_id is stable: same params → same ID
- loss is finite for **COMPLETED** status
- diagnostics < 64KB (JSON-serialized)
- Seeds are uint64 range
- All types are immutable (frozen dataclasses)

## Important Reminders

### Do what has been asked; nothing more, nothing less
- NEVER create files unless they're absolutely necessary for achieving your goal
- ALWAYS prefer editing an existing file to creating a new one
- NEVER proactively create documentation files (*.md) or README files unless explicitly requested
- Always use `uv run` for Python commands, not `python` or `python3`

## Current MVP Focus

This repository is implementing the Azure-only MVP with Pulumi-native infrastructure:

### Architecture
- **Four-Stack Pattern**: Registry → Infrastructure → Workspace → Adaptive (connected via StackReferences)
- **ComponentResources**: Encapsulate cloud provisioning complexity
- **Pulumi Stack State**: Single source of truth (no custom state management)
- **Typed Bindings**: Runtime DTOs for passing data between components

### Implemented Features
- **Infrastructure Plane**: AKS cluster creation with node pools via `mops infra up`
- **ComponentResource**: AzureModelOpsInfra creates Resource Group, ACR, AKS from zero
- **Stack Outputs**: Kubeconfig, cluster endpoints, ACR login servers
- **CLI Commands**: `mops infra up/down/status` with Pulumi Automation API

### To Be Implemented
1. **Workspace Plane**: DaskWorkspace ComponentResource using StackReference to infra
2. **Adaptive Plane**: OptunaRun ComponentResource using StackReference to workspace
3. **Postgres Provisioning**: Azure Flexible Server or containerized for dev
4. **Object Store**: Azure Storage Account integration
5. **Worker Runners**: Ask/Tell protocol implementation with Calabaria integration
6. **Bundle Loading**: OCI artifact support for model bundles

### Key Design Decisions
- **No Custom State**: All state flows through Pulumi stacks
- **Clean Separation**: Provisioning (ComponentResources) vs Runtime (bindings)
- **Provider Agnostic**: Azure MVP but extensible to AWS/GCP via same patterns
- **Security**: Secrets marked with `pulumi.Output.secret()`, workload identity for storage

## Dask Worker Configuration

### Process vs Thread Architecture

ModelOps configures Dask workers with careful consideration of Python's GIL:

**For Simulation Workloads (Default)**:
- Use `processes: 2, threads: 1` in `workspace.yaml`
- Pure Python simulation code cannot release the GIL
- Multiple processes provide true parallelism
- Each process gets `pod_memory / nprocs`

**For Data Science Workloads**:
- Use `processes: 1, threads: 4` for NumPy/Pandas
- These libraries release the GIL for vectorized operations
- Threads share memory efficiently

**Configuration Flow**:
```
workspace.yaml → Pulumi → K8s Deployment → Dask Workers
  processes: 2     replicas: 4      4 pods       8 total processes
  threads: 1                         2 procs/pod  1 thread each
```

See `docs/dask-configuration.md` for detailed guidance.