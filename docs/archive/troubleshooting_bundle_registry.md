# Troubleshooting OCI Bundle Registry Authentication

## Issue Summary

The smoke test was failing with "Expecting value: line 1 column 1 (char 0)" when workers tried to fetch OCI bundles from Azure Container Registry (ACR). This error indicated ACR was returning an HTML login page instead of JSON API responses.

## Root Causes

### 1. Repository Name Mismatch
- **Problem**: Smoke test pushed bundles to `smoke_bundle` repository but workers looked in `modelops-bundles`
- **Fix**: Ensured consistent repository naming across push and fetch operations

### 2. Bundle Reference Format Inconsistency
- **Problem**: System components had different expectations for bundle reference format
  - ModelOpsBundleRepository expected: `sha256:...` (digest only)
  - System needed: `repository@sha256:...` (repository + digest)
- **Fix**: Updated all components to use `repository@sha256:...` format consistently

### 3. Stale Docker Images in Cluster
- **Problem**: Kubernetes deployments used cached images with tag `88a216e` instead of newly built `latest` images
- **Symptoms**:
  - `make build` created new images locally
  - `make rollout-images` restarted pods but didn't pull new images
  - Cluster kept using old cached versions without the fixes

## Solutions Applied

### Code Fixes

1. **ModelOpsBundleRepository** (`modelops-bundle/src/modelops_bundle/repository.py`):
   ```python
   # Now accepts repository@sha256:digest format
   if "@" in bundle_ref:
       repository, digest_part = bundle_ref.split("@", 1)
       # Use repository-specific registry URL
       effective_registry = f"{self.registry_ref}/{repository}"
   ```

2. **SimTask Validation** (`modelops-contracts/src/modelops_contracts/simulation.py`):
   ```python
   # Updated to accept both formats:
   # - sha256:64-hex-chars
   # - repository@sha256:64-hex-chars
   ```

3. **Smoke Test** (`modelops/src/modelops/cli/dev.py`):
   ```python
   # Include repository in bundle_ref
   digest = push_bundle(bundle_dir, registry)
   bundle_ref = f"smoke_bundle@{digest}"
   ```

### Deployment Fixes

1. Force fresh image pulls:
   ```bash
   # Update to latest tag
   kubectl set image deployment/dask-workers worker=ghcr.io/.../modelops-dask-worker:latest -n modelops-dask-dev

   # Delete pods to force repull
   kubectl delete pods -l app=dask-worker -n modelops-dask-dev
   kubectl delete pods -l app=dask-scheduler -n modelops-dask-dev
   ```

## Preventing Future Image Cache Issues

### 1. Use Unique Tags for Each Build
```bash
# Build with git SHA tag
make build TAG=$(git rev-parse --short HEAD)
make push TAG=$(git rev-parse --short HEAD)

# Deploy specific version
kubectl set image deployment/dask-workers \
  worker=ghcr.io/institutefordiseasemodeling/modelops-dask-worker:$(git rev-parse --short HEAD) \
  -n modelops-dask-dev
```

### 2. Ensure imagePullPolicy is Always
```yaml
spec:
  containers:
  - name: worker
    image: ghcr.io/institutefordiseasemodeling/modelops-dask-worker:latest
    imagePullPolicy: Always  # Forces fresh pull even for :latest
```

### 3. Create a Deploy-Fresh Makefile Target
```makefile
# Add to Makefile
deploy-fresh: build push
	@echo "Deploying fresh images with version: $(VERSION)"
	kubectl set image deployment/dask-workers worker=$(WORKER_IMAGE):$(VERSION) -n $(NAMESPACE)
	kubectl set image deployment/dask-scheduler scheduler=$(SCHEDULER_IMAGE):$(VERSION) -n $(NAMESPACE)
	kubectl delete pods -l app=dask-worker -n $(NAMESPACE)
	kubectl delete pods -l app=dask-scheduler -n $(NAMESPACE)
	kubectl wait --for=condition=Ready pod -l app=dask-worker -n $(NAMESPACE) --timeout=60s
	@echo "✓ Fresh images deployed successfully"
```

### 4. Verify Deployed Images
Always verify the actual images running in the cluster:
```bash
# Check current image
kubectl get deployment dask-workers -n modelops-dask-dev \
  -o jsonpath='{.spec.template.spec.containers[0].image}'

# Check image digest on pod
kubectl get pods -n modelops-dask-dev -o json | \
  jq '.items[].status.containerStatuses[].imageID'
```

### 5. Use Image Digest for Production
For production deployments, use image digests instead of tags:
```bash
# Get digest from push output
docker push ghcr.io/org/image:tag
# Note the digest: sha256:abc123...

# Deploy using digest (immutable reference)
kubectl set image deployment/dask-workers \
  worker=ghcr.io/org/image@sha256:abc123...
```

## Key Lessons

1. **Image caching is aggressive in Kubernetes** - Even with `imagePullPolicy: Always`, if the deployment spec doesn't change, pods won't be recreated
2. **Use consistent reference formats** - Ensure all components agree on how to reference bundles (with or without repository)
3. **Test the full pipeline** - Issues can arise from mismatches between push location and fetch location
4. **Verify actual deployed versions** - Don't assume `make rollout-images` updated the images; always verify

## Debug Checklist

When debugging similar issues:

- [ ] Check actual image being used: `kubectl get deployment -o jsonpath='{.spec.template.spec.containers[0].image}'`
- [ ] Verify environment variables in pods: `kubectl exec deployment/name -- env | grep REGISTRY`
- [ ] Test authentication directly: `kubectl exec deployment/name -- curl -H "Authorization: Bearer $TOKEN" https://registry/v2/`
- [ ] Check if bundle exists in expected repository: `az acr repository show-tags --name registry --repository repo`
- [ ] Force fresh pull by deleting pods: `kubectl delete pods -l app=name`
- [ ] Review recent image builds: `docker images --format "table {{.Repository}}:{{.Tag}}\t{{.CreatedAt}}"`