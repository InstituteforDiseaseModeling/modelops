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

**Note**: Pulumi is installed automatically as a Python dependency via the Automation API - no separate Pulumi CLI installation required.

## Prerequisites

- **Python 3.11+**
- **Azure CLI** (`az`) - [Install guide](https://aka.ms/azure-cli)
- **kubectl** - [Install guide](https://kubernetes.io/docs/tasks/tools/)
- **Docker** - [Install guide](https://docs.docker.com/get-docker/)
- **Azure subscription** - [Get free trial](https://azure.microsoft.com/free)

## Quick Start

### 1. Install ModelOps
```bash
git clone https://github.com/vsbuffalo/modelops.git
cd modelops
pip install -e .
```

### 2. Initialize Configuration
```bash
# Login to Azure
az login

# Initialize ModelOps config
mops config init

# Generate infrastructure configuration (interactive)
mops infra init
# Creates ~/.modelops/infrastructure.yaml with your Azure subscription
```

### 3. Deploy Infrastructure
```bash
# Deploy (uses ~/.modelops/infrastructure.yaml by default)
mops infra up

# Check status
mops infra status
```

### 4. Run a Simulation
```bash
# Install Calabaria for experiment design (optional)
pip install modelops-calabaria

# Generate study with Sobol sampling
cb sampling sobol models.example:SimpleModel \
  --n-samples 20 \
  --n-replicates 3 \
  --output study.json

# Submit to cluster
mops jobs submit study.json

# Monitor jobs
mops jobs sync    # Sync status from Kubernetes
mops jobs list    # List all jobs with status

# Example output:
#                     Recent Jobs (last 24 hours, PDT)
# ┏━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
# ┃ Job ID       ┃ Status    ┃ Progress ┃ Created        ┃ Updated        ┃
# ┡━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
# │ job-5b718dc8 │ running   │ -        │ 7 minutes ago  │ just now       │
# │ job-e6169049 │ failed    │ -        │ 11 minutes ago │ just now       │
# │ job-267aa463 │ failed    │ -        │ 13 minutes ago │ just now       │
# └──────────────┴───────────┴──────────┴────────────────┴────────────────┘

# Get detailed status
mops jobs status <job-id>
```

### 5. Clean Up
```bash
# Destroy infrastructure
mops infra down
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

### Testing Without Credentials

To test infrastructure commands without Azure credentials (e.g., in CI):

```bash
# Use --plan flag to preview without creating resources
mops infra up infrastructure.yaml --plan

# Test CLI commands that don't require cloud access
mops config init
mops version
mops dev images print --all
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