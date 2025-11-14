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
        # Create config with Azure uploads enabled
        config = RuntimeConfig(
            bundle_source="file",
            bundles_dir=tmpdir,
            bundles_cache_dir=tmpdir,
            executor_type="direct",
            upload_to_azure=True,
            azure_connection_string="DefaultEndpointsProtocol=https;AccountName=test;AccountKey=test;EndpointSuffix=core.windows.net",
            azure_container="testresults",
        )

        # Create plugin
        plugin = ModelOpsWorkerPlugin(config=config)

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


def test_plugin_validates_azure_config():
    """Test that plugin validates Azure configuration."""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create config with Azure uploads but missing credentials
        config = RuntimeConfig(
            bundle_source="file",
            bundles_dir=tmpdir,
            bundles_cache_dir=tmpdir,
            executor_type="direct",
            upload_to_azure=True,
            # Missing both azure_connection_string and azure_storage_account
        )

        # Create plugin
        plugin = ModelOpsWorkerPlugin(config=config)

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


if __name__ == "__main__":
    test_plugin_creates_executor_with_azure_uploads()
    test_plugin_validates_azure_config()
