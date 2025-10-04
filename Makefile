# ModelOps Makefile - Development and Docker Image Management

# === Development Variables ===
MOPS := uv run mops
PROVIDER_DIR := ~/.modelops/providers
WORKSPACE_NAME := default
ENV ?= dev
NAMESPACE ?= modelops-dask-$(ENV)

# === Docker Variables ===
# Default to public GHCR for easy distribution
REGISTRY ?= ghcr.io
ORG ?= institutefordiseasemodeling
PROJECT ?= modelops

# Automatic versioning from git
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo "dev")
VERSION ?= $(GIT_SHA)
TAG ?= latest
PYTHON_VERSION ?= 3.11

# === Azure Container Registry (Future/Private Use) ===
# Uncomment below to use private ACR instead of GHCR
# -include .modelops.env
# MODELOPS_REGISTRY_SERVER ?= $(shell uv run mops registry env --format make 2>/dev/null | grep MODELOPS_REGISTRY_SERVER | cut -d= -f2)
# ifneq ($(MODELOPS_REGISTRY_SERVER),)
#   REGISTRY = $(MODELOPS_REGISTRY_SERVER)
#   ORG = modelops
# endif

# Image names
SCHEDULER_IMAGE = $(REGISTRY)/$(ORG)/$(PROJECT)-dask-scheduler
WORKER_IMAGE = $(REGISTRY)/$(ORG)/$(PROJECT)-dask-worker
RUNNER_IMAGE = $(REGISTRY)/$(ORG)/$(PROJECT)-job-runner
SMOKETEST_IMAGE = $(REGISTRY)/$(ORG)/$(PROJECT)-smoketest

# GHCR Configuration
GHCR_USER ?= $(shell git config user.name 2>/dev/null | tr ' ' '-' | tr '[:upper:]' '[:lower:]' || echo "user")

# Platform configuration for multi-architecture builds
# For Azure AKS, we only need AMD64 (x86_64) architecture
PLATFORMS ?= linux/amd64
BUILDX_BUILDER ?= modelops-multiarch

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
	@echo "  make test              # Run tests"
	@echo "  make lint              # Run linters"
	@echo ""
	@echo "Local Dask Commands:"
	@echo "  make dask-local        # Start local Dask cluster"
	@echo "  make dask-stop         # Stop local Dask cluster"
	@echo "  make test-e2e          # Run e2e tests with Dask"
	@echo "  make test-e2e-fresh    # Run e2e tests with fresh venvs (debugging)"
	@echo "  make benchmark-venv    # Benchmark warm pool vs fresh venv performance"
	@echo ""
	@echo "Docker Commands:"
	@echo "  make build            # Build and push AMD64 images (scheduler, worker, runner)"
	@echo "  make build-mac        # Build images for dev Mac (Apple Silicon/ARM64)"
	@echo "  make build-multiarch  # Build all images for AMD64 deployment"
	@echo "  make setup-buildx     # Setup Docker buildx for multi-arch"
	@echo "  make release          # Tag and push release version"
	@echo "  make update-cluster   # Update cluster with new images"
	@echo "  make test-images      # Test Docker images locally"
	@echo ""
	@echo "Prerequisites for Docker:"
	@echo "  - ../modelops-contracts must exist"
	@echo "  - ../calabaria must exist"
	@echo "  - Docker daemon must be running"
	@echo ""
	@echo "Configuration:"
	@echo "  REGISTRY=$(REGISTRY)  # Container registry"
	@echo "  ORG=$(ORG)            # Organization name"
	@echo "  TAG=$(TAG)            # Image tag"

## Install dependencies
install:
	uv sync

## Run unit tests (default)
test:
	uv run pytest tests/

## Run integration tests (requires more resources)
test-integration:
	uv run pytest tests/ -m integration -v

## Run all tests (unit + integration)
test-all:
	uv run pytest tests/ -m "" -v

## Run linters
lint:
	uv run ruff check src/
	uv run mypy src/ || true

# === Docker Targets ===

.PHONY: build build-scheduler build-worker build-multiarch build-mac setup-buildx
.PHONY: ghcr-login release update-cluster clean-images show-images test-images check-visibility make-public

