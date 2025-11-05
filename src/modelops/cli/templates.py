"""Infrastructure configuration templates.

Single source of truth for default infrastructure configurations.
"""

from datetime import datetime

DEFAULT_INFRA_TEMPLATE = """# ModelOps Infrastructure Configuration
# Generated: {timestamp}
# Run 'mops infra up' to deploy (uses this file by default)

schemaVersion: 1

# Cluster configuration
cluster:
  provider: azure
  subscription_id: "{subscription_id}"
  resource_group: modelops-{username}
  location: {location}
  aks:
    name: modelops-cluster
    kubernetes_version: "{k8s_version}"
    node_pools:
      - name: system
        mode: System
        vm_size: Standard_B2s
        count: 1
      - name: workers
        mode: User
        vm_size: Standard_B4ms
        min: 1
        max: 3

# Storage configuration
storage:
  account_tier: Standard

# Registry configuration
registry:
  sku: Basic

# Workspace configuration
workspace:
  apiVersion: modelops/v1
  kind: Workspace
  metadata:
    name: main-workspace
  spec:
    scheduler:
      image: ghcr.io/institutefordiseasemodeling/modelops-dask-scheduler:latest
      replicas: 1
    workers:
      image: ghcr.io/institutefordiseasemodeling/modelops-dask-worker:latest
      replicas: 2
      processes: 4  # Increased to prevent aggregation deadlock
      threads: 1
"""


def get_infra_template(**kwargs) -> str:
    """Get infrastructure template with substitutions.

    Args:
        **kwargs: Template substitution values:
            - timestamp: ISO format timestamp (default: now)
            - subscription_id: Azure subscription ID
            - username: User identifier for resource naming
            - location: Azure region (default: eastus2)
            - k8s_version: Kubernetes version (default: 1.30)

    Returns:
        Formatted infrastructure YAML template
    """
    kwargs.setdefault("timestamp", datetime.now().isoformat())
    kwargs.setdefault("location", "eastus2")
    kwargs.setdefault("k8s_version", "1.30")
    return DEFAULT_INFRA_TEMPLATE.format(**kwargs)
