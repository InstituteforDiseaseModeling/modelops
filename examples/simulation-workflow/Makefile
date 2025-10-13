# ------------------------------------------------------------
# K8s Job Submission Workflow Test (cleaned up)
# ------------------------------------------------------------

.DEFAULT_GOAL := help
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:

# --- Repo layout (adjust if your paths differ) ---
REPO_ROOT   := $(abspath ../../..)
CONTRACTS   := $(REPO_ROOT)/modelops-contracts
BUNDLE      := $(REPO_ROOT)/modelops-bundle
CALABARIA   := $(REPO_ROOT)/modelops-calabaria
MODELOPS    := $(abspath ../..)  # Current repo is modelops

# --- Example config ---
MODEL_CLASS := models.seir:StochasticSEIR
STUDY_FILE  := study.json
N_SAMPLES   := 20
N_REPLICATES := 3
NAMESPACE   := modelops-dask-dev

# --- Venv and tools ---
VENV    := .venv
BIN     := $(VENV)/bin
UV      := uv
PY      := $(BIN)/python

# Ensure user code importable (for cb to find models/)
export PYTHONPATH := $(CURDIR):$(PYTHONPATH)

.PHONY: help
help:
	@echo "ModelOps Job Submission Workflow"
	@echo ""
	@echo "Setup:"
	@echo "  make venv            - Create venv"
	@echo "  make setup           - Install cb + modelops-bundle (+ mops) from local repos"
	@echo ""
	@echo "Bundle:"
	@echo "  make bundle-init     - Initialize bundle metadata in this dir"
	@echo "  make bundle-register - Register models for cloud job submission"
	@echo "  make bundle-status   - Show bundle status"
	@echo "  make bundle-push     - Push bundle to registry (optional)"
	@echo ""
	@echo "Workflow:"
	@echo "  make study           - Generate Sobol samples -> $(STUDY_FILE)"
	@echo "  make submit          - Submit job with auto-push bundle"
	@echo "  make workflow        - setup -> bundle -> study -> submit"
	@echo ""
	@echo "Monitoring:"
	@echo "  make status          - k8s job + pods"
	@echo "  make logs            - tail first job pod logs"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean           - remove remove everything, including venv and study outputs"
	@echo "  make clean-bundle    - remove local bundle state"

# --- Environment setup ---

$(VENV):
	$(UV) venv $(VENV) --python 3.13

.PHONY: venv
venv: $(VENV)
	@echo "✓ venv ready: $(VENV)"

# install the tools into THIS venv so we can call $(BIN)/cb, $(BIN)/modelops-bundle, $(BIN)/mops
# Install local development versions directly
# Use order-only prerequisites (after |) to avoid rebuilding when venv exists
$(VENV)/.installed: | $(VENV)
	@if [ ! -f $@ ]; then \
		echo "Installing tools into venv..."; \
		$(UV) pip install -e $(abspath $(CONTRACTS)); \
		$(UV) pip install -e $(abspath $(BUNDLE)); \
		$(UV) pip install -e $(abspath $(CALABARIA)); \
		$(UV) pip install -e $(abspath $(MODELOPS)); \
		touch $@; \
	fi

.PHONY: setup
setup: $(VENV)/.installed
	@echo "✓ User tools installed in $(VENV):"
	@echo "  - $$( $(BIN)/cb --help >/dev/null 2>&1 && echo 'cb installed' || echo 'cb missing' )"
	@echo "  - $$( $(BIN)/modelops-bundle --help >/dev/null 2>&1 && echo 'modelops-bundle installed' || echo 'modelops-bundle missing' )"
	@echo "  - $$( $(BIN)/mops --help >/dev/null 2>&1 && echo 'mops installed' || echo 'mops missing')"

# --- Bundle commands ---

.PHONY: bundle-init
bundle-init: $(VENV)/.installed
	@if [ ! -f pyproject.toml ]; then \
		echo "Initializing bundle project..."; \
		$(BIN)/modelops-bundle init .; \
		echo "dev" > .modelops-bundle/env; \
	else \
		echo "Project already initialized (pyproject.toml exists)"; \
		[ -f .modelops-bundle/env ] || echo "dev" > .modelops-bundle/env; \
	fi

.PHONY: bundle-status
bundle-status: $(VENV)/.installed
	$(BIN)/modelops-bundle status

.PHONY: bundle-register
bundle-register: $(VENV)/.installed
	# Generate observed data if not present
	@if [ ! -f data/observed_prevalence.csv ]; then \
		echo "Generating observed data..."; \
		$(PY) generate_observed_data.py; \
	fi
	# First ensure files are tracked
	@$(BIN)/modelops-bundle add models/ 2>/dev/null || true
	@$(BIN)/modelops-bundle add targets/ 2>/dev/null || true
	@$(BIN)/modelops-bundle add data/ 2>/dev/null || true
	# Register the model for cloud job submission
	$(BIN)/modelops-bundle register-model models/seir.py --no-confirm
	# Register targets - auto-discovers decorated functions and their dependencies
	$(BIN)/modelops-bundle register-target targets/prevalence.py --no-confirm
	@echo "✓ Registered SEIR model and calibration targets"

