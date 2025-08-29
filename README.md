# ModelOps

Infrastructure orchestration for distributed machine learning experimentation and optimization.

## Overview

ModelOps provides the infrastructure layer ("the hands") for running distributed ML experiments, implementing the contracts defined in `modelops-contracts` and providing runtime infrastructure for `calabaria` (the science/algorithm layer).

## Architecture

ModelOps manages two execution planes:

1. **Workspace Plane**: Long-lived Dask clusters for simulation execution
2. **Adaptive Plane**: Ephemeral infrastructure for calibration algorithms (coming soon)

### Key Components

- **SimulationService**: Implementations for local and distributed execution
- **Workspace Management**: Pulumi-based infrastructure provisioning
- **Provider Abstraction**: Cloud-agnostic infrastructure management
- **State Management**: Local state tracking for provisioned resources

## Installation

```bash
# Install in development mode
pip install -e .

# Or install with uv
uv pip install -e .
```

## Quick Start

### 1. Configure a Provider

```bash
# Create provider configuration
mkdir -p ~/.modelops/providers

# For local development (Kind/Minikube)
cat > ~/.modelops/providers/local.yaml <<EOF
kind: Provider
provider: local
spec:
  kubeconfig: ~/.kube/config
  context: kind-kind
EOF
```

### 2. Provision a Workspace

```bash
# Create a Dask workspace
mops workspace up --name dev --provider local

# Check status
mops workspace status

# Get connection details
mops workspace connect --name dev
```

### 3. Use SimulationService

```python
from modelops.services import DaskSimulationService

# Connect to workspace
sim = DaskSimulationService.from_workspace("dev")

# Submit simulations
futures = []
for i in range(10):
    future = sim.submit(
        fn_ref="my_module:simulate",
        params={"beta": 0.5, "gamma": 0.1},
        seed=i,
        bundle_ref="oras://registry/my-sim:latest"
    )
    futures.append(future)

# Gather results
results = sim.gather(futures)
```

## CLI Commands

### Workspace Management

```bash
# Provision workspace
mops workspace up --name <name> --provider <provider> --min-workers 2 --max-workers 10

# List workspaces
mops workspace list

# Get workspace status
mops workspace status --name <name>

# Destroy workspace
mops workspace down --name <name>

# Port-forward dashboard
mops workspace port-forward --name <name>
```

### Configuration

```bash
# Show version
mops version

# Show configuration paths
mops config
```

## Development

### Project Structure

```
src/modelops/
   cli/              # CLI commands (Typer-based)
   services/         # SimulationService implementations
   state/            # Local state management
   infra/            # Infrastructure provisioning
      providers/    # Cloud provider abstractions
   __init__.py
```

### Testing

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=modelops
```

### Code Quality

```bash
# Type checking
mypy src/modelops

# Linting
ruff check src/

# Formatting
black src/
```

## Infrastructure Provisioning Flow

ModelOps follows a clean Spec->Compile->Apply pattern:

1. **Spec**: User defines infrastructure in YAML or via CLI
2. **Parse**: Configuration is validated with Pydantic models
3. **Compile**: Specs are lowered to intermediate representation (IR)
4. **Build**: IR is used to create Pulumi programs
5. **Apply**: Pulumi provisions actual cloud resources

## Provider Configuration

Providers are configured via YAML files in `~/.modelops/providers/`:

### Azure Example

```yaml
kind: Provider
provider: azure
spec:
  subscription_id: xxx-xxx-xxx
  resource_group: modelops-rg
  location: eastus
  aks_cluster: modelops-aks
auth:
  method: cli  # Uses Azure CLI authentication
```

### Local Example

```yaml
kind: Provider
provider: local
spec:
  kubeconfig: ~/.kube/config
  context: kind-kind
```

## Security

- Provider configs contain configuration, not credentials
- Authentication methods:
  - Azure CLI (development)
  - Environment variables (CI/CD)
  - Managed Identity (production)
- Secrets are stored in Kubernetes secrets, not in config files

## Dependencies

- Python >=3.11
- modelops-contracts (local dependency)
- Dask for distributed execution
- Pulumi for infrastructure provisioning (coming soon)
- Rich & Typer for CLI

## Related Projects

- **modelops-contracts**: Defines stable API protocols
- **calabaria**: Science/algorithm framework
- **modelops-bundle**: Code/data packaging (coming soon)

## License

MIT