# Troubleshooting: The Image Deployment Madness (January 2025)

## The Problem

"I ran `make deploy` but my fixes aren't working in the cluster!"

This document captures a frustrating debugging session where worker pods kept failing with git authentication errors despite multiple attempts to fix and deploy the code.

## The Symptoms

1. Worker pods failing with: `fatal: could not read Username for 'https://github.com': terminal prompts disabled`
2. Jobs "completing" but returning empty results: `Task 0: []`
3. `make deploy` appearing to work but pods still running old code

## The Root Causes

### 1. Environment Variable Stripping Bug

The subprocess_runner.py had code that was stripping environment variables when running subprocesses:

```python
# BAD: This removes GITHUB_TOKEN!
env = {k: v for k, v in os.environ.items()
       if not (k.startswith("UV_") or k.startswith("PIP_"))}
```

Fixed to:
```python
# GOOD: Preserves all env vars including GITHUB_TOKEN
env = {**os.environ, "PYTHONNOUSERSITE": "1"}
```

### 2. Kubernetes Image Caching

Even with `imagePullPolicy: Always`, Kubernetes doesn't pull new images for EXISTING pods. The `:latest` tag is mutable, but running pods keep using their cached version.

**The insidious timeline:**
1. Build and push new image with fix → `:latest` tag updated in registry ✅
2. Run `kubectl rollout restart` → Deployment "restarts" ✅
3. But pods might not actually recreate if Kubernetes thinks nothing changed ❌
4. Old pods keep running with old cached image ❌

### 3. Environment Variable Name Mismatch

Initially thought this was the issue but turned out to be fine:
- Kubernetes secret provides: `GIT_PASSWORD`, `GIT_USERNAME`, and `GITHUB_TOKEN`
- Our code expected: `GITHUB_TOKEN`
- Actually all three were present, so this wasn't the problem

## How We Diagnosed It

### Step 1: Check what's in the worker pods
```bash
# Check environment variables
kubectl exec -it deployment/dask-workers -n modelops-dask-dev -- env | grep -E 'GIT|GITHUB'
# Result: GITHUB_TOKEN=ghp_xxx (token WAS present!)

# Check the actual code in the pod
kubectl exec deployment/dask-workers -n modelops-dask-dev -- \
  grep -A5 "def _run" /usr/local/lib/python3.13/site-packages/modelops/worker/subprocess_runner.py
# Result: Found OLD code that strips env vars!
```

### Step 2: Verify local code is fixed
```bash
grep -A5 "def _run" /Users/vsb/projects/work/modelops/src/modelops/worker/subprocess_runner.py
# Result: Fixed code that preserves env vars
```

### Step 3: Check image details
```bash
# When was the local image built?
docker images | grep worker
# Result: 3 hours ago (should be recent)

# What image is the deployment using?
kubectl get deployment dask-workers -n modelops-dask-dev -o jsonpath='{.spec.template.spec.containers[0].image}'
# Result: ghcr.io/institutefordiseasemodeling/modelops-dask-worker:latest
```

## The Fix

### Immediate Fix (What Worked)
Force delete pods to make Kubernetes pull the fresh image:

```bash
# Nuclear option: force delete all worker pods
kubectl delete pods -n modelops-dask-dev -l app=dask-worker --force --grace-period=0

# Wait for new pods
sleep 10 && kubectl get pods -n modelops-dask-dev -l app=dask-worker

# Verify new pods have fixed code
kubectl exec deployment/dask-workers -n modelops-dask-dev -- \
  grep -A3 "def _run" /usr/local/lib/python3.13/site-packages/modelops/worker/subprocess_runner.py
# Should see: env = {**os.environ, "PYTHONNOUSERSITE": "1"}
```

### Long-term Fix (Prevent This Madness)

Use image digests instead of tags. The `:latest` tag is a lie - it's mutable and cached. Digests are immutable content addresses.

#### Proposed Makefile improvements:

```makefile
# Build and capture digest
BUILD_DIR := .build
$(BUILD_DIR):
    @mkdir -p $(BUILD_DIR)

build-worker: setup-buildx ghcr-login | $(BUILD_DIR)
    @docker buildx build \
        --platform linux/amd64 \
        --no-cache \
        --iidfile $(BUILD_DIR)/worker.iid \
        --push \
        ...
    @sed 's|docker-image://||' $(BUILD_DIR)/worker.iid > $(BUILD_DIR)/worker.digest
    @echo "✓ Worker pushed: $(WORKER_IMAGE)@$$(cat $(BUILD_DIR)/worker.digest)"

# Deploy by digest, not tag
deploy-by-digest:
    kubectl set image deployment/dask-workers \
        worker=$(WORKER_IMAGE)@$$(cat $(BUILD_DIR)/worker.digest) \
        -n $(NAMESPACE)
    kubectl rollout status deployment/dask-workers -n $(NAMESPACE)

# Verify deployment
verify-deploy:
    @WANT=$$(cat $(BUILD_DIR)/worker.digest); \
    GOT=$$(kubectl get pods -l app=dask-worker -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' | sed 's|.*@||'); \
    if [ "$$WANT" != "$$GOT" ]; then echo "❌ Digest mismatch!"; exit 1; fi
```

## Lessons Learned

1. **`:latest` is a trap** - It's mutable and heavily cached. Use digests or versioned tags.

2. **`kubectl rollout restart` isn't enough** - It doesn't guarantee new image pulls for `:latest` tags.

3. **Always verify the actual running code** - Don't trust that deploy worked. Exec into pods and check.

4. **Pulumi can revert manual changes** - If Pulumi manages your deployments, manual `kubectl` edits may get reverted on next `pulumi up`.

5. **Add smoke tests** - Test that your fix is actually in the built image:
   ```bash
   docker run --rm $(WORKER_IMAGE):$(TAG) python -c \
     "import inspect; from modelops.worker import subprocess_runner; \
      assert 'PYTHONNOUSERSITE' in inspect.getsource(subprocess_runner.SubprocessRunner._run)"
   ```

## The Full Diagnosis Checklist

When "it's still not working after deploy":

1. **Check the pod's environment**: `kubectl exec -it <pod> -- env | grep <VAR>`
2. **Check the actual code in the pod**: `kubectl exec <pod> -- grep -A5 <function> <file>`
3. **Check what image the pod is running**: `kubectl describe pod <pod> | grep "Image:"`
4. **Check the image digest**: `kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[0].imageID}'`
5. **Force recreate if needed**: `kubectl delete pod <pod> --force --grace-period=0`
6. **Verify after recreate**: Repeat steps 1-4

## Prevention

The core issue is that **builds aren't atomic with deploys**. You can build the right thing but deploy the wrong thing. The solution is to make them atomic:

1. Build produces a digest
2. Deploy uses that exact digest
3. Verify the cluster runs that exact digest
4. Fail loudly if any step doesn't match

This ensures `make deploy` either succeeds completely or fails visibly - no silent failures where old code keeps running.