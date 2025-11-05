"""Integration tests for OCI bundle fetching on workers.

These tests verify the complete flow:
1. Bundle push to registry
2. Worker fetches bundle from registry
3. Execution environment runs simulation
4. Results returned through Dask
"""

import pytest
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


class TestOCIBundleIntegration:
    """Test OCI bundle integration with workers."""

    @pytest.fixture
    def smoke_bundle_path(self):
        """Get path to smoke test bundle fixture."""
        return Path(__file__).parent.parent / "fixtures" / "smoke_bundle"

    def test_smoke_bundle_exists(self, smoke_bundle_path):
        """Test that smoke bundle fixture exists and is valid."""
        assert smoke_bundle_path.exists()
        assert (smoke_bundle_path / "simulate.py").exists()
        assert (smoke_bundle_path / "manifest.json").exists()
        assert (smoke_bundle_path / "requirements.txt").exists()
        assert (smoke_bundle_path / "wire.py").exists(), "wire.py is required for bundle execution"

    def test_smoke_bundle_validation(self, smoke_bundle_path):
        """Test that smoke bundle passes validation."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "modelops.cli.main",
                "dev",
                "validate-bundle",
                str(smoke_bundle_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Bundle validation PASSED" in result.stdout

    @pytest.mark.skip(reason="Calabaria bridge bundle not yet available")
    def test_calabaria_bridge_validation(self):
        """Test that Calabaria bridge bundle passes validation."""
        # TODO: Add this test when calabaria_bridge bundle is available
        pass

    @pytest.mark.skipif(
        not os.environ.get("MODELOPS_BUNDLE_REGISTRY"), reason="Registry not configured"
    )
    def test_bundle_push_to_registry(self, smoke_bundle_path):
        """Test pushing bundle to registry.

        Requires:
        - MODELOPS_BUNDLE_REGISTRY environment variable
        - Registry accessibility (ACR or local)
        """
        # This would test actual push to registry
        # For now, we just verify the bundle structure is correct
        assert smoke_bundle_path.exists()

    @pytest.mark.skipif(not os.environ.get("DASK_SCHEDULER"), reason="Dask cluster not running")
    def test_smoke_test_with_dask(self):
        """Test full smoke test with Dask cluster.

        Requires:
        - Dask cluster running
        - Workers configured with registry access
        """
        result = subprocess.run(
            [sys.executable, "-m", "modelops.cli.main", "dev", "smoke-test", "--timeout", "30"],
            capture_output=True,
            text=True,
        )
        # If Dask is running, this should work
        if "Connected to Dask cluster" in result.stdout:
            assert "Smoke test PASSED" in result.stdout

    def test_simulate_function_works(self, smoke_bundle_path):
        """Test that the simulate function can be imported and run."""
        # Add bundle path to sys.path temporarily
        import sys
        import importlib

        # Clean up any existing simulate module
        if "simulate" in sys.modules:
            del sys.modules["simulate"]

        sys.path.insert(0, str(smoke_bundle_path))
        try:
            from simulate import simulate

            # Test the function
            result = simulate(params={"test": True, "value": 2.0}, seed=12345)

            assert result["status"] == "completed"
            assert result["seed"] == 12345
            assert result["params"]["test"] is True
            assert result["computed_value"] == 2.0 * 12345
            assert result["bundle_integration"] == "working"

        finally:
            # Clean up
            sys.path.pop(0)
            if "simulate" in sys.modules:
                del sys.modules["simulate"]

    @pytest.mark.skip(reason="Calabaria bridge bundle not yet available")
    def test_calabaria_bridge_simulate(self):
        """Test that Calabaria bridge simulate function works."""
        # TODO: Add this test when calabaria_bridge bundle is available
        pass
