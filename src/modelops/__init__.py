"""ModelOps - Infrastructure orchestration for ML experimentation."""

from ._version import __version__, get_version, get_version_info

# Make key components available at package level
from .services import DaskSimulationService, LocalSimulationService

__all__ = [
    "LocalSimulationService",
    "DaskSimulationService",
    "__version__",
    "get_version",
    "get_version_info",
]
