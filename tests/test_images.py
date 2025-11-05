"""Test centralized image configuration."""

import os
import pytest
from pathlib import Path

from modelops.images import ImageConfig, get_image_config


class TestImageConfig:
    """Test image configuration loading and validation."""

    def test_load_default_config(self):
        """Test loading default configuration."""
        config = get_image_config()
        assert config is not None
        assert config.version == 1
        assert "scheduler" in config.images
        assert "worker" in config.images
        assert "runner" in config.images

    def test_image_refs(self):
        """Test image reference retrieval."""
        config = ImageConfig.from_yaml()

        # Test direct get method
        scheduler = config.get("scheduler")
        assert scheduler.startswith("ghcr.io/")
        assert "modelops-dask-scheduler" in scheduler

        # Test convenience methods
        assert config.scheduler_image() == config.get("scheduler")
        assert config.worker_image() == config.get("worker")
        assert config.runner_image() == config.get("runner")
        assert config.adaptive_worker_image() == config.get("adaptive-worker")

    def test_unknown_image_error(self):
        """Test error handling for unknown image key."""
        config = ImageConfig.from_yaml()
        with pytest.raises(KeyError, match="Unknown image key"):
            config.get("nonexistent")

    def test_singleton_caching(self):
        """Test that get_image_config returns the same instance."""
        config1 = get_image_config()
        config2 = get_image_config()
        # Should be the same instance
        assert config1 is config2

    def test_all_images_have_ghcr(self):
        """Test that all images use ghcr.io registry."""
        config = ImageConfig.from_yaml()
        for key, ref in config.images.items():
            assert ref.startswith("ghcr.io/"), f"Image {key} should use ghcr.io: {ref}"
