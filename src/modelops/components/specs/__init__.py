"""Configuration specification models for ModelOps."""

from .common import (
    ResourceRequirements,
    EnvVar,
    Toleration,
)

from .azure import (
    AzureProviderConfig,
    AKSConfig,
    NodePool,
    ACRConfig,
    Taint,
    PoolMode,
    TaintEffect,
    ACRSku,
)

from .workspace import (
    WorkspaceConfig,
)

from .adaptive import (
    AdaptiveConfig,
    CentralStoreConfig,
    WorkersConfig,
    PersistenceConfig,
    WorkerResourceConfig,
)

__all__ = [
    # Common models
    "ResourceRequirements",
    "EnvVar",
    "Toleration",
    
    # Azure models
    "AzureProviderConfig",
    "AKSConfig", 
    "NodePool",
    "ACRConfig",
    "Taint",
    "PoolMode",
    "TaintEffect",
    "ACRSku",
    
    # Workspace models
    "WorkspaceConfig",
    
    # Adaptive models
    "AdaptiveConfig",
    "CentralStoreConfig",
    "WorkersConfig",
    "PersistenceConfig",
    "WorkerResourceConfig",
]