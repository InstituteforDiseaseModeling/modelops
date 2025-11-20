# ModelOps Authentication Setup

## Overview

**Authentication is now fully automatic!** When you run `mops infra up`, all authentication is configured automatically through Pulumi. No manual steps are required.

The system handles authentication for:

1. **ACR (Container Registry)**: For pulling OCI manifests and layers
2. **Azure Blob Storage**: For accessing bundle content (via read-only SAS)

## Clean Setup Process

After spinning down all infrastructure and deleting `~/.modelops`:

### 1. Provision Infrastructure

```bash
# Set up environment
export PULUMI_CONFIG_PASSPHRASE=dev

# Provision infrastructure - this handles ALL authentication automatically
uv run mops infra up examples/unified-infra.yaml
```

That's it! Authentication is now fully configured.

### 2. Verify Authentication (Optional)

To verify authentication is working:

```bash
# Check authentication setup
uv run mops dev diagnose-auth

# Run smoke test
uv run mops dev smoke-test
```

## Authentication Architecture

### Phase 0: Static Credentials (Current MVP)
- ACR admin user enabled
- Credentials stored in K8s secret
- Environment variables:
  - `REGISTRY_USERNAME`: ACR admin username
  - `REGISTRY_PASSWORD`: ACR admin password
  - `MODELOPS_BUNDLE_REGISTRY`: Full registry URL
  - `AZURE_STORAGE_CONNECTION_STRING`: For blob access (future)

### Phase 1: Token-Based Auth (Future)
- Azure AD service principal
- Rotating tokens with refresh
- Reduced credential exposure

### Phase 2: Workload Identity (Production)
- Pod identity bound to Azure AD
- No static credentials
- Automatic token management

## Debugging

### Check Pod Environment

```bash
# Get a worker pod
POD=$(kubectl get pod -n modelops-dask-dev -l app=dask-worker -o jsonpath='{.items[0].metadata.name}')

# Check environment variables
kubectl exec $POD -n modelops-dask-dev -- env | grep -E "REGISTRY|MODELOPS|AZURE"
```

### Test ACR Authentication

```bash
kubectl exec $POD -n modelops-dask-dev -- python -c '
import os
import urllib.request
import base64

registry = os.environ["MODELOPS_BUNDLE_REGISTRY"]
username = os.environ["REGISTRY_USERNAME"]
password = os.environ["REGISTRY_PASSWORD"]

url = f"https://{registry}/v2/"
auth = base64.b64encode(f"{username}:{password}".encode()).decode()
req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})

with urllib.request.urlopen(req) as response:
    print(f"Auth successful! Status: {response.status}")
'
```

### View Logs

```bash
# Worker logs
kubectl logs -n modelops-dask-dev -l app=dask-worker -f

# With debug output
export LOG_LEVEL=DEBUG
uv run mops dev smoke-test
```

## Common Issues

### JSON Parse Error
**Symptom**: `Expecting value: line 1 column 1 (char 0)`

**Cause**: ACR returning HTML (auth error page) instead of JSON

**Fix**: Ensure credentials are properly mounted and MODELOPS_BUNDLE_REGISTRY is set

### 401 Unauthorized
**Symptom**: HTTP 401 errors in logs

**Fix**:
1. Verify ACR admin user is enabled
2. Check credentials in K8s secret
3. Ensure envFrom is configured in deployments

### Bundle Not Found
**Symptom**: 404 errors when fetching bundle

**Fix**:
1. Push the bundle: `./push-smoke-test-bundle.sh`
2. Verify it exists: `az acr repository show-tags -n <acr-name> --repository smoke-test`

## Files Created

- `setup-auth-clean.sh`: Main authentication setup script
- `push-smoke-test-bundle.sh`: Script to push test bundle to ACR
- `debug-bundle-auth.sh`: Comprehensive debugging script
- `AUTH-SETUP.md`: This documentation

## TODO

- [ ] Add auth token rotation (Phase 1)
- [ ] Implement workload identity (Phase 2)
- [ ] Add authentication to Pulumi components
- [ ] Add monitoring for auth failures