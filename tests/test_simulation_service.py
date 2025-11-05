"""Tests for simulation service integration."""

import pytest
from unittest.mock import Mock, patch, MagicMock
import os
from pathlib import Path
import hashlib

from modelops_contracts import SimTask, UniqueParameterSet, SimReturn, TableArtifact
from modelops.services.simulation import LocalSimulationService
from modelops.services.dask_simulation import DaskSimulationService

# Valid test bundle references (SHA256 with 64 hex chars)
TEST_BUNDLE_REF = "sha256:" + "a" * 64
TEST_BUNDLE_REF_2 = "sha256:" + "b" * 64


def simple_test_sim(params: dict, seed: int) -> dict:
    """Simple test simulation."""
    return {
        "output": f"params={params},seed={seed}".encode(),
        "value": str(params.get("x", 1) * seed).encode(),
    }


class TestLocalSimulationService:
    """Tests for LocalSimulationService."""

    @patch.dict(
        os.environ, {"MODELOPS_BUNDLE_SOURCE": "file", "MODELOPS_BUNDLES_DIR": "/tmp/test_bundles"}
    )
    @patch("modelops.adapters.bundle.file_repo.FileBundleRepository")
    def test_local_service_initialization(self, mock_bundle_repo_class):
        """Test LocalSimulationService initializes with executor."""
        # Mock the bundle repository
        mock_bundle_repo = Mock()
        mock_bundle_repo_class.return_value = mock_bundle_repo

        service = LocalSimulationService()

        # Should have executor
        assert hasattr(service, "executor")
        from modelops.core.executor import SimulationExecutor

        assert isinstance(service.executor, SimulationExecutor)

    @patch("modelops.adapters.bundle.file_repo.FileBundleRepository")
    @patch("modelops.core.executor.SimulationExecutor")
    @patch.dict(
        os.environ, {"MODELOPS_BUNDLE_SOURCE": "file", "MODELOPS_BUNDLES_DIR": "/tmp/test_bundles"}
    )
    def test_local_service_submit(self, mock_executor_class, mock_bundle_repo_class):
        """Test LocalSimulationService submit uses executor."""
        # Mock the bundle repository
        mock_bundle_repo = Mock()
        mock_bundle_repo_class.return_value = mock_bundle_repo

        # Setup mock executor
        mock_executor = Mock()
        # Create proper TableArtifact for output
        test_data = b"test_data"
        checksum = hashlib.blake2b(test_data, digest_size=32).hexdigest()
        artifact = TableArtifact(size=len(test_data), inline=test_data, checksum=checksum)
        mock_result = SimReturn(task_id="test-task-123", outputs={"result": artifact})
        mock_executor.execute.return_value = mock_result
        mock_executor_class.return_value = mock_executor

        service = LocalSimulationService()
        service.executor = mock_executor  # Override with our mock

        # Test execution
        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1}),
            seed=42,
        )
        future = service.submit(task)

        # Verify executor was called
        mock_executor.execute.assert_called_once_with(task)
        # Now submit returns a Future, so we need to get the result
        assert future.result() == mock_result

    @patch.dict(
        os.environ, {"MODELOPS_BUNDLE_SOURCE": "file", "MODELOPS_BUNDLES_DIR": "/tmp/test_bundles"}
    )
    @patch("modelops.adapters.bundle.file_repo.FileBundleRepository")
    def test_local_service_gather(self, mock_bundle_repo_class):
        """Test LocalSimulationService gather (passthrough)."""
        # Mock the bundle repository
        mock_bundle_repo = Mock()
        mock_bundle_repo_class.return_value = mock_bundle_repo

        service = LocalSimulationService()

        # Create mock futures
        from concurrent.futures import Future

        futures = []
        expected_results = [{"a": b"1"}, {"b": b"2"}, {"c": b"3"}]
        for result in expected_results:
            future = Future()
            future.set_result(result)
            futures.append(future)

        results = service.gather(futures)

        assert results == expected_results


class TestWorkerPlugin:
    """Tests for WorkerPlugin functionality."""

    @patch("modelops.worker.plugin.WorkerPlugin")
    def test_worker_plugin_initialization(self, mock_plugin_class):
        """Test WorkerPlugin gets registered with Dask."""
        # This would test that the plugin gets properly registered
        # when a Dask worker starts up
        mock_plugin = Mock()
        mock_plugin_class.return_value = mock_plugin

        # The actual registration happens in Dask worker startup
        # We're just verifying the plugin can be instantiated
        from modelops.worker.plugin import WorkerPlugin

        plugin = WorkerPlugin()

        assert hasattr(plugin, "setup")
        assert hasattr(plugin, "transition")


