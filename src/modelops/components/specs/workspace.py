"""Workspace configuration models with validation."""

from typing import Dict, Any, Optional, List, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from ..config_base import ConfigModel


class WorkspaceConfig(ConfigModel):
    """
    Dask workspace configuration.
    
    Validates top-level structure but keeps spec flexible for forward compatibility.
    The DaskWorkspace component handles detailed parsing of scheduler/worker configs.
    """
    apiVersion: str = Field("modelops/v1", alias="api_version")
    kind: Literal["Workspace"] = "Workspace"
    metadata: Dict[str, Any]  # name, namespace, labels, etc.
    spec: Dict[str, Any]  # scheduler, workers, tolerations, etc.
    
    model_config = ConfigDict(
        populate_by_name=True,  # Accept both field names and aliases
        extra="allow"  # Allow additional fields for forward compatibility
    )
    
    @field_validator("apiVersion", mode="before")
    @classmethod
    def normalize_api_version(cls, v):
        """Accept both apiVersion and api_version."""
        return v or "modelops/v1"
    
    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v):
        """Ensure metadata has required fields."""
        if not isinstance(v, dict):
            raise ValueError("metadata must be a dictionary")
        if "name" not in v:
            raise ValueError("metadata.name is required")
        return v
    
    @field_validator("spec")
    @classmethod
    def validate_spec(cls, v):
        """Basic validation of spec structure."""
        if not isinstance(v, dict):
            raise ValueError("spec must be a dictionary")
        
        # We could validate that scheduler/workers exist, but keeping it flexible
        # to allow for different workspace types in the future
        return v
    
    def get_namespace(self, env: str) -> str:
        """Get namespace from metadata or generate default."""
        if "namespace" in self.metadata:
            return self.metadata["namespace"]
        
        # Use centralized naming convention
        from ...core import StackNaming
        return StackNaming.get_namespace("dask", env)
    
    def get_scheduler_config(self) -> Dict[str, Any]:
        """Extract scheduler configuration from spec."""
        return self.spec.get("scheduler", {})
    
    def get_workers_config(self) -> Dict[str, Any]:
        """Extract workers configuration from spec."""
        return self.spec.get("workers", {})
    
    def get_tolerations(self) -> List[Dict[str, Any]]:
        """Extract tolerations from spec."""
        return self.spec.get("tolerations", [])