## Build all Docker images (multi-architecture for deployment)
build: build-multiarch check-visibility 
	@echo "✓ Built and pushed images with version: $(VERSION)"

## Setup Docker buildx for multi-architecture builds
setup-buildx:
	@echo "Setting up Docker buildx builder: $(BUILDX_BUILDER)"
	@docker buildx create --name $(BUILDX_BUILDER) --driver docker-container --use 2>/dev/null || docker buildx use $(BUILDX_BUILDER)
	@docker buildx inspect --bootstrap
	@echo "✓ Buildx ready for platforms: $(PLATFORMS)"

## Build images for local Mac (Apple Silicon native)
build-mac:
	@echo "Building for local mac dev architecture (Apple Silicon)..."
	$(MAKE) build-scheduler build-worker build-runner build-smoketest
	@echo "✓ Built local images with version: $(VERSION)"

## Build and push multi-arch scheduler image
build-multiarch-scheduler: setup-buildx ghcr-login
	@echo "Building multi-arch scheduler image for platforms: $(PLATFORMS)"
	@docker buildx build \
		--platform $(PLATFORMS) \
		-f docker/Dockerfile.scheduler \
		-t $(SCHEDULER_IMAGE):$(TAG) \
		-t $(SCHEDULER_IMAGE):$(VERSION) \
		--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
		--push \
		$(BUILD_CONTEXT)
	@echo "✓ Scheduler image built and pushed: $(SCHEDULER_IMAGE):$(TAG)"

## Build and push multi-arch worker image
build-multiarch-worker: setup-buildx ghcr-login
	@echo "Building multi-arch worker image for platforms: $(PLATFORMS)"
	@docker buildx build \
		--platform $(PLATFORMS) \
		-f docker/Dockerfile.worker \
		-t $(WORKER_IMAGE):$(TAG) \
		-t $(WORKER_IMAGE):$(VERSION) \
		--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
		--push \
		$(BUILD_CONTEXT)
	@echo "✓ Worker image built and pushed: $(WORKER_IMAGE):$(TAG)"

## Build and push multi-arch runner image
build-multiarch-runner: setup-buildx ghcr-login
	@echo "Building multi-arch runner image for platforms: $(PLATFORMS)"
	@docker buildx build \
		--platform $(PLATFORMS) \
		-f docker/Dockerfile.runner \
		-t $(RUNNER_IMAGE):$(TAG) \
		-t $(RUNNER_IMAGE):$(VERSION) \
		--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
		--push \
		$(BUILD_CONTEXT)
	@echo "✓ Runner image built and pushed: $(RUNNER_IMAGE):$(TAG)"

## Build and push multi-arch smoketest image
build-multiarch-smoketest: setup-buildx ghcr-login
	@echo "Building multi-arch smoketest image for platforms: $(PLATFORMS)"
	@docker buildx build \
		--platform $(PLATFORMS) \
		-f docker/Dockerfile.smoketest \
		-t $(SMOKETEST_IMAGE):$(TAG) \
		-t $(SMOKETEST_IMAGE):$(VERSION) \
		--push \
		$(BUILD_CONTEXT)/modelops
	@echo "✓ Smoketest image built and pushed: $(SMOKETEST_IMAGE):$(TAG)"

## Build and push all multi-architecture images (can be parallelized with -j)
build-multiarch: build-multiarch-scheduler build-multiarch-worker build-multiarch-runner build-multiarch-smoketest
	@echo "✓ All multi-arch images built and pushed for: $(PLATFORMS)"
	@echo "  $(SCHEDULER_IMAGE):$(TAG)"
	@echo "  $(SCHEDULER_IMAGE):$(VERSION)"
	@echo "  $(WORKER_IMAGE):$(TAG)"
	@echo "  $(WORKER_IMAGE):$(VERSION)"
	@echo "  $(SMOKETEST_IMAGE):$(TAG)"
	@echo "  $(SMOKETEST_IMAGE):$(VERSION)"

