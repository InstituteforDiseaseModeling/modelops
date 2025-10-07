# K8s Job Submission Workflow Test
# This Makefile simulates a user environment for testing ModelOps job submission

MODEL_CLASS := models.seir:StochasticSEIR
STUDY_FILE := study.json
N_SAMPLES := 20
TAG := seir-test

# Virtual environment for user-like setup
VENV := .venv
ACTIVATE := . $(VENV)/bin/activate
PYTHONPATH_PREFIX := PYTHONPATH=$(PWD):$$PYTHONPATH

.PHONY: help
help:
	@echo "ModelOps Job Submission Workflow"
	@echo ""
	@echo "Setup Commands:"
	@echo "  make setup        - Create user environment with calabaria & bundle"
	@echo "  make clean-all    - Clean everything including venv"
	@echo ""
	@echo "Bundle Commands:"
	@echo "  make bundle-init  - Initialize bundle tracking"
	@echo "  make bundle-add   - Add files to bundle tracking"
	@echo "  make bundle-status- Check bundle status"
	@echo "  make bundle-push  - Push model to registry"
	@echo ""
	@echo "Workflow Commands:"
	@echo "  make study        - Generate Sobol parameter samples"
	@echo "  make submit       - Submit job to K8s cluster"
	@echo "  make workflow     - Run full workflow"
	@echo ""
	@echo "Monitoring:"
	@echo "  make status       - Check job status"
	@echo "  make logs         - View job logs"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean        - Clean study files"
	@echo "  make clean-bundle - Clean bundle state"
	@echo "  make clean-all    - Clean everything"

.PHONY: setup
setup:
	@echo "Creating user-like environment..."
	uv venv $(VENV) --clear
	@echo "Installing packages with constraints..."
	$(ACTIVATE) && uv pip install -c ../../constraints.txt ../../../modelops-contracts
	$(ACTIVATE) && uv pip install -c ../../constraints.txt ../../../modelops-bundle
	$(ACTIVATE) && uv pip install -c ../../constraints.txt -e ../../../modelops-calabaria
	@echo "Setup complete. User tools (cb, modelops-bundle) installed in $(VENV)"

.PHONY: bundle-init
bundle-init:
	@echo "Initializing bundle project in current workflow..."
	@if [ ! -f pyproject.toml ]; then \
		echo "Creating pyproject.toml for workflow..."; \
		$(ACTIVATE) && modelops-bundle init .; \
	else \
		echo "Project already initialized (pyproject.toml exists)"; \
	fi

.PHONY: bundle-status
bundle-status:
	$(ACTIVATE) && modelops-bundle status

.PHONY: bundle-add
bundle-add:
	$(ACTIVATE) && modelops-bundle add models/
	@echo "Added SEIR model files to bundle tracking"

.PHONY: bundle-push
bundle-push:
	$(ACTIVATE) && modelops-bundle push --tag $(TAG)

.PHONY: bundle-resolve
bundle-resolve:
	@$(ACTIVATE) && modelops-bundle resolve $(TAG) | grep -o 'sha256:[a-f0-9]*'

.PHONY: study
study:
	$(ACTIVATE) && $(PYTHONPATH_PREFIX) cb sampling sobol $(MODEL_CLASS) \
		--scenario baseline \
		--n-samples $(N_SAMPLES) \
		--output $(STUDY_FILE) \
		--seed 42 \
		--scramble

.PHONY: submit
submit:
	cd ../.. && uv run --package modelops mops jobs submit examples/simulation-workflow/$(STUDY_FILE) \
		--bundle $$($(ACTIVATE) && cd ../.. && mops-bundle resolve $(TAG) | grep -o 'sha256:[a-f0-9]*') \
		--env dev

.PHONY: workflow
workflow: setup bundle-init bundle-add bundle-push study submit
	@echo "Workflow complete. Check status with: make status"

.PHONY: status
status:
	kubectl get jobs -n modelops-dask-dev
	kubectl get pods -n modelops-dask-dev | grep job

.PHONY: logs
logs:
	@echo "Fetching job logs..."
	@kubectl logs -n modelops-dask-dev $$(kubectl get pods -n modelops-dask-dev -o name | grep job | head -1) --tail=50

.PHONY: clean
clean: clean-study
	@echo "Cleaning generated files..."
	rm -f $(STUDY_FILE)

.PHONY: clean-bundle
clean-bundle:
	@echo "Cleaning bundle state..."
	rm -rf .modelops-bundle

.PHONY: clean-all
clean-all: clean clean-bundle
	@echo "Cleaning everything including virtual environment..."
	rm -rf $(VENV)
	@echo "Clean complete"

.PHONY: clean-study
clean-study:
	rm -f $(STUDY_FILE)