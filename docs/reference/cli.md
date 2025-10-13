# ModelOps CLI Reference

## Overview

The `mops` command provides infrastructure orchestration for simulation-based methods. Commands are organized into logical groups for managing different aspects of the system.

## Global Options

All commands support:
- `--help` - Show help for any command

## Main Commands

### `mops infra` - Infrastructure Management (Recommended)

Unified infrastructure provisioning that orchestrates all components in correct dependency order.

#### `mops infra init`
Generate infrastructure configuration with guided setup.

```bash
mops infra init                    # Interactive mode, saves to ~/.modelops/
mops infra init --non-interactive  # Use defaults
mops infra init --output custom.yaml  # Custom location
```

**Options:**
- `--output, -o` - Custom output path (default: ~/.modelops/infrastructure.yaml)
- `--interactive/--non-interactive` - Interactive mode for selections

**Behavior:**
- Detects Azure subscriptions from `az account list`
- Prompts for subscription selection if multiple found
- Fetches latest AKS versions for selected region
- Generates complete infrastructure configuration
- Saves to ~/.modelops/infrastructure.yaml by default

#### `mops infra up [CONFIG]`
Provision infrastructure from YAML configuration.

```bash
mops infra up                      # Uses ~/.modelops/infrastructure.yaml
mops infra up infrastructure.yaml   # Custom config
mops infra up --components storage,workspace
mops infra up --env staging --verbose
```

**Arguments:**
- `CONFIG` - Infrastructure configuration file (optional, defaults to ~/.modelops/infrastructure.yaml)

**Options:**
- `--components, -c` - Specific components to provision (registry,cluster,storage,workspace)
- `--env, -e` - Environment name (dev, staging, prod)
- `--verbose, -v` - Show detailed output
- `--force, -f` - Force reprovisioning even if exists
- `--plan` - Preview changes without applying

#### `mops infra down`
Destroy infrastructure components.

```bash
mops infra down --env dev
mops infra down --env dev --destroy-storage --destroy-registry
mops infra down --env dev --destroy-all --yes
```

**Options:**
- `--env, -e` - Environment name
- `--destroy-storage` - Include storage in destruction
- `--destroy-registry` - Include registry in destruction
- `--destroy-all` - Destroy all components including data
- `--delete-rg` - Also delete resource group (dangerous!)
- `--yes, -y` - Skip confirmation prompts
- `--verbose, -v` - Show detailed output

#### `mops infra status`
Show status of all infrastructure components.

```bash
mops infra status
mops infra status --env staging
mops infra status --json
```

**Options:**
- `--env, -e` - Environment name
- `--json` - Output in JSON format

#### `mops infra outputs`
Get infrastructure outputs (endpoints, credentials, etc.).

```bash
mops infra outputs
mops infra outputs --component cluster
mops infra outputs --show-secrets
```

**Options:**
- `--env, -e` - Environment name
- `--component` - Specific component outputs
- `--show-secrets` - Show sensitive values
- `--json` - Output in JSON format

### `mops workspace` - Dask Workspace Management

Manage Dask clusters for distributed computation.

#### `mops workspace up`
Deploy Dask workspace on existing infrastructure.

```bash
mops workspace up --env dev
mops workspace up --config workspace.yaml --env dev
```

**Options:**
- `--config` - Workspace configuration file
- `--env, -e` - Environment name
- `--verbose, -v` - Show detailed output

#### `mops workspace down`
Destroy Dask workspace.

```bash
mops workspace down --env dev
mops workspace down --env dev --force
```

**Options:**
- `--env, -e` - Environment name
- `--force, -f` - Force destroy
- `--verbose, -v` - Show detailed output

#### `mops workspace status`
Check workspace status and get connection details.

```bash
mops workspace status --env dev
mops workspace status --env dev --json
```

### `mops jobs` - Job Submission

Submit and manage simulation jobs.

#### `mops jobs submit`
Submit a simulation job to the cluster.

```bash
mops jobs submit examples/study.yaml
mops jobs submit study.yaml --name my-experiment
mops jobs submit study.yaml --output-dir ./results
```

**Arguments:**
- `STUDY_FILE` - Study configuration file (YAML)

**Options:**
- `--name` - Job name (auto-generated if not provided)
- `--output-dir` - Local directory for results
- `--namespace` - Kubernetes namespace
- `--env, -e` - Environment name
- `--wait` - Wait for job completion
- `--follow, -f` - Follow job logs

#### `mops jobs list`
List all jobs.

```bash
mops jobs list
mops jobs list --namespace modelops-dev
mops jobs list --status running
```

