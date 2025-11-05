"""Centralized image configuration for ModelOps.

This module provides a single source of truth for all container image
references used throughout the codebase.
"""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ImageConfig(BaseModel):
    """Container image configuration."""

    version: int = Field(1)
    images: dict[str, str] = Field(
        default_factory=dict, description="Map of image keys to full references"
    )

    @classmethod
    @lru_cache(maxsize=1)
    def from_yaml(cls, path: Path | None = None) -> "ImageConfig":
        """Load image configuration from YAML file.

        Args:
            path: Path to YAML file (defaults to modelops-images.yaml)

        Returns:
            ImageConfig instance
        """
        if path is None:
            # Look in current directory first
            path = Path("modelops-images.yaml")
            if not path.exists():
                # Look in package directory (for installed package)
                path = Path(__file__).parent / "modelops-images.yaml"

        if not path.exists():
            raise FileNotFoundError(f"Image configuration not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        return cls.model_validate(data)

    def get(self, image_key: str) -> str:
        """Get image reference by key.

        Args:
            image_key: Image key (e.g., "scheduler", "worker")

        Returns:
            Full image reference

        Raises:
            KeyError: If image key not found
        """
        if image_key not in self.images:
            available = ", ".join(sorted(self.images.keys()))
            raise KeyError(f"Unknown image key '{image_key}'. Available: {available}")
        return self.images[image_key]

    # Convenience methods for common images
    def scheduler_image(self) -> str:
        """Get scheduler image reference."""
        return self.get("scheduler")

    def worker_image(self) -> str:
        """Get worker image reference."""
        return self.get("worker")

    def runner_image(self) -> str:
        """Get runner image reference."""
        return self.get("runner")

    def adaptive_worker_image(self) -> str:
        """Get adaptive worker image reference."""
        return self.get("adaptive-worker")


# Global singleton - loaded on first access
_IMAGE_CONFIG: ImageConfig | None = None


def get_image_config() -> ImageConfig:
    """Get the global image configuration.

    Returns:
        Singleton ImageConfig instance
    """
    global _IMAGE_CONFIG
    if _IMAGE_CONFIG is None:
        _IMAGE_CONFIG = ImageConfig.from_yaml()
    return _IMAGE_CONFIG
