# ModelOps Makefile - Minimal setup for local development

# Variables
MOPS := uv run mops
PROVIDER_DIR := ~/.modelops/providers
WORKSPACE_NAME := default

# Setup OrbStack provider config
setup-orbstack:
	@mkdir -p $(PROVIDER_DIR)
	@echo "Creating OrbStack provider config..."
	@printf '%s\n' \
		'kind: Provider' \
		'provider: orbstack' \
		'spec:' \
		'  context: orbstack' \
		'  storage:' \
		'    type: emptydir' \
		> $(PROVIDER_DIR)/orbstack.yaml
	@echo "✓ OrbStack provider configured"

# Install dependencies
install:
	pip install -e .

# OrbStack-specific workspace commands
orbstack-up:
	$(MOPS) workspace up --name $(WORKSPACE_NAME) --provider orbstack

orbstack-down:
	$(MOPS) workspace down --name $(WORKSPACE_NAME)

orbstack-forward:
	$(MOPS) workspace port-forward --name $(WORKSPACE_NAME)

# Show workspace status
status:
	$(MOPS) workspace status --name $(WORKSPACE_NAME)

# Clean everything
clean:
	rm -rf ~/.modelops/state.json
	rm -rf ~/.modelops/providers

.PHONY: setup-orbstack install orbstack-up orbstack-down orbstack-forward status clean
