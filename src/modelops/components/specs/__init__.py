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
    AutoscalingConfig,
)

from .adaptive import (
    AdaptiveConfig,
    CentralStoreConfig,
    WorkersConfig,
    PersistenceConfig,
    WorkerResourceConfig,
)

from .infra import (
    UnifiedInfraSpec,
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
    "AutoscalingConfig",

    # Adaptive models
    "AdaptiveConfig",
    "CentralStoreConfig",
    "WorkersConfig",
    "PersistenceConfig",
    "WorkerResourceConfig",

    # Unified infrastructure
    "UnifiedInfraSpec",
]