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
MODEL_CLASS := models.sir:StarsimSIR
STUDY_FILE  := study.json
N_SAMPLES   := 256
N_REPLICATES := 10
NAMESPACE   := modelops-dask-dev

# --- Venv and tools ---
VENV    := .venv
BIN     := $(VENV)/bin
UV      := uv
PY      := $(BIN)/python

# Note: cb now automatically adds current directory to Python path
# No need to set PYTHONPATH manually anymore!

.PHONY: help
help:
	@echo "ModelOps Job Submission Workflow"
	@echo ""
	@echo "Setup:"
	@echo "  make venv            - Create venv"
	@echo "  make setup           - Install cb + modelops-bundle (+ mops) from local repos"
	@echo "  make setup-force     - Force reinstall local packages (after changes to contracts/calabaria)"
	@echo "  make mops-init       - Initialize ModelOps configuration (one-time setup)"
	@echo ""
	@echo "Bundle:"
	@echo "  make bundle-init     - Initialize bundle metadata in this dir"
	@echo "  make data            - Generate observed data for calibration"
	@echo "  make bundle-register - Register models for cloud job submission"
	@echo "  make bundle-status   - Show bundle status"
	@echo ""
	@echo "Workflow:"
	@echo "  make study           - Generate Sobol samples -> $(STUDY_FILE)"
	@echo "  make study-grid      - Generate 5x5 grid samples -> study-grid.json"
	@echo "  make study-tiny      - Generate tiny study (4 params x 2 reps) for testing"
	@echo "  make submit          - Submit Sobol study job with auto-push bundle"
	@echo "  make submit-grid     - Submit grid study job with auto-push bundle"
	@echo "  make submit-tiny     - Submit tiny study (fast test, 8 total tasks)"
	@echo "  make workflow        - setup -> bundle -> study -> submit"
	@echo ""
	@echo "Calibration:"
	@echo "  make calib-gen                    - Generate calibration spec (replicate_mean)"
	@echo "  make calib-submit                 - Generate and submit calibration job"
	@echo "  make calib-gen-per-replicate      - Generate spec with per_replicate target"
	@echo "  make calib-submit-per-replicate   - Submit per_replicate calibration"
	@echo ""
	@echo "Monitoring:"
	@echo "  make status          - k8s job + pods"
	@echo "  make logs            - tail first job pod logs"
	@echo "  make download        - Download job Parquet files from Azure"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean           - remove everything, including venv and study outputs"
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

# Force reinstall of local packages (useful after making changes to contracts/calabaria/etc)
.PHONY: setup-force
setup-force: $(VENV)
	@echo "Force reinstalling local packages..."
	@rm -f $(VENV)/.installed
	@$(MAKE) setup

# Initialize ModelOps configuration (one-time setup)
.PHONY: mops-init
mops-init: $(VENV)/.installed
	@if [ ! -f ~/.modelops/modelops.yaml ] && [ ! -f ~/.modelops/config.yaml ]; then \
		echo "Initializing ModelOps configuration..."; \
		$(BIN)/mops init; \
	else \
		echo "ModelOps already configured"; \
		if [ -f ~/.modelops/modelops.yaml ]; then \
			echo "  Unified config: ~/.modelops/modelops.yaml"; \
		else \
			echo "  Legacy config: ~/.modelops/config.yaml"; \
		fi; \
	fi

# --- Data generation ---

# Generate observed data for calibration
.PHONY: data
data: data/observed_incidence.csv data/observed_trajectories.parquet

data/observed_incidence.csv: $(VENV)/.installed generate_observed_data.py
	@mkdir -p data
	@echo "Generating observed data with true parameters (beta=0.08, dur_inf=5.0)..."
	$(PY) generate_observed_data.py

# Also create parquet version for compatibility
data/observed_trajectories.parquet: data/observed_incidence.csv
	@touch data/observed_trajectories.parquet
	@echo "Created placeholder parquet file (real conversion would happen here)"

# --- Bundle commands ---

.PHONY: bundle-init
bundle-init: $(VENV)/.installed
	@if [ ! -d .modelops-bundle ]; then \
		echo "Initializing bundle project..."; \
		$(BIN)/modelops-bundle init .; \
		echo "dev" > .modelops-bundle/env; \
	else \
		echo "Bundle already initialized (.modelops-bundle exists)"; \
		[ -f .modelops-bundle/env ] || echo "dev" > .modelops-bundle/env; \
	fi

.PHONY: bundle-status
bundle-status: $(VENV)/.installed
	$(BIN)/modelops-bundle status

.PHONY: bundle-register
bundle-register: $(VENV)/.installed data
	# Register the model for cloud job submission (auto code deps, auto prune)
	$(BIN)/modelops-bundle register-model models/sir.py --auto-code --regen-all
	# Register targets - auto-discovers decorated functions and their dependencies
	$(BIN)/modelops-bundle register-target targets/incidence.py --regen-all
	# Define calibration target set for convenience
	$(BIN)/modelops-bundle target-set set incidence \
		--target incidence_replicate_mean_target \
		--target incidence_per_replicate_target
	@echo "✓ Registered SIR model and calibration targets"

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
		--scramble
	@echo "✓ Wrote $(STUDY_FILE) with $(N_REPLICATES) replicates per parameter set"

.PHONY: study-grid
study-grid: study-grid.json

study-grid.json: $(VENV)/.installed
	$(BIN)/cb sampling grid $(MODEL_CLASS) \
		--scenario baseline \
		--grid-points 60 \
		--n-replicates 20 \
		--output study-grid.json \
		--seed 42
	@echo "✓ Wrote study-grid.json with grid points, 20 replicates each"

.PHONY: study-tiny
study-tiny: study-tiny.json