.PHONY: bundle-push
bundle-push: $(VENV)/.installed
	$(BIN)/modelops-bundle push
	@echo "✓ Bundle pushed to registry"

# --- Study + Submit ---

.PHONY: study
study: $(STUDY_FILE)

$(STUDY_FILE): $(VENV)/.installed
	$(BIN)/cb sampling sobol $(MODEL_CLASS) \
		--scenario baseline \
		--n-samples $(N_SAMPLES) \
		--n-replicates $(N_REPLICATES) \
		--output $(STUDY_FILE) \
		--seed 42 \
		--scramble \
		--targets "targets.prevalence:prevalence_target"
	@echo "✓ Wrote $(STUDY_FILE) with $(N_REPLICATES) replicates per parameter set"

.PHONY: submit
submit: bundle-init bundle-register $(STUDY_FILE) $(VENV)/.installed
	# Submit with auto-push from current directory
	$(BIN)/mops jobs submit $(abspath $(STUDY_FILE)) --auto
	@echo "✓ Submitted job with auto-pushed bundle"

.PHONY: workflow
workflow: setup bundle-init bundle-register study submit
	@echo "✓ Workflow complete. Try: make status"

# --- Monitoring ---

.PHONY: status
status:
	kubectl get jobs -n $(NAMESPACE) || true
	kubectl get pods -n $(NAMESPACE) | grep job || true

.PHONY: logs
logs:
	@pod="$$(kubectl get pods -n $(NAMESPACE) -o name | grep job | head -1)"; \
	[ -n "$$pod" ] && kubectl logs -n $(NAMESPACE) "$$pod" --tail=50 || echo "No job pod found"

.PHONY: check-pod-results
check-pod-results:
	@echo "Checking results stored on worker pods..."
	@kubectl exec deployment/dask-workers -n $(NAMESPACE) -- bash -c \
		"echo 'Results in /tmp/modelops/provenance:' && \
		 find /tmp/modelops/provenance -name '*.json' -type f 2>/dev/null | wc -l | xargs echo 'JSON files:' && \
		 find /tmp/modelops/provenance -name '*.arrow' -type f 2>/dev/null | wc -l | xargs echo 'Arrow files:' && \
		 echo && echo 'Sample result path:' && \
		 find /tmp/modelops/provenance -name 'result.json' -type f 2>/dev/null | head -1" \
		 2>/dev/null || echo "No worker pods running or no results found"

.PHONY: sync-results
sync-results:
	@echo "Syncing results from worker pods to local results/ directory..."
	@mkdir -p results
	@# Get all worker pods and sync from each
	@kubectl get pods -n $(NAMESPACE) -l app=dask-worker -o name | while read pod; do \
		POD_NAME=$$(echo $$pod | cut -d/ -f2); \
		echo "Syncing from $$POD_NAME..."; \
		kubectl cp $(NAMESPACE)/$$POD_NAME:/tmp/modelops/provenance results/$$POD_NAME 2>/dev/null || \
		echo "  Warning: Could not sync from $$POD_NAME (pod may have no results or be terminating)"; \
	done
	@echo ""
	@echo "Local results summary:"
	@find results -name '*.json' -type f 2>/dev/null | wc -l | xargs echo "  JSON files:"
	@find results -name '*.arrow' -type f 2>/dev/null | wc -l | xargs echo "  Arrow files:"
	@find results -name 'result.json' -type f 2>/dev/null | wc -l | xargs echo "  Result files:"
	@echo ""
	@echo "Results synced to: results/"

.PHONY: list-results
list-results:
	@echo "Checking job results in Azure Blob Storage..."
	@# Try to get storage account dynamically, fallback to modelopsdev
	@STORAGE_ACCOUNT=$$(az storage account list --query "[?tags.component=='modelops'].name" -o tsv 2>/dev/null | head -1); \
	if [ -z "$$STORAGE_ACCOUNT" ]; then STORAGE_ACCOUNT="modelopsdev"; fi; \
	echo "Storage account: $$STORAGE_ACCOUNT"; \
	echo ""; \
	echo "Jobs container:"; \
	az storage blob list --container-name jobs --account-name "$$STORAGE_ACCOUNT" \
		--query "[?starts_with(name, 'jobs/')].{name:name, size:properties.contentLength}" -o table 2>/dev/null || \
		echo "No jobs found or az CLI not configured"; \
	echo ""; \
	echo "Results container (if exists):"; \
	az storage blob list --container-name results --account-name "$$STORAGE_ACCOUNT" \
		--query "[].{name:name, size:properties.contentLength}" -o table 2>/dev/null || \
		echo "No results found or container doesn't exist"

# --- Cleanup ---

.PHONY: clean-data
clean-data:
	rm -rf data/

.PHONY: clean-bundle
clean-bundle:
	rm -rf .modelops-bundle pyproject.toml

.PHONY: clean
clean: clean-bundle clean-data
	rm -f $(STUDY_FILE)
	rm -rf $(VENV) .constraints.txt
	@echo "✓ Clean complete"
