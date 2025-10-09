# ModelOps

[![Tests](https://github.com/vsbuffalo/modelops/actions/workflows/tests.yml/badge.svg)](https://github.com/vsbuffalo/modelops/actions/workflows/tests.yml)
[![Docker Build](https://github.com/institutefordiseasemodeling/modelops/actions/workflows/docker-build.yml/badge.svg)](https://github.com/vsbuffalo/modelops/actions/workflows/docker-build.yml)
[![Docker Images](https://img.shields.io/badge/ghcr.io-modelops-blue)](https://github.com/orgs/institutefordiseasemodeling/packages)

Infrastructure orchestration for distributed machine learning experimentation
and optimization.

## Build Status & Quick Deploy

```bash
# Check build status: Look for green badge above or visit Actions tab
# After successful build (usually ~5 mins), deploy:
make pull-latest  # Pull CI-built images
make deploy       # Deploy to cluster
make verify-deploy # Verify deployment
```

## Overview

ModelOps provides the infrastructure layer ("the hands") for running
distributed ML experiments, implementing the contracts defined in
`modelops-contracts` and providing runtime infrastructure for `calabaria` (the
science/algorithm layer).

## Architecture

ModelOps implements a four-stack architecture using Pulumi:

1. **Registry Stack** (`mops registry`): Creates container registry (ACR for
   Azure)
2. **Infrastructure Stack** (`mops infra`): Creates cloud resources (AKS,
   resource groups, networking)
3. **Workspace Stack** (`mops workspace`): Deploys Dask clusters on Kubernetes
   for simulation execution
4. **Adaptive Stack** (`mops adaptive`): Manages ephemeral optimization runs
   (Optuna, MCMC, etc.)

Each stack references outputs from previous stacks using Pulumi
StackReferences, enabling clean separation of concerns and independent
lifecycle management.

### Key Components

- **SimulationService**: Implementations for local and distributed execution
- **Four-Stack Management**: Pulumi-based infrastructure provisioning with
  registry isolation
- **Provider Abstraction**: Cloud-agnostic infrastructure management (Azure
  MVP, AWS/GCP coming)
- **Centralized Naming**: Consistent resource naming across environments

## Installation

```bash
# Install with uv (recommended)
uv pip install -e .

# Or standard pip
pip install -e .

# Install required dependencies
uv pip install numpy  # For example simulations
```

## Quick Start

### 1. Configure Azure Provider

```bash
# Create provider configuration
mkdir -p ~/.modelops/providers

cat > ~/.modelops/providers/azure.yaml <<EOF
provider: azure
subscription_id: "YOUR-SUBSCRIPTION-ID"
location: eastus2
resource_group: modelops-rg
aks:
  name: modelops-aks
  kubernetes_version: "1.32"
EOF
```

### 2. Create Infrastructure (Stack 1)

```bash
# Create Azure infrastructure
mops infra up --config ~/.modelops/providers/azure.yaml --env dev

# Check status
mops infra status --env dev
```

### 3. Deploy Dask Workspace (Stack 2)

```bash
# Deploy Dask on the infrastructure
mops workspace up --env dev

# Check status
mops workspace status --env dev

# Port-forward for local access
kubectl port-forward -n modelops-dask-dev svc/dask-scheduler 8786:8786
```

### 4. Run Simulations

```python
from modelops.services import DaskSimulationService

# Connect to Dask
sim = DaskSimulationService("tcp://localhost:8786")

# Submit simulations
futures = []
for i in range(10):
    future = sim.submit(
        fn_ref="examples.simulations:monte_carlo_pi",
        params={"n_samples": 100000},
        seed=i,
        bundle_ref=""  # MVP: assumes code is pre-installed
    )
    futures.append(future)

# Gather results
results = sim.gather(futures)
```

## Running Simulations

ModelOps provides both local and distributed simulation capabilities. Example simulations are included in `examples/` directory.

### Available Example Simulations

1. **Monte Carlo Pi Estimation** (`monte_carlo_pi`)
   - Estimates π using random sampling
   - Configurable sample size
   - Good for testing parallel execution

2. **Black-Scholes Option Pricing** (`black_scholes_option`)
   - European option pricing via Monte Carlo
   - Supports calls and puts
   - Configurable volatility, strike, maturity

3. **Stochastic Growth Model** (`stochastic_growth_model`)
   - Simulates asset price paths using GBM
   - Calculates returns and drawdowns
   - Useful for financial modeling

### Running Simulations Locally

Test simulations without any infrastructure using the local execution mode:

```bash
# Set Python path to include the project
export PYTHONPATH=/path/to/modelops:$PYTHONPATH

# Run all simulation types locally
uv run python examples/run_dask_simulation.py --local --test all -n 10

# Run specific simulation type
uv run python examples/run_dask_simulation.py --local --test pi -n 20
uv run python examples/run_dask_simulation.py --local --test option -n 15
uv run python examples/run_dask_simulation.py --local --test growth -n 5
```

Options:
- `--local`: Use LocalSimulationService (no Dask required)
- `--test [all|pi|option|growth]`: Which simulations to run
- `-n`: Number of simulations to execute

### Running Simple Functions on Dask

For immediate testing with the existing Dask cluster (no custom images needed):

```bash
# 1. Port-forward the Dask scheduler
kubectl port-forward -n modelops-default svc/dask-scheduler 8786:8786

# 2. Run simple pure-Python functions
uv run python examples/test_dask_simple.py
```

This runs:
- Monte Carlo Pi estimation using pure Python
- Matrix multiplication benchmarks
- Shows ~15 tasks/second throughput on a single worker

### Running ModelOps Simulations on Dask

To run the full simulation suite on Dask (requires custom worker image):

```bash
# 1. Port-forward Dask scheduler and dashboard
kubectl port-forward -n modelops-default svc/dask-scheduler 8786:8786 &
kubectl port-forward -n modelops-default svc/dask-scheduler 8787:8787 &

# 2. Run distributed simulations (requires custom worker image)
PYTHONPATH=/path/to/modelops:$PYTHONPATH uv run python examples/run_dask_simulation.py --test all -n 100
```

**Note**: This currently requires building a custom Dask worker image with ModelOps code installed. See "Custom Worker Images" section below.

### Monitoring with Dask Dashboard

While simulations are running, monitor execution via the Dask dashboard:

```bash
# Port-forward the dashboard
kubectl port-forward -n modelops-default svc/dask-scheduler 8787:8787

# Open in browser
open http://localhost:8787
```

The dashboard shows:
- Task progress and timeline
- Worker CPU and memory usage
- Task stream and performance metrics
- Cluster topology

### Debugging and Cluster Status

```bash
# Check Dask pods
kubectl get pods -n modelops-default

# View scheduler logs
kubectl logs -n modelops-default -l app=dask-scheduler

# View worker logs
kubectl logs -n modelops-default -l app=dask-worker

# Get service endpoints
kubectl get svc -n modelops-default

# Check worker resource usage
kubectl top pods -n modelops-default
```

### Custom Worker Images

To run ModelOps simulations on Dask workers, you need to build a custom image:

```dockerfile
# docker/dask-worker/Dockerfile
FROM ghcr.io/dask/dask:2024.8.0-py3.11

# Install dependencies
RUN pip install numpy modelops-contracts

# Copy ModelOps code
COPY src/modelops /opt/modelops/src/modelops
COPY examples /opt/modelops/examples

WORKDIR /opt/modelops
ENV PYTHONPATH=/opt/modelops/src:/opt/modelops:$PYTHONPATH
```

Build and use:
```bash
# Build image
docker build -t myregistry/dask-worker:latest docker/dask-worker/

# Update worker deployment
kubectl set image deployment/dask-workers -n modelops-default \
  worker=myregistry/dask-worker:latest
```

## CLI Commands

### Infrastructure Management (Stack 1)

```bash
# Create infrastructure
mops infra up --config <config.yaml> --env <env>

# Check status
mops infra status --env <env>

# Destroy infrastructure (keeps resource group by default)
mops infra down --config <config.yaml> --env <env>

# Destroy everything including resource group
mops infra down --config <config.yaml> --env <env> --delete-rg
```

### Workspace Management (Stack 2)

```bash
# Deploy Dask workspace
mops workspace up --env <env> [--config workspace.yaml]

# List all workspaces
mops workspace list

# Check workspace status
mops workspace status --env <env>

# Destroy workspace
mops workspace down --env <env>
```

### Adaptive Runs (Stack 3)

```bash
# Start optimization run
mops adaptive up <config.yaml> --env <env> [--run-id <id>]

# Check run status
mops adaptive status <run-id>

# View logs
mops adaptive logs <run-id> [-f]

# List all runs
mops adaptive list

# Destroy run
mops adaptive down <run-id>
```

### Utility Commands

```bash
# Show version
mops version

# Show configuration
mops config
```

## Development

### Project Structure

```
src/modelops/
├── cli/              # CLI commands (Typer-based)
├── services/         # SimulationService implementations
├── core/            # Core utilities (naming, etc.)
├── infra/           # Infrastructure provisioning
│   └── components/  # Pulumi ComponentResources
├── examples/        # Example simulations and tests
└── tests/          # Unit tests
```

### Environment Setup

```bash
# Use direnv for automatic environment configuration
cp .envrc.template .envrc
direnv allow

# Or manually set Pulumi passphrase
export PULUMI_CONFIG_PASSPHRASE=dev
```

### Testing

```bash
# Run all tests
uv run pytest

# Run specific test module
uv run pytest tests/test_naming.py -v

# Run with coverage
uv run pytest --cov=modelops --cov-report=html
```

### Code Quality

```bash
# Type checking
uv run mypy src/

# Linting
uv run ruff check src/

# Formatting
uv run black src/
```

## Troubleshooting

### Common Issues

**"unknown stack modelops-infra-dev"**
- The infrastructure wasn't created with the current naming convention
- Solution: Recreate infrastructure with `mops infra up`

**"ModuleNotFoundError: No module named 'modelops'"**
- The Dask workers don't have ModelOps code installed
- Solution: Use `--local` flag or build custom worker image

**"No module named 'numpy'"**
- Missing required dependencies
- Solution: `uv pip install numpy`

**Port-forward not working**
- Check if pods are running: `kubectl get pods -n modelops-default`
- Check service exists: `kubectl get svc -n modelops-default`

## Infrastructure Provisioning Flow

ModelOps follows a four-stack pattern with Pulumi:

1. **Stack 1 (Registry)**: Creates container registry
   - Azure Container Registry (ACR)
   - Exports: login server, registry credentials

2. **Stack 2 (Infrastructure)**: Creates cloud resources
   - Resource groups, AKS clusters, networking
   - Exports: kubeconfig, cluster details

3. **Stack 3 (Workspace)**: Deploys Dask using Stack 2's kubeconfig
   - Dask scheduler and workers
   - Exports: scheduler address, dashboard URL

4. **Stack 4 (Adaptive)**: Creates optimization jobs using Stack 2 & 3
   - References Dask scheduler from Stack 3
   - Manages Optuna, MCMC, and other adaptive algorithms

## Security

- Provider configs contain configuration, not credentials
- Authentication methods:
  - Azure CLI (development)
  - Environment variables (CI/CD)
  - Managed Identity (production)
- Secrets stored in Kubernetes secrets
- Per-user resource groups for isolation

## Dependencies

- Python >=3.11
- modelops-contracts (API protocols)
- Dask 2024.8.0 (distributed execution)
- Pulumi (infrastructure as code)
- Kubernetes (container orchestration)
- NumPy (numerical computations)

## Related Projects

- **modelops-contracts**: Defines stable API protocols
- **calabaria**: Science/algorithm framework
- **modelops-bundle**: Code/data packaging (coming soon)

## License

MIT
