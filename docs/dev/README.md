# ModelOps Developer Guide

This guide consolidates developer documentation for ModelOps. For architecture details, see [docs/architecture/](../architecture/).

## Table of Contents
- [Testing](#testing)
- [Image Management](#image-management)
- [Common Issues & Fixes](#common-issues--fixes)
- [Debugging Commands](#debugging-commands)
- [Building & Deploying](#building--deploying)

## Testing

### Running Tests

```bash
# Unit tests (default, fast ~10-20s)
make test
# or
uv run pytest

# Integration tests (creates LocalCluster instances)
make test-integration

# Run specific test file
uv run pytest tests/test_component_dependencies.py

# Run specific test function
uv run pytest tests/test_dask_serialization.py::test_cloudpickle_simtask

# Run with coverage
uv run pytest --cov=modelops --cov-report=html
```

### Using External Dask for Debugging

By default, integration tests create their own LocalCluster. To use an external cluster:

```bash
# Start external Dask cluster
make dask-local

# Use external cluster (must explicitly opt-in)
DASK_ADDRESS=tcp://localhost:8786 make test-integration
# or
make test-integration-external  # uses --dask-address flag

# Stop when done
make dask-stop
```

### CI Behavior
- **Resource Scaling**: CI uses 1 worker with 1GB memory (vs 2 workers, 2GB locally)
- **Timeouts**: 60-second per test, 10-minute overall
- **Auto-skip**: Tests skip gracefully when resources are constrained

## Image Management

### Single Source of Truth

All Docker image references are centralized in `modelops-images.yaml`:

```yaml
profiles:
  prod:
    registry: {host: ghcr.io, org: institutefordiseasemodeling}
    default_tag: latest
  dev:
    registry: {host: ghcr.io, org: institutefordiseasemodeling}
    default_tag: dev

images:
  scheduler: {name: modelops-dask-scheduler}
  worker: {name: modelops-dask-worker}
  runner: {name: modelops-dask-runner}
```

### Using Image Configuration

```bash
# CLI access to image config
mops dev images print scheduler     # Single image
mops dev images print --all         # All images
mops dev images export-env          # Export as env vars

# In Python code
from modelops.images import get_image_config
config = get_image_config()
worker_image = config.worker_image()  # ghcr.io/institutefordiseasemodeling/modelops-dask-worker:latest

# In Makefile
WORKER_IMAGE := $(shell uv run mops dev images print worker)
```

### Digest-Based Deployment (Preventing Cache Issues)

The `:latest` tag is mutable and heavily cached by Kubernetes. Use digests for reliable deployments:

```bash
# Build and capture digest
make build-worker
# Stores digest in .build/worker.digest

# Deploy by digest (not tag)
kubectl set image deployment/dask-workers \
  worker=$(WORKER_IMAGE)@$(cat .build/worker.digest) \
  -n modelops-dask-dev

# Verify deployment
kubectl get pods -l app=dask-worker -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'
```

## Common Issues & Fixes

### Pulumi Passphrase Errors

**Error**: "incorrect passphrase" when accessing Pulumi stacks

**Root Cause**: `PULUMI_CONFIG_PASSPHRASE_FILE` not passed to subprocess

**Fix**: Ensure `env_vars=dict(os.environ)` in `src/modelops/core/automation.py:workspace_options()`

```python
# CRITICAL: Pass full environment to subprocess
return auto.LocalWorkspaceOptions(
    env_vars=dict(os.environ)  # Must pass environment
)
```

### Bundle Registry Authentication

**Error**: "Expecting value: line 1 column 1 (char 0)" when fetching bundles

**Root Cause**: ACR returning HTML login page instead of JSON

**Common Causes**:
1. Repository name mismatch (e.g., pushing to `smoke_bundle`, pulling from `modelops-bundles`)
2. Bundle reference format inconsistency (need `repository@sha256:digest`)
3. Wrong registry URL in environment

**Fix**: Ensure consistent repository naming and format:
```python
# Correct format
bundle_ref = "smoke_bundle@sha256:abc123..."
MODELOPS_BUNDLE_REGISTRY = "modelopsdevacrvsb.azurecr.io"  # No repository path
```

### Kubernetes Using Stale Images

**Symptom**: Fixes aren't working despite `make deploy`

**Root Cause**: Kubernetes caches `:latest` tags aggressively

**Quick Fix**:
```bash
# Force delete pods to pull fresh images
kubectl delete pods -n modelops-dask-dev -l app=dask-worker --force --grace-period=0

# Verify new code is running
kubectl exec deployment/dask-workers -n modelops-dask-dev -- \
  grep -A3 "your_function" /path/to/file.py
```

**Better Fix**: Use digest-based deployment (see above)

### Dask Fixture Timeouts

**Error**: Integration tests hang for 30+ seconds

**Root Cause**: Tests trying to connect to external Dask before creating LocalCluster

**Fix**: Default to LocalCluster (already fixed in conftest.py):
```python
# Tests now create LocalCluster by default
# Must explicitly opt-in to external with --dask-address or DASK_ADDRESS
```

## Debugging Commands

### Check Pod Status and Logs

```bash
# List pods
kubectl get pods -n modelops-dask-dev

# Check pod details
kubectl describe pod <pod-name> -n modelops-dask-dev

# View logs
kubectl logs -n modelops-dask-dev -l app=dask-scheduler
kubectl logs -n modelops-dask-dev -l app=dask-worker --tail=50

# Follow logs
kubectl logs -f deployment/dask-workers -n modelops-dask-dev
```

### Port Forwarding

```bash
# Dask scheduler
kubectl port-forward -n modelops-dask-dev svc/dask-scheduler 8786:8786

# Dask dashboard
kubectl port-forward -n modelops-dask-dev svc/dask-scheduler 8787:8787

# Multiple ports
kubectl port-forward -n modelops-dask-dev svc/dask-scheduler 8786:8786 8787:8787
```

### Verify Deployments

```bash
# Check what image a pod is actually running
kubectl get pod <pod-name> -o jsonpath='{.status.containerStatuses[0].imageID}'

# Check environment variables
kubectl exec -it deployment/dask-workers -n modelops-dask-dev -- env | grep MODELOPS

# Check actual code in pod
kubectl exec deployment/dask-workers -n modelops-dask-dev -- \
  cat /usr/local/lib/python3.13/site-packages/modelops/__version__.py

# Force rollout restart
kubectl rollout restart deployment/dask-workers -n modelops-dask-dev
kubectl rollout status deployment/dask-workers -n modelops-dask-dev
```

### Pulumi State Inspection

```bash
# Check stack outputs
pulumi stack output --stack modelops-infra-dev

# List all stacks
pulumi stack ls

# Check specific output
pulumi stack output kubeconfig --stack modelops-infra-dev

# Show full stack state (verbose)
pulumi stack export --stack modelops-infra-dev | jq .
```

## Building & Deploying

### GitHub Actions Workflow

Images are automatically built on push to main:
- Triggered by `.github/workflows/docker-build.yml`
- Pushes to `ghcr.io/institutefordiseasemodeling/`
- Tagged with commit SHA and `latest`

### Local Development Build

```bash
# Build all images
make build  # Builds scheduler, worker, runner

# Build specific image
make build-worker
make build-scheduler
make build-runner

# Push to registry (after building)
make push

# Pull latest from registry
make pull-latest

# Full deployment cycle
make build push deploy verify-deploy
```

### Deployment Verification

Always verify deployments actually worked:

```bash
# Custom verification command
make verify-deploy

# Manual verification
kubectl get pods -n modelops-dask-dev
kubectl logs -n modelops-dask-dev -l app=dask-worker --tail=10

# Run smoke test
mops dev smoke-test
```

## Additional Resources

- [Architecture Documentation](../architecture/)
- [Python 3.13 JSON Bug](python-3.13-json-unboundlocalerror.md)
- [Test Suite Documentation](../../tests/README.md)
- [Main README](../../README.md)

## Tips

1. **Always verify deployments** - Don't trust that `make deploy` worked
2. **Use digests for production** - Tags are mutable and cached
3. **Check environment variables** - Many issues are missing env vars
4. **Force delete pods when in doubt** - Kubernetes caching is aggressive
5. **Review the image config** - Single source of truth in `modelops-images.yaml`