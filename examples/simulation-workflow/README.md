# K8s Job Submission Workflow Example

This example demonstrates the complete workflow for submitting simulation jobs to Kubernetes using ModelOps.

## Testing Approach

This workflow uses a **user simulation environment** for testing:

- **`.venv/` directory**: Contains a dedicated virtual environment that simulates a user's setup
- **User tools**: `modelops-calabaria` and `modelops-bundle` are installed in this venv (like a user would)
- **ModelOps testing**: The `mops` command runs from source via `uv run --package modelops` for testing

This approach ensures we're testing ModelOps in a realistic user environment while keeping the development setup clean.

## Prerequisites

1. Infrastructure deployed: `mops infra status` shows resources ready
2. Workspace running: `mops workspace status` shows Dask cluster running
3. Registry access configured

## Setup

Create the user-like environment:

```bash
make setup
```

This creates `.venv/` and installs modelops-calabaria and modelops-bundle as a user would.

## Quick Start

Run the complete workflow:

```bash
make workflow
```

This will:
1. Initialize bundle tracking
2. Push the SEIR model to the registry
3. Generate Sobol parameter samples
4. Submit the job to Kubernetes

## Step-by-Step

### 1. Initialize Bundle Tracking
```bash
make bundle-init
```

### 2. Push Model to Registry
```bash
make bundle-push
```

### 3. Generate Parameter Samples
```bash
make study
```

This uses Calabaria's Sobol sampler to generate 20 parameter sets.

### 4. Submit Job
```bash
make submit
```

Submits the study to Kubernetes for distributed execution.

## Monitor Progress

Check job status:
```bash
make status
```

View logs:
```bash
make logs
```

## Files

- `models/seir.py` - Minimal stochastic SEIR model
- `study.json` - Generated parameter samples (git-ignored)
- `Makefile` - User workflow commands