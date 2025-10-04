"""Storage configuration models with validation."""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator
from ..config_base import ConfigModel


class ContainerConfig(BaseModel):
    """Configuration for a storage container."""
    name: str = Field(..., pattern="^[a-z0-9-]+$", min_length=3, max_length=63)
    purpose: Optional[str] = None
    access_level: Literal["private", "blob", "container"] = "private"
    lifecycle_days: Optional[int] = None  # Delete after N days (for workspace)
    
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
    
    Defines blob storage containers for ModelOps components to store
    bundles, results, workspace scratch, and task definitions.
    """
    version: int = Field(1, ge=1, le=1)
    provider: Literal["azure"] = "azure"  # Only Azure for MVP
    
    # Storage account settings (optional, with smart defaults)
    account_name: Optional[str] = None  # Auto-generated if not provided
    resource_group: Optional[str] = None  # Explicit resource group to use
    username: Optional[str] = None  # Explicit username for naming (usually auto-detected)
    sku: Literal["Standard_LRS", "Standard_GRS", "Premium_LRS"] = "Standard_LRS"
    location: Optional[str] = None  # Inherit from infrastructure
    
    # Containers to create with defaults
    containers: List[ContainerConfig] = Field(
        default_factory=lambda: [
            ContainerConfig(
                name="bundles", 
                purpose="OCI artifacts and small model packages (<100MB)"
            ),
            ContainerConfig(
                name="bundle-blobs",
                purpose="Large files referenced by bundles (>100MB, sharded storage)"
            ),
            ContainerConfig(
                name="workspace",
                purpose="Scratch space for active computations",
                lifecycle_days=7  # Auto-cleanup
            ),
            ContainerConfig(
                name="results",
                purpose="Permanent experiment outputs and metrics"
            ),
            ContainerConfig(
                name="tasks",
                purpose="ZeroOps task registry definitions"
            ),
        ]
    )
    
    def to_pulumi_config(self) -> dict:
        """Convert to dictionary for Pulumi consumption."""
        return self.model_dump(by_alias=False, exclude_unset=False)
    
    def get_container_names(self) -> List[str]:
        """Get list of container names."""
        return [c.name for c in self.containers]
    
    def get_container(self, name: str) -> Optional[ContainerConfig]:
        """Get container config by name."""
        for container in self.containers:
            if container.name == name:
                return container
        return None