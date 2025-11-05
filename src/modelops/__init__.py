"""ModelOps - Infrastructure orchestration for ML experimentation."""

__version__ = "0.1.0"

# Make key components available at package level
from .services import DaskSimulationService, LocalSimulationService

__all__ = ["LocalSimulationService", "DaskSimulationService", "__version__"]
