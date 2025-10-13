# ModelOps

[![Tests](https://github.com/vsbuffalo/modelops/actions/workflows/tests.yml/badge.svg)](https://github.com/vsbuffalo/modelops/actions/workflows/tests.yml)
[![Docker Build](https://github.com/vsbuffalo/modelops/actions/workflows/docker-build.yml/badge.svg)](https://github.com/vsbuffalo/modelops/actions/workflows/docker-build.yml)

Kubernetes-native infrastructure orchestration for distributed simulation and optimization workloads.

## What is ModelOps?

ModelOps provides the infrastructure layer for running distributed simulations and adaptive optimization algorithms (Optuna, MCMC) on Kubernetes. It implements the contracts defined in `modelops-contracts` and provides runtime infrastructure for science frameworks like `calabaria`.

**Key Features:**
- **Four-stack architecture** with Pulumi for clean infrastructure management
- **OCI bundle support** for reproducible simulation code distribution
- **Warm process pools** for 16x faster simulation execution
- **Single source of truth** for Docker images and configuration
- **Azure-native** with AWS/GCP coming soon

## Prerequisites

### Required Tools
- **Python 3.11+**
- **Azure CLI** (`az`) - [Install guide](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli)
- **kubectl** - [Install guide](https://kubernetes.io/docs/tasks/tools/)
- **Docker** - [Install guide](https://docs.docker.com/get-docker/)

### Python Package Manager
We recommend using `uv` for fast dependency management:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Installation

```bash
# Clone the repository
git clone https://github.com/vsbuffalo/modelops.git
cd modelops

# Install with uv (recommended)
uv pip install -e .

# Or with standard pip
pip install -e .
```

**Note**: Pulumi is installed automatically as a Python dependency - no separate installation required.

## Quick Start

### 1. Configure Azure Credentials
```bash
# Login to Azure
az login

# Set your subscription
az account set --subscription YOUR_SUBSCRIPTION_ID
```

### 2. Initialize ModelOps Configuration
```bash
# Initialize config (creates ~/.modelops/config.yaml)
mops config init

# Verify configuration
mops config
```

### 3. Create Infrastructure
```bash
# Create infrastructure.yaml
cat > infrastructure.yaml <<EOF
apiVersion: modelops/v1
kind: Infrastructure
metadata:
  name: dev
spec:
  cluster:
    provider: azure
    location: eastus2
    kubernetes_version: "1.30"
    node_count: 2
  storage:
    account_tier: Standard
  registry:
    sku: Basic
  workspace:
    workers:
      replicas: 2
      processes: 2
      threads: 1
EOF

# Provision everything
mops infra up infrastructure.yaml --env dev

# Check status
mops infra status --env dev
```

### 4. Run a Simulation
```bash
# Create a study configuration
cat > study.yaml <<EOF
apiVersion: modelops/v1
kind: Study
metadata:
  name: pi-estimation
spec:
  simulation:
    function: examples.simulations:monte_carlo_pi
    bundle_ref: ""
  parameters:
    n_samples: 100000
  replicates: 100
  seed: 42
EOF

# Submit the job
mops jobs submit study.yaml

# Check status
mops jobs list
mops jobs status <job-id>
```

### 5. Clean Up
```bash
# Destroy infrastructure (keeps data by default)
mops infra down --env dev

# Destroy everything including data
mops infra down --env dev --destroy-all --yes
```

## Documentation

- **[Quick Start Guide](docs/setup/quick-start.md)** - Detailed setup instructions
- **[CLI Reference](docs/reference/cli.md)** - Complete command documentation
- **[Developer Guide](docs/dev/README.md)** - Testing, debugging, troubleshooting
- **[Architecture](docs/architecture/)** - System design and internals

## Project Structure

```
modelops/
├── src/modelops/
│   ├── cli/              # CLI commands
│   ├── client/           # Service clients
│   ├── infra/            # Pulumi infrastructure
│   ├── services/         # Core services
│   └── worker/           # Dask worker implementation
├── docs/                 # Documentation
├── examples/             # Example simulations
└── tests/                # Test suite
```

## Development

```bash
# Run tests
make test              # Unit tests
make test-integration  # Integration tests

# Build Docker images
make build            # Build all images
make deploy           # Deploy to cluster

# Development utilities
mops dev smoke-test   # Verify bundle execution
mops dev images print --all  # Show image configuration
```

See [Developer Guide](docs/dev/README.md) for detailed development instructions.

## Architecture Overview

ModelOps uses a four-stack pattern with Pulumi:

1. **Registry Stack** - Container registry (ACR)
2. **Infrastructure Stack** - Cloud resources (AKS, networking)
3. **Workspace Stack** - Dask cluster deployment
4. **Adaptive Stack** - Optimization runs (Optuna, MCMC)

Each stack references outputs from previous stacks, enabling clean separation of concerns and independent lifecycle management.

## Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

## Related Projects

- **[modelops-contracts](https://github.com/institutefordiseasemodeling/modelops-contracts)** - Stable API contracts
- **[calabaria](https://github.com/institutefordiseasemodeling/calabaria)** - Science/algorithm framework
- **[modelops-bundle](https://github.com/institutefordiseasemodeling/modelops-bundle)** - OCI bundle packaging

## License

MIT

## Support

- **Issues**: [GitHub Issues](https://github.com/vsbuffalo/modelops/issues)
- **Discussions**: [GitHub Discussions](https://github.com/vsbuffalo/modelops/discussions)