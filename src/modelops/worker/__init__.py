"""ModelOps Worker components for Dask integration.

This module implements the WorkerPlugin architecture for clean
lifecycle management and dependency injection.
"""

from .plugin import ModelOpsWorkerPlugin
from .config import RuntimeConfig

__all__ = [
    "ModelOpsWorkerPlugin",
    "RuntimeConfig",
]