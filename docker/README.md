# ModelOps Docker Images

This directory contains Docker images for ModelOps MVP deployment.

## Architecture

We use a simple, local-copy approach for including private dependencies:
- **modelops-contracts** and **calabaria** are copied from local directories during build
- This avoids complexity with GitHub PATs and private registries for MVP
- Production deployments can use proper private registry authentication

## Images

### dask-scheduler
The Dask scheduler with ModelOps dependencies:
- Base: `ghcr.io/dask/dask:2024.8.0-py3.11`
- Includes: modelops, modelops-contracts, calabaria
- Exposes: ports 8786 (scheduler) and 8787 (dashboard)

### dask-worker
The Dask worker with ModelOps dependencies:
- Base: `ghcr.io/dask/dask:2024.8.0-py3.11`
- Includes: modelops, modelops-contracts, calabaria
- Configured for distributed simulation execution

## Building Images

### Prerequisites
The build requires these repositories to exist as siblings:
```
parent-dir/
├── modelops/           # This repo
├── modelops-contracts/ # Private contracts repo
└── calabaria/          # Science evaluation repo
```

### Build Commands

```bash
# Build all images
make build

# Build specific image
make build-scheduler
make build-worker

# Build with custom tag
make build TAG=v0.1.0

# Build for different registry
make build REGISTRY=myregistry.io ORG=myorg
```

### Testing Images

```bash
# Test that imports work
make test-images

# This runs:
docker run --rm ghcr.io/modelops/dask-worker:latest python -c \
  "import modelops; import modelops_contracts; print('✓ All imports successful')"
```

## Deploying to Cluster

```bash
# Push images to registry (requires auth)
make push

# Update local cluster with new images
make update-cluster

# Show current images in cluster
make show-images
```

## Local Development Workflow

1. Make code changes in modelops/modelops-contracts/calabaria
2. Build new images: `make build`
3. Deploy to local cluster: `make update-cluster`
4. Test with simulations: `make run-simulation-dask`

## Registry Authentication

For GitHub Container Registry:
```bash
# Login to ghcr.io
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

# Push images
make push
```

## Production Considerations

For production deployments, consider:
1. Using proper private registry authentication instead of local copying
2. Multi-stage builds to minimize image size
3. Security scanning of base images
4. Version pinning for all dependencies
5. Separate images for different worker types (CPU/GPU)