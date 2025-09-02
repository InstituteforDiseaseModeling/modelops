"""Specification models for ModelOps configuration validation.

Provides Pydantic models for validating YAML configurations
for workspaces, adaptive runs, and other components.
"""

from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field, field_validator, ConfigDict


class ResourceRequirements(BaseModel):
    """Resource requirements for containers."""
    requests: Dict[str, str] = Field(default_factory=lambda: {"memory": "1Gi", "cpu": "1"})
    limits: Dict[str, str] = Field(default_factory=lambda: {"memory": "1Gi", "cpu": "1"})
    
    @field_validator("requests", "limits")
    @classmethod
    def validate_resources(cls, v):
        """Validate resource format."""
        for key in v:
            if key not in ["memory", "cpu", "nvidia.com/gpu"]:
                raise ValueError(f"Unknown resource type: {key}")
        return v


class EnvVar(BaseModel):
    """Environment variable specification."""
    name: str
    value: str
    value_from: Optional[Dict[str, Any]] = Field(None, alias="valueFrom")


class Toleration(BaseModel):
    """Kubernetes toleration specification."""
    key: str
    operator: str = "Equal"
    value: Optional[str] = None
    effect: Optional[str] = None


class SchedulerSpec(BaseModel):
    """Dask scheduler specification."""
    image: str = "ghcr.io/dask/dask:latest"
    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    node_selector: Optional[Dict[str, str]] = Field(None, alias="nodeSelector")
    env: List[EnvVar] = Field(default_factory=list)


class WorkersSpec(BaseModel):
    """Dask workers specification."""
    replicas: int = 3
    image: str = "ghcr.io/dask/dask:latest"
    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    node_selector: Optional[Dict[str, str]] = Field(None, alias="nodeSelector")
    threads: int = 2
    env: List[EnvVar] = Field(default_factory=list)


class WorkspaceMetadata(BaseModel):
    """Workspace metadata."""
    name: str
    namespace: Optional[str] = None
    labels: Optional[Dict[str, str]] = None
    annotations: Optional[Dict[str, str]] = None


class WorkspaceSpec(BaseModel):
    """Complete workspace specification."""
    model_config = ConfigDict(populate_by_name=True)
    
    api_version: str = Field("modelops/v1", alias="apiVersion")
    kind: str = "Workspace"
    metadata: WorkspaceMetadata
    spec: Dict[str, Any]
    
    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v):
        if v != "Workspace":
            raise ValueError(f"Invalid kind: {v}, expected 'Workspace'")
        return v
    
    def to_config_dict(self) -> Dict[str, Any]:
        """Convert to configuration dictionary for DaskWorkspace."""
        return self.model_dump(by_alias=True, exclude_unset=True)


class WorkspaceSpecV2(BaseModel):
    """Structured workspace specification with full validation."""
    model_config = ConfigDict(populate_by_name=True)
    
    api_version: str = Field("modelops/v1", alias="apiVersion")
    kind: str = "Workspace"
    metadata: WorkspaceMetadata
    spec: "WorkspaceSpecDetails"
    
    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v):
        if v != "Workspace":
            raise ValueError(f"Invalid kind: {v}, expected 'Workspace'")
        return v


class WorkspaceSpecDetails(BaseModel):
    """Detailed workspace specification."""
    model_config = ConfigDict(populate_by_name=True)
    
    scheduler: SchedulerSpec
    workers: WorkersSpec
    tolerations: Optional[List[Toleration]] = None
    image_pull_secrets: Optional[List[Dict[str, str]]] = Field(None, alias="imagePullSecrets")


# Update forward reference
WorkspaceSpecV2.model_rebuild()


class PersistenceSpec(BaseModel):
    """Persistence specification for stateful components."""
    model_config = ConfigDict(populate_by_name=True)
    
    size: str = "10Gi"
    storage_class: str = Field("default", alias="storageClass")


class CentralStoreSpec(BaseModel):
    """Central store (Postgres) specification."""
    persistence: PersistenceSpec
    version: str = "15"


class AdaptiveWorkersSpec(BaseModel):
    """Adaptive workers specification."""
    model_config = ConfigDict(populate_by_name=True)
    
    replicas: int = 1
    image: str
    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    node_selector: Optional[Dict[str, str]] = Field(None, alias="nodeSelector")


class AdaptiveAlgorithmSpec(BaseModel):
    """Adaptive algorithm configuration."""
    model_config = ConfigDict(populate_by_name=True)
    
    adapter_path: str = Field(..., alias="adapterPath")
    batch_size: int = Field(4, alias="batchSize")
    replicates: int = 10


class AdaptiveSpec(BaseModel):
    """Complete adaptive run specification."""
    model_config = ConfigDict(populate_by_name=True)
    
    api_version: str = Field("modelops/v1", alias="apiVersion")
    kind: str = "Adaptive"
    metadata: Dict[str, Any]
    spec: Dict[str, Any]
    
    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v):
        if v != "Adaptive":
            raise ValueError(f"Invalid kind: {v}, expected 'Adaptive'")
        return v