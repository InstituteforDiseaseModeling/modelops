"""Configuration specification models for ModelOps."""

from .adaptive import (
    AdaptiveConfig,
    CentralStoreConfig,
    PersistenceConfig,
    WorkerResourceConfig,
    WorkersConfig,
)
from .azure import (
    ACRConfig,
    ACRSku,
    AKSConfig,
    AzureProviderConfig,
    NodePool,
    PoolMode,
    Taint,
    TaintEffect,
)
from .common import (
    EnvVar,
    ResourceRequirements,
    Toleration,
)
from .infra import (
    UnifiedInfraSpec,
)
from .workspace import (
    AutoscalingConfig,
    WorkspaceConfig,
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
