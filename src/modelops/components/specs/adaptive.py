"""Adaptive infrastructure configuration models with validation."""

from typing import Dict, Any, Optional, List, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ..config_base import ConfigModel
from .common import ResourceRequirements
from ...images import get_image_config


class PersistenceConfig(BaseModel):
    """Persistence configuration for stateful components."""
    enabled: bool = True
    size: str = Field("10Gi", pattern=r"^\d+[KMGT]i$")
    storageClass: str = Field("managed-csi", alias="storage_class")
    
    model_config = ConfigDict(populate_by_name=True)


class CentralStoreConfig(BaseModel):
    """Central store configuration for distributed coordination."""
    kind: Literal["postgres", "redis", "none"] = "postgres"
    mode: Literal["in-cluster", "external"] = "in-cluster"
    persistence: Optional[PersistenceConfig] = Field(default_factory=PersistenceConfig)
    database: str = "optuna"
    user: str = "optuna_user"
    
    # External connection details (when mode="external")
    host: Optional[str] = None
    port: Optional[int] = None
    password: Optional[str] = None
    
    model_config = ConfigDict(populate_by_name=True)
    
    @model_validator(mode="after")
    def validate_external_fields(self):
        """Ensure external mode has required connection details."""
        if self.mode == "external":
            if not self.host:
                raise ValueError("host is required when mode is 'external'")
            if not self.port:
                raise ValueError("port is required when mode is 'external'")
        return self


class WorkerResourceConfig(BaseModel):
    """Worker resource requirements - kept light for adaptive workers."""
    requests: Dict[str, str] = Field(
        default_factory=lambda: {"cpu": "100m", "memory": "256Mi"}
    )
    limits: Dict[str, str] = Field(
        default_factory=lambda: {"cpu": "500m", "memory": "512Mi"}
    )
    
    @field_validator("requests", "limits")
    @classmethod
    def validate_resources(cls, v):
        """Validate resource format."""
        if "cpu" in v:
            # Basic validation for CPU format (e.g., "100m", "0.5", "2")
            cpu = v["cpu"]
            if not (cpu.endswith("m") or cpu.replace(".", "").isdigit()):
                raise ValueError(f"Invalid CPU format: {cpu}")
        
        if "memory" in v:
            # Basic validation for memory format (e.g., "1Gi", "512Mi")
            memory = v["memory"]
            if not any(memory.endswith(suffix) for suffix in ["Ki", "Mi", "Gi", "Ti"]):
                raise ValueError(f"Invalid memory format: {memory}")
        
        return v


class WorkersConfig(BaseModel):
    """Worker configuration for adaptive infrastructure."""
    # Use centralized image configuration as default
    _img_config = get_image_config()
    image: str = Field(default_factory=lambda: WorkersConfig._img_config.adaptive_worker_image())
    replicas: int = Field(2, ge=1, le=100)
    resources: WorkerResourceConfig = Field(default_factory=WorkerResourceConfig)
    command: Optional[List[str]] = None
    
    model_config = ConfigDict(populate_by_name=True)


# Removed WorkspaceRefConfig - workspace connection is through StackReferences, not user config


class AdaptiveConfig(ConfigModel):
    """
    Adaptive infrastructure configuration with validation.
    
    This configuration defines stateful components needed by optimization
    algorithms like Optuna, including databases, caches, and worker specifications.
    """
    version: int = Field(1, ge=1, le=1)  # Currently only version 1
    algorithm: Literal["optuna", "hyperopt", "custom"] = "optuna"
    
    central_store: Optional[CentralStoreConfig] = Field(
        default_factory=CentralStoreConfig
    )
    workers: Optional[WorkersConfig] = Field(default_factory=WorkersConfig)
    
    # Optional algorithm-specific configuration
    storage_connection: Optional[str] = None
    
    model_config = ConfigDict(
        populate_by_name=True,
        extra="allow"  # Allow additional fields for forward compatibility
    )
    
    @field_validator("algorithm")
    @classmethod
    def validate_algorithm(cls, v):
        """Validate algorithm is supported."""
        supported = ["optuna", "hyperopt", "custom"]
        if v not in supported:
            raise ValueError(f"Algorithm '{v}' not supported. Choose from: {', '.join(supported)}")
        return v
    
    def to_pulumi_config(self) -> Dict[str, Any]:
        """
        Convert to dictionary for Pulumi consumption.
        
        Returns flattened configuration suitable for AdaptiveInfra component.
        """
        config = self.model_dump(by_alias=False, exclude_unset=False, exclude={'workspace_ref'})
        
        # Flatten some nested configs for backward compatibility
        if self.workers:
            worker_dict = self.workers.model_dump(by_alias=False)
            # Move worker image and command to top level for compatibility
            if "image" in worker_dict:
                config["image"] = worker_dict["image"]
            if "command" in worker_dict:
                config["command"] = worker_dict["command"]
            if "replicas" in worker_dict:
                config["workers"] = {"replicas": worker_dict["replicas"]}
        
        return config
    
    def get_namespace(self, env: str, name: str) -> str:
        """
        Get namespace for adaptive infrastructure.
        
        Args:
            env: Environment name
            name: Infrastructure name
            
        Returns:
            Kubernetes namespace name
        """
        # Always use the adaptive namespace pattern for adaptive infrastructure
        # The workspace_ref namespace is for referencing the Dask workspace, not for this infra
        return f"modelops-adaptive-{env}-{name}"
    
    def get_central_store_config(self) -> Optional[Dict[str, Any]]:
        """Get central store configuration as dict."""
        if not self.central_store or self.central_store.kind == "none":
            return None
        return self.central_store.model_dump(by_alias=False, exclude_unset=False)
    
    def get_worker_config(self) -> Dict[str, Any]:
        """Get worker configuration as dict."""
        if not self.workers:
            return {}
        return self.workers.model_dump(by_alias=False, exclude_unset=False)
