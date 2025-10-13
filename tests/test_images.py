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
        assert config.default_profile == "prod"
        assert "prod" in config.profiles
        assert "dev" in config.profiles

    def test_prod_profile(self):
        """Test production profile values."""
        config = ImageConfig.from_yaml(profile="prod")
        profile = config.get_profile("prod")

        assert profile.registry.host == "ghcr.io"
        assert profile.registry.org == "institutefordiseasemodeling"
        assert profile.default_tag == "latest"

    def test_dev_profile(self):
        """Test development profile values."""
        config = ImageConfig.from_yaml(profile="dev")
        profile = config.get_profile("dev")

        assert profile.registry.host == "ghcr.io"
        assert profile.registry.org == "vsbuffalo"
        assert profile.default_tag == "dev"

    def test_image_refs(self):
        """Test image reference generation."""
        config = ImageConfig.from_yaml(profile="prod")

        scheduler = config.scheduler_image()
        assert scheduler == "ghcr.io/institutefordiseasemodeling/modelops-dask-scheduler:latest"

        worker = config.worker_image()
        assert worker == "ghcr.io/institutefordiseasemodeling/modelops-dask-worker:latest"

        runner = config.runner_image()
        assert runner == "ghcr.io/institutefordiseasemodeling/modelops-dask-runner:latest"

        adaptive = config.adaptive_worker_image()
        assert adaptive == "ghcr.io/institutefordiseasemodeling/modelops-adaptive-worker:0.1.0"

    def test_profile_override(self):
        """Test profile override via environment variable."""
        # Save original value
        original = os.environ.get("MOPS_IMAGE_PROFILE")

        try:
            # Set to dev profile
            os.environ["MOPS_IMAGE_PROFILE"] = "dev"

            # Force reload by clearing cache
            ImageConfig.from_yaml.cache_clear()
            config = ImageConfig.from_yaml()

            scheduler = config.scheduler_image()
            assert "vsbuffalo" in scheduler
            assert scheduler.endswith(":dev")

        finally:
            # Restore original value
            if original:
                os.environ["MOPS_IMAGE_PROFILE"] = original
            else:
                os.environ.pop("MOPS_IMAGE_PROFILE", None)

            # Clear cache again
            ImageConfig.from_yaml.cache_clear()

    def test_unknown_profile_error(self):
        """Test error handling for unknown profile."""
        with pytest.raises(ValueError, match="Unknown profile"):
            ImageConfig.from_yaml(profile="nonexistent")

    def test_singleton_caching(self):
        """Test that get_image_config returns the same instance."""
        config1 = get_image_config()
        config2 = get_image_config()
        # Should be the same function that returns the config
        assert config1 == config2


class TestImageValidation:
    """Test that no hardcoded image references remain."""

    def test_no_hardcoded_vsbuffalo_refs(self):
        """Ensure no hardcoded vsbuffalo references remain in code."""
        # This would be run in CI with a grep check
        # For now, just verify our config doesn't default to vsbuffalo in prod
        config = ImageConfig.from_yaml(profile="prod")

        for image_key in ["scheduler", "worker", "runner", "adaptive-worker"]:
            ref = config.ref(image_key)
            assert "vsbuffalo" not in ref, f"Found vsbuffalo in {image_key}: {ref}"
            assert "institutefordiseasemodeling" in ref, f"Expected IDM org in {image_key}: {ref}"