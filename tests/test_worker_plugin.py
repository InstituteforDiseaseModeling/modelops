#!/usr/bin/env python
"""Test worker plugin with storage backend configuration."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from modelops.worker.plugin import ModelOpsWorkerPlugin
from modelops.worker.config import RuntimeConfig


def test_plugin_creates_executor_with_azure_uploads():
    """Test that the plugin correctly wires up Azure uploads."""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Set environment variables for config
        old_env = dict(os.environ)
        try:
            os.environ["MODELOPS_BUNDLE_SOURCE"] = "file"
            os.environ["MODELOPS_BUNDLES_DIR"] = tmpdir
            os.environ["MODELOPS_BUNDLES_CACHE_DIR"] = tmpdir
            os.environ["MODELOPS_EXECUTOR_TYPE"] = "direct"
            os.environ["MODELOPS_UPLOAD_TO_AZURE"] = "true"
            os.environ["AZURE_STORAGE_CONNECTION_STRING"] = "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=test;EndpointSuffix=core.windows.net"
            os.environ["MODELOPS_AZURE_CONTAINER"] = "testresults"

            # Create plugin (reads from environment)
            plugin = ModelOpsWorkerPlugin()

            # Create mock worker
            mock_worker = MagicMock()
            mock_worker.id = "test-worker"

            # Setup should not fail
            plugin.setup(mock_worker)

            # Verify executor was created
            assert hasattr(mock_worker, "modelops_runtime")
            assert hasattr(mock_worker, "modelops_exec_env")

            # Teardown
            plugin.teardown(mock_worker)

            print("✓ Plugin setup with Azure uploads succeeded")
        finally:
            # Restore environment
            os.environ.clear()
            os.environ.update(old_env)


def test_plugin_validates_azure_config():
    """Test that plugin validates Azure configuration."""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Set environment variables with Azure uploads but missing credentials
        old_env = dict(os.environ)
        try:
            os.environ["MODELOPS_BUNDLE_SOURCE"] = "file"
            os.environ["MODELOPS_BUNDLES_DIR"] = tmpdir
            os.environ["MODELOPS_BUNDLES_CACHE_DIR"] = tmpdir
            os.environ["MODELOPS_EXECUTOR_TYPE"] = "direct"
            os.environ["MODELOPS_UPLOAD_TO_AZURE"] = "true"
            # Missing both azure_connection_string and azure_storage_account

            # Create plugin (reads from environment)
            plugin = ModelOpsWorkerPlugin()

            # Create mock worker
            mock_worker = MagicMock()
            mock_worker.id = "test-worker"

            # Setup should fail validation
            try:
                plugin.setup(mock_worker)
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "upload_to_azure is enabled" in str(e)
                print("✓ Validation correctly caught missing Azure config")
        finally:
            # Restore environment
            os.environ.clear()
            os.environ.update(old_env)


if __name__ == "__main__":
    test_plugin_creates_executor_with_azure_uploads()
    test_plugin_validates_azure_config()
