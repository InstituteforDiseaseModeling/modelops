"""Infrastructure components using Pulumi ComponentResources.

This module provides reusable infrastructure components that encapsulate
cloud resources and expose typed outputs for cross-plane communication.
"""

from .azure import AzureModelOpsInfra

__all__ = ["AzureModelOpsInfra"]