## Build Dask scheduler image
build-scheduler:
	@echo "Building Dask scheduler image: $(SCHEDULER_IMAGE):$(TAG)"
	@echo "Note: Requires ../modelops-contracts and ../calabaria to exist"
	docker build \
		-f docker/Dockerfile.scheduler \
		-t $(SCHEDULER_IMAGE):$(TAG) \
		--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
		$(BUILD_CONTEXT)

## Build Dask worker image  
build-worker:
	@echo "Building Dask worker image: $(WORKER_IMAGE):$(TAG)"
	@echo "Note: Requires ../modelops-contracts and ../calabaria to exist"
	docker build \
		-f docker/Dockerfile.worker \
		-t $(WORKER_IMAGE):$(TAG) \
		--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
		$(BUILD_CONTEXT)

## Build job runner image
build-runner:
	@echo "Building job runner image: $(RUNNER_IMAGE):$(TAG)"
	@echo "Note: Requires ../modelops-contracts to exist"
	docker build \
		-f docker/Dockerfile.runner \
		-t $(RUNNER_IMAGE):$(TAG) \
		--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
		$(BUILD_CONTEXT)

## Build smoke test image
build-smoketest:
	@echo "Building smoke test image: $(SMOKETEST_IMAGE):$(TAG)"
	docker build \
		-f docker/Dockerfile.smoketest \
		-t $(SMOKETEST_IMAGE):$(TAG) \
		.

## Login to GitHub Container Registry
ghcr-login:
	@if [ "$(REGISTRY)" = "ghcr.io" ]; then \
		if [ -z "$(GHCR_PAT)" ]; then \
			echo "⚠️  GHCR_PAT not set, trying gh auth token..."; \
			GH_TOKEN=$$(gh auth token 2>/dev/null); \
			if [ -n "$$GH_TOKEN" ]; then \
				echo "$$GH_TOKEN" | docker login ghcr.io -u $(GHCR_USER) --password-stdin; \
			else \
				echo "❌ No GHCR_PAT or gh auth found. Please:"; \
				echo "   export GHCR_PAT=<your-github-pat>"; \
				echo "   or run: gh auth login"; \
				exit 1; \
			fi; \
		else \
			echo "$(GHCR_PAT)" | docker login ghcr.io -u $(GHCR_USER) --password-stdin; \
		fi; \
		echo "✓ Logged into GHCR"; \
	fi

# === Azure ACR Login (Preserved for Future Use) ===
# acr-login:
# 	@REGISTRY_NAME=$$(uv run mops registry env --format make 2>/dev/null | grep MODELOPS_REGISTRY_NAME | cut -d= -f2); \
# 	if [ -z "$$REGISTRY_NAME" ]; then \
# 		echo "Error: No registry found. Run 'mops registry create' first"; \
# 		exit 1; \
# 	fi; \
# 	echo "Logging into Azure Container Registry: $$REGISTRY_NAME"; \
# 	TOKEN=$$(az acr login -n $$REGISTRY_NAME --expose-token -o json 2>/dev/null | jq -r '.accessToken'); \
# 	if [ -z "$$TOKEN" ]; then \
# 		echo "Error: Failed to get ACR token. Check Azure login with 'az login'"; \
# 		exit 1; \
# 	fi; \
# 	echo "$$TOKEN" | docker login $(REGISTRY) -u 00000000-0000-0000-0000-000000000000 --password-stdin && \
# 	echo "✓ Successfully logged into $(REGISTRY)"

## Check if GHCR packages are public (and warn if not)
check-visibility:
	@echo "Checking GHCR package visibility..."
	@if [ -z "$(GHCR_PAT)" ]; then \
		echo "⚠️  GHCR_PAT not set, skipping visibility check"; \
		echo "   Set GHCR_PAT to enable automatic visibility checking"; \
	else \
		PRIVATE_PKGS=$$(curl -s -H "Authorization: Bearer $(GHCR_PAT)" \
			-H "X-GitHub-Api-Version: 2022-11-28" \
			"https://api.github.com/orgs/$(ORG)/packages?package_type=container&visibility=private" \
			| jq -r '.[].name' 2>/dev/null | grep -E "$(PROJECT)-(dask-(scheduler|worker)|job-runner)" || true); \
		if [ -n "$$PRIVATE_PKGS" ]; then \
			echo "❌ The following packages are PRIVATE and need to be made public:"; \
			echo "$$PRIVATE_PKGS" | sed 's/^/   - /'; \
			echo ""; \
			echo "To fix this, go to: https://github.com/orgs/$(ORG)/packages"; \
			echo "Click on each package → Settings → Change visibility → Make public"; \
			exit 1; \
		else \
			echo "✓ All ModelOps packages are public"; \
		fi; \
	fi