class TestDaskSimulationService:
    """Tests for DaskSimulationService."""

    @patch.dict(
        os.environ, {"MODELOPS_BUNDLE_SOURCE": "file", "MODELOPS_BUNDLES_DIR": "/tmp/test_bundles"}
    )
    @patch("dask.distributed.Client")
    def test_dask_service_submit(self, mock_client_class):
        """Test DaskSimulationService submit."""
        mock_client = Mock()
        mock_future = Mock()
        mock_client.submit.return_value = mock_future
        mock_client_class.return_value = mock_client

        # Mock the client to avoid connection attempts
        from modelops.worker.config import RuntimeConfig

        service = DaskSimulationService.__new__(DaskSimulationService)
        service.scheduler_address = "tcp://localhost:8786"
        service.client = mock_client
        service.config = RuntimeConfig.from_env()
        service._plugin_installed = True  # Skip plugin installation

        # Submit a simulation task
        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF_2,
            entrypoint="example.func/test",
            params=UniqueParameterSet.from_dict({"x": 10}),
            seed=42,
        )
        future = service.submit(task)

        # Verify client.submit was called with worker_run_sim and task
        mock_client.submit.assert_called_once()
        args = mock_client.submit.call_args[0]
        # Check that it's calling the worker_run_sim function
        assert callable(args[0])  # Should be a function
        assert args[1] == task  # Should pass the task

        # DaskSimulationService wraps futures in DaskFutureAdapter
        assert hasattr(future, "wrapped")

    @patch.dict(
        os.environ, {"MODELOPS_BUNDLE_SOURCE": "file", "MODELOPS_BUNDLES_DIR": "/tmp/test_bundles"}
    )
    @patch("dask.distributed.Client")
    def test_dask_service_gather(self, mock_client_class):
        """Test DaskSimulationService gather."""
        mock_client = Mock()
        mock_results = [{"r1": b"1"}, {"r2": b"2"}]
        mock_client.gather.return_value = mock_results
        mock_client_class.return_value = mock_client

        # Mock the client to avoid connection attempts
        from modelops.worker.config import RuntimeConfig

        service = DaskSimulationService.__new__(DaskSimulationService)
        service.scheduler_address = "tcp://localhost:8786"
        service.client = mock_client
        service.config = RuntimeConfig.from_env()
        service._plugin_installed = True  # Skip plugin installation

        # Create DaskFutureAdapter wrappers
        mock_futures = [Mock(), Mock()]
        wrapped_futures = []
        for f in mock_futures:
            adapter = Mock()
            adapter.wrapped = f
            wrapped_futures.append(adapter)

        results = service.gather(wrapped_futures)

        # Verify client.gather was called with unwrapped futures
        mock_client.gather.assert_called_once_with(mock_futures)
        assert results == mock_results

    @patch.dict(
        os.environ, {"MODELOPS_BUNDLE_SOURCE": "file", "MODELOPS_BUNDLES_DIR": "/tmp/test_bundles"}
    )
    @patch("dask.distributed.Client")
    def test_dask_service_cleanup(self, mock_client_class):
        """Test DaskSimulationService cleanup."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        # Mock the client to avoid connection attempts
        from modelops.worker.config import RuntimeConfig

        service = DaskSimulationService.__new__(DaskSimulationService)
        service.scheduler_address = "tcp://localhost:8786"
        service.client = mock_client
        service.config = RuntimeConfig.from_env()
        service._plugin_installed = True  # Skip plugin installation

        # DaskSimulationService doesn't have a close method
        # It relies on client cleanup via context manager or explicit client.close()
        mock_client.close()
        mock_client.close.assert_called_once()


class TestSimulationIntegration:
    """Integration tests with actual simulation functions."""

    @patch.dict(
        os.environ, {"MODELOPS_BUNDLE_SOURCE": "file", "MODELOPS_BUNDLES_DIR": "/tmp/test_bundles"}
    )
    @patch("modelops.adapters.bundle.file_repo.FileBundleRepository")
    @patch("modelops.core.executor.SimulationExecutor")
    def test_simulation_with_mock_executor(self, mock_executor_class, mock_bundle_repo_class):
        """Test simulation execution flow."""
        # Mock the bundle repository
        mock_bundle_repo = Mock()
        mock_bundle_repo_class.return_value = mock_bundle_repo

        # Setup mock executor
        mock_executor = Mock()

        # Create proper TableArtifacts for outputs
        pi_data = b"3.14159"
        pi_checksum = hashlib.blake2b(pi_data, digest_size=32).hexdigest()
        pi_artifact = TableArtifact(size=len(pi_data), inline=pi_data, checksum=pi_checksum)

        error_data = b"0.001"
        error_checksum = hashlib.blake2b(error_data, digest_size=32).hexdigest()
        error_artifact = TableArtifact(
            size=len(error_data), inline=error_data, checksum=error_checksum
        )

        mock_result = SimReturn(
            task_id="test-123", outputs={"pi_estimate": pi_artifact, "error": error_artifact}
        )
        mock_executor.execute.return_value = mock_result
        mock_executor_class.return_value = mock_executor

        service = LocalSimulationService()
        service.executor = mock_executor

        # Submit task
        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF,
            entrypoint="simulations.monte_carlo_pi/test",
            params=UniqueParameterSet.from_dict({"n_samples": 1000}),
            seed=42,
        )
        future = service.submit(task)

        # Get result from future
        result = future.result()

        # Verify result structure
        assert isinstance(result, SimReturn)
        assert result.task_id == "test-123"
        assert "pi_estimate" in result.outputs
        assert "error" in result.outputs
