# ModelOps

[![Tests](https://github.com/institutefordiseasemodeling/modelops/actions/workflows/tests.yml/badge.svg)](https://github.com/institutefordiseasemodeling/modelops/actions/workflows/tests.yml)
[![Docker Build](https://github.com/institutefordiseasemodeling/modelops/actions/workflows/docker-build.yml/badge.svg)](https://github.com/institutefordiseasemodeling/modelops/actions/workflows/docker-build.yml)

Kubernetes-native infrastructure orchestration for distributed simulation and
calibration workloads.

## What is ModelOps?

ModelOps is the infrastructure layer for the ModelOps/Calabaria cloud
simulation-based modeling platform.

With its sibling packages like
[ModelOps-Calabaria](https://github.com/institutefordiseasemodeling/modelops-calabaria)
and
[ModelOps-Bundle](https://github.com/institutefordiseasemodeling/modelops-bundle),
ModelOps/Calabaria allows researchers to:

 1. Spin up their own (non-shared) cloud-based cluster, complete with a
    parallel simulation execution service, via a user-friendly command-line
    interface.
 2. Use ModelOps-Bundle to effortlessly and reproducibly mirror model code and
    data between the research-user's workstation and the cloud. ModelOps-Bundle
    tracks registered models and their data and code dependencies, ensuring
    that cloud execution results are fully provenanced and scientifically
    reproducible. ModelOps-Bundle is intentionally decoupled (and not a
    replacement for) version control software like Git by design. This allows
    the user to develop and refine multiple model variants in the same project
    repository without invalidating past model runs or calibrations when a
    different model's code is changed (i.e., via a model-specific dependency
    graph).

3. ModelOps-Calabaria is the science-facing interface layer, which has four key
   components:

   a. A thin, user-friendly interface that wraps *any* model, allowing it
   to run on the ModelOps platform. This interface decouples the *modeling
   interface* (i.e., running simulations) from the underlying simulation library
   (e.g., Starsim, LASER, EMOD, etc.).

   b. A common interface to a suite of available calibration algorithms that
   are plug-and-play once a model has been wrapped in Calabaria's model interface.

   c. An expressive, declarative, user-friendly interface for common model
   workflow operations, such as running different scenarios,
   reparameterizations, running models on a subset of free parameters after
   fixing others, etc.

   d. A toolkit of common modeling operations, such as generating parameter
   sweeps using quasi-random low-discrepancy sequences (e.g., Sobol sequences),
   random samples, grids, etc.

**Key ModelOps Design Features:**

- **CLI for spinning up & tearing down** all cloud infrastructure needed to run models
- **Four-stack architecture** with Pulumi for clean infrastructure management
- **OCI bundle support** for reproducible simulation code distribution (via [ModelOps-Contracts](https://github.com/institutefordiseasemodeling/modelops-contracts))
- **Warm process pools** for 16x faster simulation execution
- **Single source of truth** for Docker images and configuration
- **Azure-native** (with AWS/GCP easily addable later)

## Prerequisites

- **Python 3.11+**
- **Azure CLI** (`az`) - [Install guide](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli), used for logging in to Azure.
- **Azure subscription** - [Get free trial](https://azure.microsoft.com/free)

Additionally, developers and early alpha testers benefit from installing

- **kubectl** - [Install guide](https://kubernetes.io/docs/tasks/tools/)

since logging/job monitoring functionality is not fully built out into the
ModelOps CLI yet. Developers should also install Docker,

- **Docker** - [Install guide](https://docs.docker.com/get-docker/)

## Installation

### Quick Install (Recommended)

The fastest way to get started is with our installer script:

```bash
# Clone the repository (organization members have access)
git clone https://github.com/InstituteforDiseaseModeling/modelops.git
cd modelops
bash install.sh
```

Once the repository is public, you'll be able to install directly:

```bash
curl -sSL https://raw.githubusercontent.com/InstituteforDiseaseModeling/modelops/main/install.sh | bash
```

This installer will:
- Install `uv` (modern Python package manager) if not present
- Install the complete ModelOps suite: `mops`, `modelops-bundle`, and `cb` commands
- Configure your shell PATH (with your permission)
- Verify the installation

### Alternative: Manual Installation

If you prefer to install manually or are developing ModelOps:

```bash
# Install uv first
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install ModelOps with all components
uv tool install --python  ">=3.12"  "modelops[full]@git+https://github.com/institutefordiseasemodeling/modelops.git"
```

### For Developers

```bash
# Clone repositories
git clone https://github.com/institutefordiseasemodeling/modelops.git
cd modelops

# Install in development mode
uv pip install -e ".[full]"
```

**Note**: Pulumi is installed automatically as a Python dependency - no separate CLI installation required.

## Quick Start

### 1. Initialize Configuration

First, we need to configure ModelOps to work with your Azure environment. This
creates a config file with your resource names, cluster settings, and
deployment preferences.

```bash
# Login to Azure
az login

# Initialize ModelOps (creates unified configuration)
mops init
# Creates ~/.modelops/modelops.yaml with all settings
# Uses smart defaults - no prompts required!

# Optional: Interactive mode for customization
mops init --interactive
```

### 2. Deploy Infrastructure

Now we'll provision provision all the cloud infrastructure needed to work with
ModelOps/Calabaria (e.g. Kubernetes cluster, container registry, and storage).
This typically takes 10-15 minutes.

```bash
# Deploy (uses ~/.modelops/modelops.yaml by default)
mops infra up

# Check status
mops infra status
```

### 3. Initialize a Project

With infrastructure ready, let's set up a project for ModelOps. You can either
create a new project from scratch or add ModelOps-Bundle support to an existing
one. This is necessary to register models with ModelsOps-Bundle so they can be
run on the cloud.

```bash
# For a new project
mkdir my-simulation
cd my-simulation
mops bundle init .
```

For this tutorial, we'll work through an existing example, the Starsim SIR model:

```bash
# Navigate to the example project
cd examples/starsim-sir
mops bundle init .
# This creates pyproject.toml and .modelops-bundle/ for tracking

# Check what's being tracked
mops bundle status
```

**Note:** All commands from here on assume you're in the
`examples/starsim-sir/` directory (after you've cloned this repo with `git
clone https://github.com/institutefordiseasemodeling/modelops.git ` for the
examples).

### 4. Register Models

Model registration tells ModelOps which code and data files and model
dependencies, to make cloud execution scientifically reproducible. This design
keeps infrastructure code separate from your scientific models --- your model
stays pure Python with no cloud/execution concerns, so you can run models
locally or in a Jupyter notebook too!

```bash
# Register a model class with its dependencies
mops bundle register-model models/sir.py --no-confirm
# Auto-discovers all BaseModel subclasses in the file

# Or register specific class with data dependencies
mops bundle register-model models/sir.py --class StarsimSIR \
  --data data/demographics.csv \
  --data config/contact_matrix.csv

# Why? When ANY dependency changes, ModelOps knows to invalidate
# cached results and re-run. Your model code stays clean - no
# decorators or infrastructure imports needed!
```

### 5. Define Targets

Targets (a Calabaria feature) define how your model interfaces with observed
data. When you register a target, it enables ModelOps to automatically compute
losses when doing parameter "sweeps", and is necessary when doing model
calibrations.

Here, for the sake of the example, we'll generate synthetic observed data, then
register target functions.

```bash
# Generate synthetic observed data for testing
python generate_observed_data.py
# Creates data/observed_incidence.csv or similar

# Register target functions that compare model output to data
mops bundle register-target targets/incidence.py --no-confirm
# Targets define loss functions for calibration

# Targets can use different evaluation strategies:
# - replicate_mean_mse: Average replicates first, then compute MSE
# - mean_of_per_replicate_mse: Compute MSE per replicate, then average
```

### 6. Submit Simulation Jobs (Parameter Sampling)

Create a parameter sweep study to explore the parameter space:

```bash
# Generate Sobol sampling study (quasi-random parameter exploration)
cb sampling sobol "models/sir.py:StochasticSIR" \
  --scenario baseline \
  --n-samples 256 \
  --n-replicates 500 \
  --seed 42 \
  --scramble \
  --output study.json

# Submit job to cluster (auto-pushes bundle to registry)
mops jobs submit study.json --auto

# Monitor execution
mops jobs list          # See all jobs
mops jobs status <job-id>  # Detailed status
```

This workflow is best for **exploratory analysis** - testing many parameter combinations to understand model behavior.

### 6.5. Submit Calibration Jobs (Parameter Optimization)

Alternatively, use calibration to **find optimal parameters** that match observed data:

```bash
# Generate calibration specification (uses Optuna TPE sampler)
cb calibration optuna models.sir:StarsimSIR \
  data/observed_incidence.csv \
  beta:0.01:0.2,dur_inf:3:10 \
  --target-set incidence \
  --max-trials 100 \
  --batch-size 4 \
  --n-replicates 10 \
  --output calibration_spec.json

# Submit calibration job
mops jobs submit calibration_spec.json --target-set incidence --auto

# Monitor progress
mops jobs status <job-id>
```

After completion, view the optimal parameters found:

```bash
# Download results
mops results download <job-id> --format all

# View calibration summary
cat results/<job-id>/calibration/summary.json
```

Example output:
```json
{
  "job_id": "calib-c8af4c75",
  "algorithm": "optuna",
  "best_params": {
    "beta": 0.0816,
    "dur_inf": 4.828
  },
  "summary": {
    "n_trials": 100,
    "n_completed": 100,
    "best_value": 7.555
  }
}
```

## Quick Demo (Starsim SIR)

```shell
$ mops bundle register-model models/sir.py
+ sir_starsimsir       entry=models.sir:StarsimSIR
✓ Models updated: +1 ~0 -0

$ mops bundle register-target --regen-all targets/incidence.py
+ incidence_per_replicate_target entry=targets.incidence:incidence_per_replicate_target
+ incidence_replicate_mean_target entry=targets.incidence:incidence_replicate_mean_target
✓ Targets updated: +2 ~0 -0

$ cb sampling sobol sir_starsimsir --n-samples 1000 --name sobol --n-replicates 100
Generated 1000 Sobol samples for 2 parameters
✓ Generated SimulationStudy with 1000 parameter sets

$ mops jobs submit sobol.json
Auto-pushing bundle
✓ Job submitted successfully!
  Job ID: job-47179d43
  Environment: dev
  Status: Running
```

That is the entire workflow: register once, auto-discover outputs/targets, generate a study, and submit it.

**Key Differences:**
- **Simulation jobs**: Explore parameter space systematically (Sobol grid)
- **Calibration jobs**: Optimize parameters to match data (Optuna TPE)

### 7. Download and Analyze Results

After jobs complete, download results and generate diagnostic reports.

```bash
# Download results from cloud storage (auto-detects latest job if no ID given)
mops results download
# Downloads Parquet files to results/ directory

# Or download specific job
mops results download job-abc123

# Generate calibration diagnostics PDF report
cb diagnostics report results/views/jobs/<job-id>/targets/incidence/data.parquet
# Creates a PDF with:
# - Overview with optimum summary and loss landscape
# - 1D parameter profiles showing loss vs each parameter
# - 2D contour plots for parameter pairs

# View the PDF report
open results/views/jobs/<job-id>/targets/incidence/data_diagnostic_report.pdf
```

### 8. Clean Up

When you're done experimenting, tear down the cloud resources to avoid
unnecessary charges. This removes all Azure resources but preserves your local
configuration and code.

```bash
# Destroy infrastructure
mops infra down
```

## Documentation

- **[Quick Start Guide](docs/setup/quick_start.md)** - Detailed setup instructions
- **[CLI Reference](docs/reference/cli.md)** - Complete command documentation
- **[Developer Guide](docs/dev/readme.md)** - Testing, debugging, troubleshooting
- **[Architecture](docs/architecture/)** - System design and internals
- **[Documentation Index](docs/index.md)** - Overview of current + archived references

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

Each stack references outputs from previous stacks, enabling clean separation
of concerns and independent lifecycle management.

## Contributing & Feedback

We welcome contributions! Feel free to reach out to Vince Buffalo
(vince.buffalo@gatesfoundation.org) for tips on how to get started and where
efforts would be most helpful!

## Related Projects

- **[modelops-contracts](https://github.com/institutefordiseasemodeling/modelops-contracts)** - Stable API contracts
- **[modelops-calabaria](https://github.com/institutefordiseasemodeling/modelops-calabaria)** - Science/algorithm framework
- **[modelops-bundle](https://github.com/institutefordiseasemodeling/modelops-bundle)** - OCI bundle packaging

## License

MIT

## Support

- **Issues**: [GitHub Issues](https://github.com/institutefordiseasemodeling/modelops/issues)
- **Discussions**: [GitHub Discussions](https://github.com/institutefordiseasemodeling/modelops/discussions)
