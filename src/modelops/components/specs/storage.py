"""Storage configuration models with validation."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..config_base import ConfigModel

# Standard required containers for ModelOps - NOT configurable
STANDARD_CONTAINERS: list[dict[str, any]] = [
    {
        "name": "bundle-blobs",
        "purpose": "Large files (>50MB) referenced by OCI bundles in ACR",
        "access_level": "private",
        "lifecycle_days": None,
    },
    {
        "name": "workspace",
        "purpose": "Scratch space for active computations",
        "access_level": "private",
        "lifecycle_days": 7,  # Auto-cleanup after 7 days
    },
    {
        "name": "results",
        "purpose": "Permanent experiment outputs and metrics",
        "access_level": "private",
        "lifecycle_days": None,
    },
    {
        "name": "jobs",
        "purpose": "Kubernetes job definitions and metadata",
        "access_level": "private",
        "lifecycle_days": None,
    },
]


class ContainerConfig(BaseModel):
    """Configuration for a storage container."""

    name: str = Field(..., pattern="^[a-z0-9-]+$", min_length=3, max_length=63)
    purpose: str | None = None
    access_level: Literal["private", "blob", "container"] = "private"
    lifecycle_days: int | None = None  # Delete after N days (for workspace)

    @field_validator("name")
    @classmethod
    def validate_container_name(cls, v):
        """Ensure container name meets Azure requirements."""
        if "--" in v or v.startswith("-") or v.endswith("-"):
            raise ValueError(f"Invalid container name format: {v}")
        return v


class StorageConfig(ConfigModel):
    """
    Storage configuration with validation.

    Creates standard blob storage containers required for ModelOps:
    - bundle-blobs: Large files referenced by OCI bundles
    - workspace: Scratch space with auto-cleanup
    - results: Permanent experiment outputs
    - jobs: Kubernetes job metadata
    """

    version: int = Field(1, ge=1, le=1)
    provider: Literal["azure"] = "azure"  # Only Azure for MVP

    # Storage account settings (optional, with smart defaults)
    account_name: str | None = None  # Auto-generated if not provided
    resource_group: str | None = None  # Explicit resource group to use
    username: str | None = None  # Explicit username for naming (usually auto-detected)
    sku: Literal["Standard_LRS", "Standard_GRS", "Premium_LRS"] = "Standard_LRS"
    location: str | None = None  # Inherit from infrastructure

    # Containers are NOT configurable - using standard set
    @property
    def containers(self) -> list[ContainerConfig]:
        """Get standard containers (not configurable)."""
        return [ContainerConfig(**c) for c in STANDARD_CONTAINERS]

    def to_pulumi_config(self) -> dict:
        """Convert to dictionary for Pulumi consumption."""
        config = self.model_dump(by_alias=False, exclude_unset=False)
        # Add containers property since it's computed
        config["containers"] = self.containers
        return config

    def get_container_names(self) -> list[str]:
        """Get list of container names."""
        return [c.name for c in self.containers]

    def get_container(self, name: str) -> ContainerConfig | None:
        """Get container config by name."""
        for container in self.containers:
            if container.name == name:
                return container
        return None
