"""ModelOps Worker components for Dask integration.

This module implements the WorkerPlugin architecture for clean
lifecycle management and dependency injection.
"""

from .config import RuntimeConfig
from .plugin import ModelOpsWorkerPlugin

__all__ = [
    "ModelOpsWorkerPlugin",
    "RuntimeConfig",
]