# Alias for backward compatibility
make-public: check-visibility


## Tag a release version
release:
	@if [ -z "$(RELEASE_VERSION)" ]; then \
		echo "Usage: make release RELEASE_VERSION=v1.0.0"; \
		exit 1; \
	fi
	docker tag $(SCHEDULER_IMAGE):$(TAG) $(SCHEDULER_IMAGE):$(RELEASE_VERSION)
	docker tag $(WORKER_IMAGE):$(TAG) $(WORKER_IMAGE):$(RELEASE_VERSION)
	docker tag $(RUNNER_IMAGE):$(TAG) $(RUNNER_IMAGE):$(RELEASE_VERSION)
	docker push $(SCHEDULER_IMAGE):$(RELEASE_VERSION)
	docker push $(WORKER_IMAGE):$(RELEASE_VERSION)
	docker push $(RUNNER_IMAGE):$(RELEASE_VERSION)
	@echo "✓ Released version $(RELEASE_VERSION)"

# === Azure ACR Targets (Preserved for Future Use) ===
# verify-push:
# 	@echo "Verifying images in registry..."
# 	@REGISTRY_NAME=$$(uv run mops registry env --format make 2>/dev/null | grep MODELOPS_REGISTRY_NAME | cut -d= -f2); \
# 	if [ -n "$$REGISTRY_NAME" ]; then \
# 		echo "\nImages in $$REGISTRY_NAME:"; \
# 		az acr repository list --name $$REGISTRY_NAME 2>/dev/null || echo "No repositories found"; \
# 		echo "\nScheduler tags:"; \
# 		az acr repository show-tags --name $$REGISTRY_NAME --repository modelops/dask-scheduler 2>/dev/null || echo "No scheduler image"; \
# 		echo "\nWorker tags:"; \
# 		az acr repository show-tags --name $$REGISTRY_NAME --repository modelops/dask-worker 2>/dev/null || echo "No worker image"; \
# 	else \
# 		echo "No Azure registry configured - using $(REGISTRY)"; \
# 	fi

## Update cluster with new images (Pulumi-managed)
update-cluster:
	@echo "⚠ Dask deployments are managed by Pulumi - kubectl updates will be reverted"
	@echo ""
	@echo "To update cluster with new images:"
	@echo "  1. Build and push: make build"
	@echo "  2. Restart workspace:"
	@echo "     uv run mops workspace down"
	@echo "     uv run mops workspace up --config examples/workspace.yaml"
	@echo ""
	@echo "Latest images: $(SCHEDULER_IMAGE):$(TAG), $(WORKER_IMAGE):$(TAG)"

## Rollout new images to running Dask deployments
rollout-images:
	@echo "Rolling out latest images to Dask deployments in namespace: $(NAMESPACE)"
	@kubectl rollout restart deployment dask-scheduler -n $(NAMESPACE) 2>/dev/null || true
	@kubectl rollout restart deployment dask-workers -n $(NAMESPACE) 2>/dev/null || true
	@# Note: Job runners are one-shot K8s Jobs, not deployments - they pick up new images on next submission
	@echo "Waiting for rollouts to complete..."
	@kubectl rollout status deployment dask-scheduler -n $(NAMESPACE) --timeout=120s 2>/dev/null || true
	@kubectl rollout status deployment dask-workers -n $(NAMESPACE) --timeout=120s 2>/dev/null || true
	@echo "✓ New images rolled out successfully"
	@echo "Note: Job runners will use new image on next job submission"
	@kubectl get pods -n $(NAMESPACE) | grep -E "dask-|job-|NAME"

