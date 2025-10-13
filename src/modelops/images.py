"""Centralized image configuration loader.

This module provides a single source of truth for all container image references
in the ModelOps system. It supports multiple profiles (prod, dev, local) and
can be overridden via the MOPS_IMAGE_PROFILE environment variable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


# Digest format validation
_DIGEST_RE = re.compile(r"^[A-Za-z0-9_+.-]+:[0-9a-fA-F]{32,}$")


class Registry(BaseModel):
    """Container registry configuration."""

    host: str = Field(..., description="Registry hostname (e.g., ghcr.io)")
    org: str = Field(..., description="Organization/namespace in registry")


class Image(BaseModel):
    """Individual image configuration."""

    name: str = Field(..., description="Image name without registry/org prefix")
    tag: Optional[str] = Field(None, description="Image tag (overrides default)")
    digest: Optional[str] = Field(None, description="Image digest for pinning")

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, v: Optional[str]) -> Optional[str]:
        """Validate digest format if provided."""
        if v is not None and not _DIGEST_RE.match(v):
            raise ValueError(f"Invalid digest format: {v}")
        return v

    def ref(self, registry: Registry, default_tag: str) -> str:
        """Generate full image reference.

        Args:
            registry: Registry configuration
            default_tag: Default tag to use if not specified

        Returns:
            Full image reference (host/org/name:tag or host/org/name@digest)
        """
        if self.digest:
            return f"{registry.host}/{registry.org}/{self.name}@{self.digest}"
        tag = self.tag or default_tag or "latest"
        return f"{registry.host}/{registry.org}/{self.name}:{tag}"


class Profile(BaseModel):
    """Profile configuration (prod, dev, local)."""

    registry: Registry
    default_tag: str = Field("latest", description="Default tag for this profile")


class ImageConfig(BaseModel):
    """Complete image configuration."""

    version: int = Field(1, description="Configuration schema version")
    default_profile: str = Field(..., description="Default profile name")
    profiles: Dict[str, Profile] = Field(..., description="Available profiles")
    images: Dict[str, Image] = Field(..., description="Image definitions")

    @classmethod
    @lru_cache(maxsize=1)
    def from_yaml(
        cls,
        path: Optional[Path] = None,
        profile: Optional[str] = None,
    ) -> ImageConfig:
        """Load configuration from YAML file.

        Args:
            path: Path to YAML file (defaults to modelops-images.yaml)
            profile: Profile name to use (overrides default/env)

        Returns:
            Loaded and validated configuration

        Raises:
            FileNotFoundError: If configuration file not found
            ValueError: If profile not found or configuration invalid
        """
        if path is None:
            # Look for config file in project root
            path = Path("modelops-images.yaml")
            if not path.exists():
                # Try parent directory (for when running from subdirs)
                path = Path(__file__).parent.parent.parent / "modelops-images.yaml"

        if not path.exists():
            raise FileNotFoundError(f"Image configuration not found: {path}")

        with open(path, "r") as f:
            data = yaml.safe_load(f)

        # Parse the base configuration
        config = cls.model_validate(data)

        # Resolve the profile to use
        selected_profile = (
            profile
            or os.getenv("MOPS_IMAGE_PROFILE")
            or config.default_profile
        )

        if selected_profile not in config.profiles:
            available = ", ".join(sorted(config.profiles.keys()))
            raise ValueError(
                f"Unknown profile '{selected_profile}'. Available: {available}"
            )

        return config

    def get_profile(self, name: Optional[str] = None) -> Profile:
        """Get a specific profile or the default.

        Args:
            name: Profile name (uses default if not specified)

        Returns:
            Profile configuration
        """
        profile_name = (
            name
            or os.getenv("MOPS_IMAGE_PROFILE")
            or self.default_profile
        )
        return self.profiles[profile_name]

    def ref(self, image_key: str, profile: Optional[str] = None) -> str:
        """Get full image reference for a given key.

        Args:
            image_key: Image key (e.g., "scheduler", "worker")
            profile: Profile to use (defaults to current)

        Returns:
            Full image reference

        Raises:
            KeyError: If image key not found
        """
        if image_key not in self.images:
            available = ", ".join(sorted(self.images.keys()))
            raise KeyError(
                f"Unknown image key '{image_key}'. Available: {available}"
            )

        prof = self.get_profile(profile)
        return self.images[image_key].ref(prof.registry, prof.default_tag)

    # Convenience methods for common images
    def scheduler_image(self, profile: Optional[str] = None) -> str:
        """Get scheduler image reference."""
        return self.ref("scheduler", profile)

    def worker_image(self, profile: Optional[str] = None) -> str:
        """Get worker image reference."""
        return self.ref("worker", profile)

    def runner_image(self, profile: Optional[str] = None) -> str:
        """Get runner image reference."""
        return self.ref("runner", profile)

    def adaptive_worker_image(self, profile: Optional[str] = None) -> str:
        """Get adaptive worker image reference."""
        return self.ref("adaptive-worker", profile)


# Global singleton - loaded on first access
_IMAGE_CONFIG: Optional[ImageConfig] = None


def get_image_config() -> ImageConfig:
    """Get the global image configuration.

    Returns:
        Singleton ImageConfig instance
    """
    global _IMAGE_CONFIG
    if _IMAGE_CONFIG is None:
        _IMAGE_CONFIG = ImageConfig.from_yaml()
    return _IMAGE_CONFIG


# Export convenience
IMAGE_CONFIG = get_image_config