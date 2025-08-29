"""Provider abstractions for cloud infrastructure."""

from .base import WorkspaceProvider
from .registry import ProviderRegistry

__all__ = ["WorkspaceProvider", "ProviderRegistry"]