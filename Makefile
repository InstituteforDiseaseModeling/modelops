# ModelOps Makefile - Development and Docker Image Management

# === Development Variables ===
MOPS := uv run mops
PROVIDER_DIR := ~/.modelops/providers
WORKSPACE_NAME := default

# === Docker Variables ===
REGISTRY ?= ghcr.io
ORG ?= modelops
TAG ?= latest
PYTHON_VERSION ?= 3.11

# Image names
SCHEDULER_IMAGE = $(REGISTRY)/$(ORG)/dask-scheduler:$(TAG)
WORKER_IMAGE = $(REGISTRY)/$(ORG)/dask-worker:$(TAG)

# Build context is parent directory to access sibling repos, 
# like ../modelops-contracts and ../calabaria (TODO/POST-MVP)
BUILD_CONTEXT = ..

# === Development Targets ===

.PHONY: help install test lint workspace-up workspace-down workspace-status

## Display help
help:
	@echo "ModelOps Development & Docker Management"
	@echo ""
	@echo "Development Commands:"
	@echo "  make install           # Install dependencies with uv"
	@echo "  make test             # Run tests"
	@echo "  make lint             # Run linters"
	@echo "  make workspace-up     # Create local workspace"
	@echo "  make workspace-down   # Destroy local workspace"
	@echo "  make workspace-status # Show workspace status"
	@echo ""
	@echo "Docker Commands:"
	@echo "  make build            # Build all Docker images"
	@echo "  make build-worker     # Build worker image only"
	@echo "  make build-scheduler  # Build scheduler image only"
	@echo "  make push            # Push images to registry"
	@echo "  make update-cluster  # Update cluster with new images"
	@echo "  make test-images     # Test Docker images locally"
	@echo ""
	@echo "Prerequisites for Docker:"
	@echo "  - ../modelops-contracts must exist"
	@echo "  - ../calabaria must exist"
	@echo "  - Docker daemon must be running"
	@echo ""
	@echo "Configuration:"
	@echo "  REGISTRY=$(REGISTRY)  # Container registry"
	@echo "  ORG=$(ORG)           # Organization name"
	@echo "  TAG=$(TAG)           # Image tag"

## Install dependencies
install:
	uv sync

## Run tests
test:
	uv run pytest tests/

## Run linters
lint:
	uv run ruff check src/
	uv run mypy src/ || true

## Create local workspace
workspace-up:
	$(MOPS) workspace up --name $(WORKSPACE_NAME) --provider orbstack

## Destroy local workspace
workspace-down:
	$(MOPS) workspace down --name $(WORKSPACE_NAME)

## Show workspace status
workspace-status:
	$(MOPS) workspace status --name $(WORKSPACE_NAME)

# === Docker Targets ===

.PHONY: build build-scheduler build-worker push push-scheduler push-worker
.PHONY: update-cluster clean-images show-images test-images

## Build all Docker images
build: build-scheduler build-worker

## Build Dask scheduler image
build-scheduler:
	@echo "Building Dask scheduler image: $(SCHEDULER_IMAGE)"
	@echo "Note: Requires ../modelops-contracts and ../calabaria to exist"
	docker build \
		-f docker/Dockerfile.scheduler \
		-t $(SCHEDULER_IMAGE) \
		--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
		$(BUILD_CONTEXT)

## Build Dask worker image  
build-worker:
	@echo "Building Dask worker image: $(WORKER_IMAGE)"
	@echo "Note: Requires ../modelops-contracts and ../calabaria to exist"
	docker build \
		-f docker/Dockerfile.worker \
		-t $(WORKER_IMAGE) \
		--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
		$(BUILD_CONTEXT)

## Push scheduler image to registry
push-scheduler:
	docker push $(SCHEDULER_IMAGE)

## Push worker image to registry
push-worker:
	docker push $(WORKER_IMAGE)

## Push all images to registry
push: push-scheduler push-worker

## Update cluster with new images
update-cluster:
	@echo "Updating cluster with new images..."
	kubectl set image deployment/dask-scheduler -n modelops-default \
		scheduler=$(SCHEDULER_IMAGE)
	kubectl set image deployment/dask-workers -n modelops-default \
		worker=$(WORKER_IMAGE)
	kubectl rollout status deployment/dask-scheduler -n modelops-default
	kubectl rollout status deployment/dask-workers -n modelops-default

## Clean Docker images
clean-images:
	docker rmi $(SCHEDULER_IMAGE) $(WORKER_IMAGE) || true

## Show current images in cluster
show-images:
	@echo "Current images in cluster:"
	@kubectl get deployment -n modelops-default -o wide | grep dask || true

## Test Docker images locally
# TODO: test calabaria too?
test-images:
	@echo "Testing worker image imports..."
	docker run --rm $(WORKER_IMAGE) python -c \
		"import modelops; import modelops_contracts; print('✓ All imports successful')"

# === Combined Workflows ===

.PHONY: dev-setup dev-test dev-deploy

## Complete development setup
dev-setup: install workspace-up
	@echo "Development environment ready!"
	@echo "Dashboard available at: http://localhost:8787"

## Build and test everything
dev-test: test test-images
	@echo "All tests passed!"

## Build and deploy to local cluster
dev-deploy: build update-cluster
	@echo "New images deployed to cluster!"

# === Simulation Examples ===

.PHONY: run-simulation-local run-simulation-dask

## Run simulation locally
run-simulation-local:
	PYTHONPATH=. uv run python examples/run_dask_simulation.py --local --test pi -n 5

## Run simulation on Dask
run-simulation-dask:
	PYTHONPATH=. uv run python examples/run_dask_simulation.py --test pi -n 10 
