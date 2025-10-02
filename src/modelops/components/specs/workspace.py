"""Workspace configuration models with validation."""

from typing import Dict, Any, Optional, List, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from ..config_base import ConfigModel


class AutoscalingConfig(BaseModel):
    """Configuration for Dask worker autoscaling."""
    enabled: bool = Field(True, description="Enable autoscaling")
    type: Literal["hpa", "dask-adaptive"] = Field(
        "hpa",
        description="Autoscaling type (HPA for now, dask-adaptive later)"
    )
    min_workers: int = Field(2, ge=0, description="Minimum number of workers")
    max_workers: int = Field(20, ge=1, description="Maximum number of workers")
    target_cpu: int = Field(
        70, ge=10, le=100,
        description="Target CPU utilization percentage for HPA"
    )
    scale_down_delay: int = Field(
        300, ge=0,
        description="Delay in seconds before scaling down"
    )

    @field_validator("max_workers")
    @classmethod
    def validate_max_workers(cls, v, values):
        """Ensure max >= min workers."""
        min_workers = values.data.get("min_workers", 2)
        if v < min_workers:
            raise ValueError(f"max_workers ({v}) must be >= min_workers ({min_workers})")
        return v


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

        # Require image specifications - no defaults allowed
        if "scheduler" not in v or not isinstance(v["scheduler"], dict):
            raise ValueError("spec.scheduler is required")
        if "image" not in v["scheduler"]:
            raise ValueError("spec.scheduler.image is required - no default images allowed")

        if "workers" not in v or not isinstance(v["workers"], dict):
            raise ValueError("spec.workers is required")
        if "image" not in v["workers"]:
            raise ValueError("spec.workers.image is required - no default images allowed")

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

    def get_autoscaling_config(self) -> AutoscalingConfig:
        """Extract autoscaling configuration from spec."""
        autoscaling_dict = self.spec.get("autoscaling", {})
        if isinstance(autoscaling_dict, dict):
            return AutoscalingConfig(**autoscaling_dict)
        return AutoscalingConfig()  # Return defaults