## Clean Docker images
clean-images:
	docker rmi $(SCHEDULER_IMAGE):$(TAG) $(WORKER_IMAGE):$(TAG) $(RUNNER_IMAGE):$(TAG) || true
	docker rmi $(SCHEDULER_IMAGE):$(VERSION) $(WORKER_IMAGE):$(VERSION) $(RUNNER_IMAGE):$(VERSION) || true

## Show current images in cluster
show-images:
	@echo "Current images in cluster (namespace: $(NAMESPACE)):"
	@kubectl get deployment -n $(NAMESPACE) -o wide | grep dask || true

## Test Docker images locally
test-images:
	@echo "Testing worker image imports..."
	docker run --rm $(WORKER_IMAGE):$(TAG) python -c \
		"import modelops; import modelops_contracts; import calabaria; print('✓ All imports successful')"

# === Combined Workflows ===

.PHONY: dev-setup dev-test dev-deploy

## Build and test everything
dev-test: test test-images
	@echo "All tests passed!"

## Build and deploy workflow
dev-deploy: build
	@echo "✓ Images built and pushed ($(VERSION))"
	@echo "To deploy: uv run mops workspace down && uv run mops workspace up --config examples/workspace.yaml"

## Build and deploy multi-arch images
dev-deploy-multiarch: build-multiarch
	@echo "✓ Multi-arch images built and pushed ($(VERSION))"
	@echo "To deploy: uv run mops workspace down && uv run mops workspace up --config examples/workspace.yaml"

# === Simulation Examples ===

.PHONY: run-simulation-local run-simulation-dask

## Run simulation locally
run-simulation-local:
	PYTHONPATH=. uv run python examples/run_dask_simulation.py --local --test pi -n 5

## Run simulation on Dask
run-simulation-dask:
	PYTHONPATH=. uv run python examples/run_dask_simulation.py --test pi -n 10

# === Local Dask Development ===

.PHONY: dask-local dask-stop test-e2e test-e2e-fresh benchmark-venv

## Start local Dask cluster for development
dask-local:
	@echo "Starting local Dask cluster..."
	@uv run python examples/start_local_dask.py &
	@sleep 2
	@echo "✓ Dask cluster started at tcp://localhost:8786"
	@echo "  Dashboard: http://localhost:8787"

## Stop local Dask cluster
dask-stop:
	@echo "Stopping local Dask cluster..."
	@pkill -f "start_local_dask.py" 2>/dev/null || true
	@pkill -f "dask scheduler" 2>/dev/null || true
	@pkill -f "dask worker" 2>/dev/null || true
	@echo "✓ Dask cluster stopped"

## Run end-to-end tests with local Dask
test-e2e:
	@echo "Starting Dask and running e2e tests..."
	@$(MAKE) dask-local
	@sleep 3
	@uv run python examples/test_simulation_e2e.py
	@$(MAKE) dask-stop

## Run e2e tests with fresh venvs (slow, for debugging)
test-e2e-fresh:
	@echo "Starting Dask and running e2e tests with fresh venvs..."
	@$(MAKE) dask-local
	@sleep 3
	@MODELOPS_FORCE_FRESH_VENV=true uv run python examples/test_simulation_e2e.py
	@$(MAKE) dask-stop

## Benchmark warm pool vs fresh venv performance
benchmark-venv:
	@echo "Starting Dask for benchmark..."
	@$(MAKE) dask-local
	@sleep 3
	@echo "Running hyperfine benchmark..."
	@hyperfine \
	  --command-name 'cached venv' \
	    'uv run python examples/test_simulation_e2e.py' \
	  --command-name 'fresh venv' \
	    'MODELOPS_FORCE_FRESH_VENV=true uv run python examples/test_simulation_e2e.py' \
	  --warmup 1
	@$(MAKE) dask-stop

# === State Cleanup Targets ===

.PHONY: clean-unreachable clean-workspace clean-storage clean-all-state reset-stacks

## Clean unreachable K8s resources from workspace
clean-unreachable:
	@echo "Cleaning unreachable K8s resources from workspace..."
	@PULUMI_K8S_DELETE_UNREACHABLE=true pulumi destroy \
		--cwd ~/.modelops/pulumi/workspace \
		--stack modelops-workspace-$(ENV) --yes || true
	@echo "✓ Workspace cleaned"

