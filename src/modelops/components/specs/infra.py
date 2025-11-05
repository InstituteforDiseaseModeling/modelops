"""Unified infrastructure specification."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from ..config_base import ConfigModel
from .azure import AzureProviderConfig
from .storage import StorageConfig
from .workspace import WorkspaceConfig


class UnifiedInfraSpec(ConfigModel):
    """
    Unified infrastructure specification.

    Single YAML file to define all infrastructure components.
    Components are optional and provisioned only if specified.

    Example YAML:
        schemaVersion: 1
        cluster:
          provider: azure
          subscription_id: xxx
          resource_group: modelops
          location: eastus2
        storage:
          containers:
            - name: bundles
            - name: results
        workspace:
          apiVersion: modelops/v1
          kind: Workspace
          metadata:
            name: dask
          spec:
            autoscaling:
              enabled: true
              min_workers: 2
              max_workers: 20
    """

    schema_version: int = Field(1, ge=1, le=1, alias="schemaVersion")

    # Optional registry configuration
    registry: dict[str, Any] | None = Field(None, description="Container registry configuration")

    # Cluster configuration (was "infra")
    cluster: AzureProviderConfig | None = Field(
        None, description="Kubernetes cluster configuration"
    )

    # Storage configuration
    storage: StorageConfig | None = Field(None, description="Blob storage configuration")

    # Workspace configuration
    workspace: WorkspaceConfig | None = Field(None, description="Dask workspace configuration")

    # Control behavior
    continue_on_error: bool = Field(
        False,
        alias="continueOnError",
        description="Continue provisioning even if a component fails",
    )

    # Optional explicit dependencies
    depends_on: dict[str, list[str]] | None = Field(
        None, alias="dependsOn", description="Explicit component dependencies"
    )

    @classmethod
    def from_yaml(cls, path: str) -> "UnifiedInfraSpec":
        """
        Load from YAML file.

        Args:
            path: Path to YAML file

        Returns:
            UnifiedInfraSpec instance
        """
        with open(Path(path)) as f:
            data = yaml.safe_load(f)

        # Handle nested configurations
        if "cluster" in data and isinstance(data["cluster"], dict):
            data["cluster"] = AzureProviderConfig(**data["cluster"])

        if "storage" in data and isinstance(data["storage"], dict):
            data["storage"] = StorageConfig(**data["storage"])

        if "workspace" in data and isinstance(data["workspace"], dict):
            data["workspace"] = WorkspaceConfig(**data["workspace"])

        return cls(**data)

    @classmethod
    def empty(cls) -> "UnifiedInfraSpec":
        """Create an empty spec with defaults."""
        return cls(schema_version=1)

    def get_components(self) -> list[str]:
        """
        Get list of defined components.

        Returns:
            List of component names that are configured
        """
        components = []

        # Resource group is always needed for Azure components
        # Add it first if any Azure components are configured
        if self.registry or self.cluster or self.storage:
            components.append("resource_group")

        if self.registry:
            components.append("registry")
        if self.cluster:
            components.append("cluster")
        if self.storage:
            components.append("storage")
        if self.workspace:
            components.append("workspace")
        return components

    def to_yaml(self, path: str | None = None) -> str:
        """
        Export to YAML format.

        Args:
            path: Optional path to write YAML file

        Returns:
            YAML string representation
        """
        # Convert to dict
        data = self.model_dump(by_alias=True, exclude_unset=True)

        # Convert nested configs to dicts
        if "cluster" in data and hasattr(data["cluster"], "to_pulumi_config"):
            data["cluster"] = data["cluster"].to_pulumi_config()
        if "storage" in data and hasattr(data["storage"], "to_pulumi_config"):
            data["storage"] = data["storage"].to_pulumi_config()
        if "workspace" in data and hasattr(data["workspace"], "to_pulumi_config"):
            data["workspace"] = data["workspace"].to_pulumi_config()

        yaml_str = yaml.dump(data, default_flow_style=False, sort_keys=False)

        if path:
            Path(path).write_text(yaml_str)

        return yaml_str

    def validate_dependencies(self) -> bool:
        """
        Validate that dependencies are satisfied.

        Returns:
            True if all dependencies are valid

        Raises:
            ValueError: If invalid dependencies detected
        """
        components = set(self.get_components())

        if self.depends_on:
            for component, deps in self.depends_on.items():
                if component not in components:
                    raise ValueError(f"Component '{component}' in dependsOn not defined")
                for dep in deps:
                    if dep not in components:
                        raise ValueError(
                            f"Dependency '{dep}' for component '{component}' not defined"
                        )

        # Check default dependencies
        if "workspace" in components:
            if "cluster" not in components:
                raise ValueError("Workspace requires cluster to be defined")

        return True
