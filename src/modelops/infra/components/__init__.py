"""Infrastructure components using Pulumi ComponentResources.

This module provides reusable infrastructure components that encapsulate
cloud resources and expose typed outputs for cross-plane communication.
"""

from .adaptive import AdaptiveInfra
from .registry import ContainerRegistry
from .workspace import DaskWorkspace

# Only import Azure if azure dependencies are installed
try:
    from .azure import ModelOpsCluster
except ImportError:
    ModelOpsCluster = None

__all__ = ["ModelOpsCluster", "DaskWorkspace", "AdaptiveInfra", "ContainerRegistry"]