study-tiny.json: $(VENV)/.installed
	$(BIN)/cb sampling sobol $(MODEL_CLASS) \
		--scenario baseline \
		--n-samples 4 \
		--n-replicates 2 \
		--output study-tiny.json \
		--seed 42 \
		--scramble
	@echo "✓ Wrote study-tiny.json with 4 params, 2 replicates each (for quick testing)"

.PHONY: submit
submit: bundle-init bundle-register $(STUDY_FILE) $(VENV)/.installed
	# Submit with auto-push from current directory (default)
	$(BIN)/mops jobs submit $(abspath $(STUDY_FILE))
	@echo "✓ Submitted job with auto-pushed bundle"

.PHONY: submit-grid
submit-grid: bundle-init bundle-register study-grid.json $(VENV)/.installed
	# Submit grid study with auto-push from current directory (default)
	$(BIN)/mops jobs submit $(abspath study-grid.json)
	@echo "✓ Submitted grid study job with auto-pushed bundle"

.PHONY: submit-tiny
submit-tiny: bundle-init bundle-register study-tiny.json $(VENV)/.installed
	# Submit tiny study for quick testing (4 params x 2 replicates = 8 tasks)
	$(BIN)/mops jobs submit $(abspath study-tiny.json)
	@echo "✓ Submitted tiny study job (4 params, 2 replicates) with auto-pushed bundle"

.PHONY: workflow
workflow: setup bundle-init bundle-register study submit
	@echo "✓ Workflow complete. Try: make status"

# --- Calibration ---

.PHONY: calib-gen calib-submit calib-gen-per-replicate calib-submit-per-replicate

# Generate calibration spec with replicate_mean target
calib-gen: $(VENV)/.installed data
	@echo "Generating calibration spec with incidence_replicate_mean_target..."
	$(BIN)/cb calibration optuna $(MODEL_CLASS) \
	  data/observed_incidence.csv \
	  beta:0.01:0.2,dur_inf:3:10 \
	  --target-set incidence \
	  --max-trials 100 \
	  --batch-size 4 \
	  --n-replicates 10 \
	  --name incidence-mean \
	  --output calibration_spec.json
	@echo "✓ Generated calibration_spec.json"

# Submit calibration job
calib-submit: bundle-init bundle-register calib-gen $(VENV)/.installed
	@echo "Submitting calibration job..."
	$(BIN)/mops jobs submit $(abspath calibration_spec.json) --target-set incidence
	@echo "✓ Calibration job submitted"

# Generate calibration spec with per_replicate target (for comparison)
calib-gen-per-replicate: $(VENV)/.installed data
	@echo "Generating calibration spec with incidence_per_replicate_target..."
	$(BIN)/cb calibration optuna $(MODEL_CLASS) \
	  data/observed_incidence.csv \
	  beta:0.01:0.2,dur_inf:3:10 \
	  --target incidence_per_replicate_target \
	  --max-trials 100 \
	  --batch-size 4 \
	  --n-replicates 10 \
	  --name incidence-per-replicate \
	  --output calibration_spec_per_replicate.json
	@echo "✓ Generated calibration_spec_per_replicate.json"

# Submit per_replicate calibration job
calib-submit-per-replicate: bundle-init bundle-register calib-gen-per-replicate $(VENV)/.installed
	@echo "Submitting per_replicate calibration job..."
	$(BIN)/mops jobs submit $(abspath calibration_spec_per_replicate.json) \
		--target incidence_per_replicate_target
	@echo "✓ Per-replicate calibration job submitted"

# --- Monitoring ---

.PHONY: status
status:
	kubectl get jobs -n $(NAMESPACE) || true
	kubectl get pods -n $(NAMESPACE) | grep job || true

.PHONY: logs
logs:
	@pod="$$(kubectl get pods -n $(NAMESPACE) -o name | grep job | head -1)"; \
	[ -n "$$pod" ] && kubectl logs -n $(NAMESPACE) "$$pod" --tail=50 || echo "No job pod found"

.PHONY: download
download:
	@echo "Downloading job loss data from Azure..."
	@# Get the latest job ID from Kubernetes
	@JOB_ID=$$(kubectl get pods -n $(NAMESPACE) -l app=job-runner --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.labels.job-id}' 2>/dev/null); \
	if [ -z "$$JOB_ID" ]; then \
		echo "No job ID found. Specify with JOB_ID=job-xxx make download"; \
		exit 1; \
	fi; \
	echo "Job ID: $$JOB_ID"; \
	echo ""; \
	mkdir -p results/losses; \
	echo "Downloading Parquet files..."; \
	az storage blob download-batch \
		--account-name modelopsdevvsba1f2 \
		--source results \
		--pattern "views/jobs/$$JOB_ID/targets/*/data.parquet" \
		--destination results/losses \
		--auth-mode key 2>/dev/null || \
		echo "Download failed. Check Azure credentials."; \
	echo ""; \
	if [ -d "results/losses/views" ]; then \
		echo "Downloaded files:"; \
		find results/losses -name "*.parquet" -type f | while read f; do \
			echo "  $$f ($$(stat -f%z "$$f" 2>/dev/null || stat -c%s "$$f" 2>/dev/null) bytes)"; \
		done; \
	else \
		echo "No files downloaded"; \
	fi

# --- Cleanup ---

.PHONY: clean-data
clean-data:
	rm -rf data/

.PHONY: clean-bundle
clean-bundle:
	rm -rf .modelops-bundle pyproject.toml

.PHONY: clean
clean: clean-bundle clean-data
	rm -f $(STUDY_FILE) study-grid.json study-tiny.json sobol.json calibration_spec*.json test*.json
	rm -rf $(VENV)
	@echo "✓ Clean complete"