## Clean workspace state when cluster is gone
clean-workspace:
	@echo "Cleaning workspace state..."
	@PULUMI_K8S_DELETE_UNREACHABLE=true pulumi refresh \
		--cwd ~/.modelops/pulumi/workspace \
		--stack modelops-workspace-$(ENV) --yes 2>/dev/null || true
	@PULUMI_K8S_DELETE_UNREACHABLE=true pulumi destroy \
		--cwd ~/.modelops/pulumi/workspace \
		--stack modelops-workspace-$(ENV) --yes 2>/dev/null || true
	@echo "✓ Workspace state cleaned"

## Clean storage state
clean-storage:
	@echo "Cleaning storage state..."
	@pulumi destroy \
		--cwd ~/.modelops/pulumi/storage \
		--stack modelops-storage-$(ENV) --yes 2>/dev/null || true
	@echo "✓ Storage state cleaned"

## Clean all Pulumi state (use with caution!)
clean-all-state: clean-workspace clean-storage
	@echo "✓ All state cleaned for environment: $(ENV)"

## Nuclear option: Reset all stacks (requires confirmation)
reset-stacks:
	@echo "⚠️  WARNING: This will destroy ALL stacks for environment: $(ENV)"
	@echo "This includes: infra, workspace, storage, registry"
	@read -p "Type 'DESTROY' to confirm: " confirm && [ "$$confirm" = "DESTROY" ] || exit 1
	@echo "Resetting all stacks..."
	@$(MAKE) clean-workspace ENV=$(ENV)
	@$(MAKE) clean-storage ENV=$(ENV)
	@pulumi destroy --cwd ~/.modelops/pulumi/registry --stack modelops-registry-$(ENV) --yes 2>/dev/null || true
	@pulumi destroy --cwd ~/.modelops/pulumi/infra --stack modelops-infra-$(ENV) --yes 2>/dev/null || true
	@echo "✓ All stacks reset. You can start fresh with 'mops infra up'"

## Show all Azure ModelOps resources
azure-status:
	@echo "═══════════════════════════════════════════════════════════════════"
	@echo "                    Azure ModelOps Resources"
	@echo "═══════════════════════════════════════════════════════════════════"
	@echo ""
	@echo "📁 RESOURCE GROUPS:"
	@echo "───────────────────────────────────────────────────────────────────"
	@az group list --query "[?contains(name, 'modelops')].{Name:name, Location:location}" -o table 2>/dev/null || echo "  ✗ No resource groups found"
	@echo ""
	@echo "🌐 KUBERNETES CLUSTERS (AKS):"
	@echo "───────────────────────────────────────────────────────────────────"
	@az aks list --query "[?contains(name, 'modelops')].{Name:name, ResourceGroup:resourceGroup, Status:powerState.code, Version:kubernetesVersion, Nodes:agentPoolProfiles[0].count}" -o table 2>/dev/null || echo "  ✗ No AKS clusters found"
	@echo ""
	@echo "📦 CONTAINER REGISTRIES (ACR):"
	@echo "───────────────────────────────────────────────────────────────────"
	@az acr list --query "[?contains(name, 'modelops')].{Name:name, ResourceGroup:resourceGroup, LoginServer:loginServer}" -o table 2>/dev/null || echo "  ✗ No container registries found"
	@echo ""
	@echo "💾 STORAGE ACCOUNTS:"
	@echo "───────────────────────────────────────────────────────────────────"
	@az storage account list --query "[?contains(name, 'modelops')].{Name:name, ResourceGroup:resourceGroup, Location:location, SKU:sku.name}" -o table 2>/dev/null || echo "  ✗ No storage accounts found"
	@echo ""
	@echo "═══════════════════════════════════════════════════════════════════"

