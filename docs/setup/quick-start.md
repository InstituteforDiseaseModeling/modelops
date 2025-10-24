# ModelOps Quick Start Guide

This guide walks through setting up ModelOps from scratch to running your first distributed simulation.

## Prerequisites

Before starting, ensure you have:

- **Python 3.11 or later** - Required for ModelOps
- **Azure subscription** - [Get a free trial](https://azure.microsoft.com/free) if you don't have one
- **Azure CLI** - [Installation guide](https://aka.ms/azure-cli)
- **kubectl** - [Installation guide](https://kubernetes.io/docs/tasks/tools/)
- **Docker** - [Installation guide](https://docs.docker.com/get-docker/)

## Step 1: Install ModelOps

Clone the repository and install ModelOps in development mode:

```bash
git clone https://github.com/institutefordiseasemodeling/modelops.git
cd modelops
pip install -e .
```

Alternatively, if you have `uv` installed (recommended for faster installs):
```bash
uv pip install -e .
```

## Step 2: Configure Azure Access

Login to Azure and verify your subscription:

```bash
# Login to Azure
az login

# View your subscriptions
az account list --output table

# (Optional) Set default subscription
az account set --subscription YOUR_SUBSCRIPTION_ID
```

## Step 3: Initialize ModelOps

Initialize the ModelOps configuration and generate infrastructure template:

```bash
# Create ModelOps configuration
mops config init

# Generate infrastructure configuration (interactive)
mops infra init
```

The `infra init` command will:
1. List your available Azure subscriptions
2. Let you select which subscription to use
3. Ask for your preferred Azure region (default: eastus2)
4. Detect the latest AKS version for that region
5. Generate `~/.modelops/infrastructure.yaml` with all settings

## Step 4: Deploy Infrastructure

Deploy all infrastructure components with a single command:

```bash
# Deploy infrastructure (uses ~/.modelops/infrastructure.yaml automatically)
mops infra up

# Monitor deployment progress
mops infra status
```

This creates:
- Azure Resource Group
- Azure Container Registry (ACR) for Docker images
- Azure Kubernetes Service (AKS) cluster
- Azure Storage Account for results
- Dask workspace for distributed computing

Deployment typically takes 5-10 minutes.

## Step 5: Run Your First Simulation

### Option A: Using Calabaria (Recommended)

Install Calabaria for scientific experiment design:

```bash
pip install modelops-calabaria
```

Generate a parameter study using Sobol sampling:

```bash
# Generate study with 20 parameter sets, 3 replicates each
cb sampling sobol models.example:SimpleModel \
  --n-samples 20 \
  --n-replicates 3 \
  --output study.json

# Submit to cluster
mops jobs submit study.json

# Monitor progress
mops jobs list
mops jobs status <job-id>
```

### Option B: Manual Study Creation

Create a study configuration manually:

```bash
cat > study.json <<EOF
{
  "model": "examples.simulations:monte_carlo_pi",
  "scenario": "baseline",
  "parameter_sets": [
    {"n_samples": 100000},
    {"n_samples": 200000},
    {"n_samples": 500000}
  ],
  "n_replicates": 5
}
EOF

# Submit job
mops jobs submit study.json
```

## Step 6: Monitor and Retrieve Results

Check job status and logs:

```bash
# List all jobs
mops jobs list

# Get detailed status
mops jobs status <job-id>

# View logs
mops jobs logs <job-id> --follow

# Sync results locally (when complete)
mops jobs sync <job-id> --output ./results
```

## Step 7: Clean Up Resources

When finished, destroy the infrastructure to avoid charges:

```bash
# Destroy infrastructure (keeps data by default)
mops infra down

# Or destroy everything including stored data
mops infra down --destroy-all --yes
```

## Troubleshooting

### Azure Login Issues

If you encounter authentication problems:

```bash
# Clear cached credentials
az logout
az login --use-device-code

# Verify access
az account show
```

### Subscription Access

Verify you have access to your subscription:

```bash
# List all accessible subscriptions
az account list --output table

# Check current subscription
az account show --query '{name:name, id:id}'
```

### Infrastructure Deployment Issues

Get detailed status and logs:

```bash
# Detailed infrastructure status
mops infra status --verbose

# Check Pulumi state directly
pulumi stack --stack modelops-infra-dev

# Force refresh of status
mops infra status --refresh
```

### Kubernetes Connection

If you can't connect to the cluster:

```bash
# Update kubeconfig
mops infra outputs cluster --show-secrets | grep kubeconfig

# Or get credentials directly from Azure
az aks get-credentials --resource-group modelops-<username> --name modelops-cluster
```

## Next Steps

- Read the [Architecture Overview](../architecture/README.md) to understand the system design
- Explore [Advanced Configuration](./advanced-config.md) for customizing your deployment
- Check out the [Developer Guide](../dev/README.md) for contributing to ModelOps
- Learn about [Bundle Management](../bundles/README.md) for packaging your simulation code

## Getting Help

- **Documentation**: [docs/](../)
- **Issues**: [GitHub Issues](https://github.com/institutefordiseasemodeling/modelops/issues)
- **Discussions**: [GitHub Discussions](https://github.com/institutefordiseasemodeling/modelops/discussions)