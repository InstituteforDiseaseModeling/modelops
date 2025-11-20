# ModelOps Image Pipeline Setup

## Current Configuration

### Development Setup (vsbuffalo)
- **Registry**: `ghcr.io/vsbuffalo/modelops-dask-*`
- **Repository**: `github.com/vsbuffalo/modelops`
- **Visibility**: Public packages
- **CI/CD**: GitHub Actions in vsbuffalo/modelops

### Production Setup (InstituteforDiseaseModeling)
- **Registry**: `ghcr.io/institutefordiseasemodeling/modelops-dask-*`
- **Repository**: Will push to `github.com/InstituteforDiseaseModeling/modelops`
- **Visibility**: Public packages
- **CI/CD**: GitHub Actions in IDM repo

## Configuration Files

### Makefile
```makefile
# Development registry (vsbuffalo for dev, institutefordiseasemodeling for production)
REGISTRY ?= ghcr.io
# ORG ?= institutefordiseasemodeling  # Production - uncomment when ready to deploy to IDM
ORG ?= vsbuffalo  # Development - comment out when deploying to production
```

### GitHub Actions (.github/workflows/docker-build.yml)
```yaml
env:
  REGISTRY: ghcr.io
  # ORG: institutefordiseasemodeling  # Production - uncomment for IDM deployment
  ORG: vsbuffalo  # Development - comment out for production
```

## Switching Between Development and Production

### To Deploy to Development (vsbuffalo):
1. Ensure Makefile has `ORG ?= vsbuffalo` uncommented
2. Ensure GitHub Actions has `ORG: vsbuffalo` uncommented
3. Push to `vsbuffalo/modelops` repository
4. Deploy with: `make rollout-images`

### To Deploy to Production (IDM):
1. Update Makefile: Comment out vsbuffalo line, uncomment IDM line
2. Update GitHub Actions: Comment out vsbuffalo line, uncomment IDM line
3. Add IDM remote if not present: `git remote add idm git@github.com:InstituteforDiseaseModeling/modelops.git`
4. Push to IDM: `git push idm main`
5. Deploy with: `make rollout-images`

## Image Verification

### Verify Deployment Script
Use `scripts/verify-deployment.sh` to verify what's deployed:
```bash
./scripts/verify-deployment.sh [namespace]
```

This script will show:
- Current image tags and SHA256 digests
- VERSION file contents (if present in new builds)
- Code verification (checks for specific fixes)
- Git commit info

### VERSION File
New images include `/app/VERSION` with:
- `GIT_SHA`: The commit SHA the image was built from
- `BUILD_DATE`: When the image was built
- `PYTHON_VERSION`: Python version in the image

## Updated rollout-images Target

The Makefile's `rollout-images` target now:
1. Updates deployments to use `:latest` tag
2. Sets `imagePullPolicy: Always` to force fresh pulls
3. Deletes pods to force immediate re-pull
4. Waits for rollout completion

## Troubleshooting

### Issue: Old images still being used
- Run `scripts/verify-deployment.sh` to check what's actually deployed
- Check the image SHA256 to ensure it's different
- Verify CI has run and completed successfully
- Ensure you're pulling from the correct registry (vsbuffalo vs IDM)

### Issue: ImagePullBackOff errors
- For private repos: Create image pull secret
- For public repos: Ensure packages are set to public visibility
- Check the image exists in the registry

### Issue: Code changes not reflected in running pods
1. Verify the commit was pushed to the correct remote
2. Check CI/CD ran successfully
3. Verify new image was built (check GitHub Actions)
4. Run `make rollout-images` to force update
5. Use `scripts/verify-deployment.sh` to confirm deployment