## Quick Azure resource cleanup check
azure-check:
	@echo "Checking for ModelOps resources in Azure..."
	@RG_COUNT=$$(az group list --query "[?contains(name, 'modelops')] | length(@)" -o tsv 2>/dev/null || echo "0"); \
	AKS_COUNT=$$(az aks list --query "[?contains(name, 'modelops')] | length(@)" -o tsv 2>/dev/null || echo "0"); \
	ACR_COUNT=$$(az acr list --query "[?contains(name, 'modelops')] | length(@)" -o tsv 2>/dev/null || echo "0"); \
	STORAGE_COUNT=$$(az storage account list --query "[?contains(name, 'modelops')] | length(@)" -o tsv 2>/dev/null || echo "0"); \
	if [ "$$RG_COUNT" = "0" ] && [ "$$AKS_COUNT" = "0" ] && [ "$$ACR_COUNT" = "0" ] && [ "$$STORAGE_COUNT" = "0" ]; then \
		echo "✓ Azure is clean - no ModelOps resources found"; \
	else \
		echo "⚠️  Found Azure resources:"; \
		[ "$$RG_COUNT" != "0" ] && echo "  • $$RG_COUNT resource group(s)"; \
		[ "$$AKS_COUNT" != "0" ] && echo "  • $$AKS_COUNT AKS cluster(s)"; \
		[ "$$ACR_COUNT" != "0" ] && echo "  • $$ACR_COUNT container registry(s)"; \
		[ "$$STORAGE_COUNT" != "0" ] && echo "  • $$STORAGE_COUNT storage account(s)"; \
		echo ""; \
		echo "Run 'make azure-status' for details or 'make azure-clean' to remove all"; \
	fi

## Delete all Azure ModelOps resources
azure-clean:
	@echo "⚠️  WARNING: This will delete ALL ModelOps resources in Azure!"
	@echo "This includes all resource groups starting with 'modelops-'"
	@read -p "Type 'DELETE AZURE' to confirm: " confirm && [ "$$confirm" = "DELETE AZURE" ] || exit 1
	@echo "Deleting all ModelOps resource groups..."
	@for rg in $$(az group list --query "[?contains(name, 'modelops')].name" -o tsv 2>/dev/null); do \
		echo "  Deleting $$rg..."; \
		az group delete --name $$rg --yes --no-wait; \
	done
	@echo "✓ Deletion initiated. Resources will be removed in the background."
	@echo "Run 'make azure-check' in a few minutes to verify cleanup."

## Quick cleanup for common dev issues
dev-cleanup:
	@echo "Running quick cleanup for common development issues..."
	@$(MOPS) cleanup unreachable workspace --yes 2>/dev/null || true
	@$(MOPS) cleanup orphaned --yes 2>/dev/null || true

## Clean local state (preserves passphrase for security)
clean-local-state:
	@echo "Cleaning local ModelOps state (preserving passphrase)..."
	@# Preserve the passphrase file
	@if [ -f ~/.modelops/secrets/pulumi-passphrase ]; then \
		cp ~/.modelops/secrets/pulumi-passphrase /tmp/modelops-passphrase-backup; \
	fi
	@# Clean everything except passphrase
	@find ~/.modelops -type f -not -path "*/secrets/*" -delete 2>/dev/null || true
	@find ~/.modelops -type d -empty -delete 2>/dev/null || true
	@# Restore passphrase if it was backed up
	@if [ -f /tmp/modelops-passphrase-backup ]; then \
		mkdir -p ~/.modelops/secrets; \
		mv /tmp/modelops-passphrase-backup ~/.modelops/secrets/pulumi-passphrase; \
		chmod 600 ~/.modelops/secrets/pulumi-passphrase; \
	fi
	@echo "✓ Local state cleaned (passphrase preserved)"

## Nuclear clean: Remove everything including passphrase (use with caution!)
clean-nuclear:
	@echo "⚠️  WARNING: This will remove ALL local ModelOps data including the Pulumi passphrase!"
	@echo "You will not be able to access existing Pulumi stacks after this."
	@read -p "Type 'DELETE ALL' to confirm: " confirm && [ "$$confirm" = "DELETE ALL" ] || exit 1
	@rm -rf ~/.modelops
	@echo "✓ All local ModelOps data removed"
	@echo "Note: If Pulumi stacks still exist, you'll need to delete them via Azure portal"
	@echo "✓ Dev cleanup complete" 