#### `mops jobs status JOB_ID`
Get status of a specific job.

```bash
mops jobs status job-abc123
mops jobs status job-abc123 --json
```

#### `mops jobs logs JOB_ID`
View job logs.

```bash
mops jobs logs job-abc123
mops jobs logs job-abc123 --follow
mops jobs logs job-abc123 --tail 100
```

#### `mops jobs sync`
Sync job status from Kubernetes.

```bash
mops jobs sync
mops jobs sync --job-id job-abc123
```

### `mops dev` - Developer Tools

Developer utilities and testing commands.

#### `mops dev images`
Manage Docker image configuration.

```bash
# Print image references
mops dev images print scheduler
mops dev images print --all
mops dev images print --profile dev worker

# Export as environment variables
mops dev images export-env
mops dev images export-env --profile local
```

**Actions:**
- `print` - Print image reference(s)
- `export-env` - Export as shell environment variables

**Options:**
- `--profile` - Image profile (prod, dev, local)
- `--all` - Show all images

#### `mops dev smoke-test`
Run smoke test to verify bundle execution.

```bash
mops dev smoke-test
mops dev smoke-test --bundle ./my-bundle
mops dev smoke-test --registry localhost:5000
```

**Options:**
- `--bundle` - Bundle directory to test
- `--registry` - Registry URL
- `--namespace` - Kubernetes namespace
- `--verbose, -v` - Show detailed output

### `mops adaptive` - Adaptive Optimization

Manage optimization runs (Optuna, MCMC, etc.).

#### `mops adaptive up CONFIG`
Start an optimization run.

```bash
mops adaptive up optuna-config.yaml
mops adaptive up config.yaml --run-id my-calibration
```

**Arguments:**
- `CONFIG` - Adaptive configuration file

**Options:**
- `--run-id` - Run identifier (auto-generated if not provided)
- `--env, -e` - Environment name
- `--verbose, -v` - Show detailed output

#### `mops adaptive down RUN_ID`
Stop and clean up an optimization run.

```bash
mops adaptive down my-calibration
mops adaptive down my-calibration --force
```

### `mops config` - Configuration

View and manage ModelOps configuration.

```bash
mops config
mops config --json
```

Shows:
- Current environment
- Active profiles
- Configuration paths
- Provider settings

### `mops version` - Version Information

Show ModelOps version and component versions.

```bash
mops version
mops version --json
```

## Component-Specific Commands (Advanced)

These commands manage individual components. For most use cases, use `mops infra` instead.

### `mops cluster` - Kubernetes Cluster
- `up` - Create AKS cluster
- `down` - Destroy cluster
- `status` - Check cluster status

### `mops registry` - Container Registry
- `create` - Create ACR registry
- `destroy` - Destroy registry
- `status` - Check registry status
- `login` - Authenticate to registry

### `mops storage` - Blob Storage
- `up` - Create storage account
- `down` - Destroy storage
- `status` - Check storage status

## Environment Variables

- `MODELOPS_ENV` - Default environment (dev, staging, prod)
- `MOPS_IMAGE_PROFILE` - Docker image profile (prod, dev, local)
- `PULUMI_CONFIG_PASSPHRASE_FILE` - Pulumi passphrase file location
- `MODELOPS_BUNDLE_REGISTRY` - Bundle registry URL
- `AZURE_SUBSCRIPTION_ID` - Azure subscription for resources

## Configuration Files

### Infrastructure Configuration (`infrastructure.yaml`)

```yaml
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
    account_kind: StorageV2
  registry:
    sku: Basic
  workspace:
    workers:
      replicas: 4
      processes: 2
      threads: 1
```

### Study Configuration (`study.yaml`)

```yaml
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
```

## Examples

### Complete Infrastructure Setup

```bash
# 1. Create infrastructure
mops infra up infrastructure.yaml --env dev

# 2. Check status
mops infra status --env dev

# 3. Deploy workspace
mops workspace up --env dev

# 4. Submit a job
mops jobs submit study.yaml

# 5. Check results
mops jobs status <job-id>
mops jobs logs <job-id>

# 6. Clean up
mops workspace down --env dev
mops infra down --env dev --yes
```

### Development Workflow

```bash
# Use dev profile for images
export MOPS_IMAGE_PROFILE=dev

# Check image configuration
mops dev images print --all

# Run smoke test
mops dev smoke-test

# View logs
kubectl logs -n modelops-dask-dev -l app=dask-worker
```

## Exit Codes

- `0` - Success
- `1` - General error
- `2` - Configuration error
- `3` - Infrastructure error
- `130` - Interrupted (Ctrl+C)