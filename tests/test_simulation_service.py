"""Tests for simulation service integration with runners."""

import pytest
from unittest.mock import Mock, patch, MagicMock
import os

from modelops_contracts import SimTask, UniqueParameterSet
from modelops.services.simulation import (
    LocalSimulationService,
    DaskSimulationService,
    _worker_run_sim,
)
from modelops.runtime.runners import DirectRunner, BundleRunner


def simple_test_sim(params: dict, seed: int) -> dict:
    """Simple test simulation."""
    return {
        "output": f"params={params},seed={seed}".encode(),
        "value": str(params.get("x", 1) * seed).encode(),
    }


class TestLocalSimulationService:
    """Tests for LocalSimulationService with runners."""
    
    def test_local_service_default_runner(self):
        """Test LocalSimulationService uses DirectRunner by default."""
        service = LocalSimulationService()
        
        # Should have DirectRunner by default
        assert isinstance(service.runner, DirectRunner)
        
        # Test execution with test simulation
        # TODO(MVP): Using local://dev for tests
        task = SimTask(
            bundle_ref="local://dev",  # PLACEHOLDER: Uses all-zeros digest for MVP
            entrypoint="tests.test_simulation_service.simple_test_sim/test",
            params=UniqueParameterSet.from_dict({"x": 5}),
            seed=10
        )
        result = service.submit(task)
        
        assert isinstance(result, dict)
        assert b"params={'x': 5},seed=10" in result["output"]
        assert result["value"] == b"50"
    
    def test_local_service_custom_runner(self):
        """Test LocalSimulationService with custom runner."""
        mock_runner = Mock()
        mock_runner.run.return_value = {"test": b"custom"}
        
        service = LocalSimulationService(runner=mock_runner)
        
        # Should use provided runner
        assert service.runner == mock_runner
        
        # Test execution
        # TODO(MVP): Using local://dev for tests
        task = SimTask(
            bundle_ref="local://dev",  # PLACEHOLDER: Uses all-zeros digest for MVP
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1}),
            seed=42
        )
        result = service.submit(task)
        
        # Verify runner was called with extracted values
        mock_runner.run.assert_called_once_with(
            "module.func",  # Now uses dot notation directly
            {"x": 1},
            42,
            "local://dev"
        )
        assert result == {"test": b"custom"}
    
    def test_local_service_gather(self):
        """Test LocalSimulationService gather (passthrough)."""
        service = LocalSimulationService()
        
        # For local service, gather just returns the input
        futures = [{"a": b"1"}, {"b": b"2"}, {"c": b"3"}]
        results = service.gather(futures)
        
        assert results == futures


class TestWorkerRunSim:
    """Tests for _worker_run_sim function."""
    
    @patch('modelops.services.simulation.get_runner')
    def test_worker_run_sim_uses_runner(self, mock_get_runner):
        """Test _worker_run_sim uses runner from environment."""
        mock_runner = Mock()
        mock_runner.run.return_value = {"result": b"test"}
        mock_get_runner.return_value = mock_runner
        
        # Call worker function with SimTask
        task = SimTask(
            bundle_ref="sha256:abc123456789",  # Need full digest
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"param": "value"}),
            seed=123
        )
        result = _worker_run_sim(task)
        
        # Verify runner was obtained and used with extracted values
        mock_get_runner.assert_called_once()
        mock_runner.run.assert_called_once_with(
            "module.func",  # Now uses dot notation directly
            {"param": "value"},
            123,
            "sha256:abc123456789"
        )
        assert result == {"result": b"test"}
    
    @patch.dict(os.environ, {"MODELOPS_RUNNER_TYPE": "bundle"})
    @patch('modelops.services.simulation.get_runner')
    def test_worker_respects_env_var(self, mock_get_runner):
        """Test worker respects MODELOPS_RUNNER_TYPE env var."""
        mock_runner = Mock()
        mock_runner.run.return_value = {"data": b"bundle"}
        mock_get_runner.return_value = mock_runner
        
        task = SimTask(
            bundle_ref="sha256:ref123456789",  # Need proper scheme
            entrypoint="m.f/test",
            params=UniqueParameterSet.from_dict({}),
            seed=0
        )
        result = _worker_run_sim(task)
        
        # get_runner should be called without arguments
        # (so it reads from environment)
        mock_get_runner.assert_called_once_with()
        assert result == {"data": b"bundle"}


class TestDaskSimulationService:
    """Tests for DaskSimulationService integration."""
    
    @patch('dask.distributed.Client')
    def test_dask_service_submit(self, mock_client_class):
        """Test DaskSimulationService submit uses _worker_run_sim."""
        mock_client = Mock()
        mock_future = Mock()
        mock_client.submit.return_value = mock_future
        mock_client_class.return_value = mock_client
        
        service = DaskSimulationService("tcp://localhost:8786", silence_warnings=True)
        
        # Submit a simulation task
        task = SimTask(
            bundle_ref="sha256:bundle123456",  # Need proper scheme
            entrypoint="example.func/test",
            params=UniqueParameterSet.from_dict({"x": 10}),
            seed=42
        )
        future = service.submit(task)
        
        # Verify client.submit was called with _worker_run_sim and task
        mock_client.submit.assert_called_once()
        args = mock_client.submit.call_args[0]
        assert args[0] == _worker_run_sim  # Function
        assert args[1] == task              # SimTask
        
        assert future == mock_future
    
    @patch('dask.distributed.Client')
    def test_dask_service_gather(self, mock_client_class):
        """Test DaskSimulationService gather."""
        mock_client = Mock()
        mock_results = [{"r1": b"1"}, {"r2": b"2"}]
        mock_client.gather.return_value = mock_results
        mock_client_class.return_value = mock_client
        
        service = DaskSimulationService("tcp://localhost:8786", silence_warnings=True)
        
        # Gather futures
        mock_futures = [Mock(), Mock()]
        results = service.gather(mock_futures)
        
        # Verify client.gather was called
        mock_client.gather.assert_called_once_with(mock_futures)
        assert results == mock_results
    
    @patch('dask.distributed.Client')
    def test_dask_service_close(self, mock_client_class):
        """Test DaskSimulationService close."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        
        service = DaskSimulationService("tcp://localhost:8786")
        service.close()
        
        # Verify client.close was called
        mock_client.close.assert_called_once()


class TestSimulationWithRunners:
    """Integration tests with actual simulation functions."""
    
    def test_examples_simulation_with_direct_runner(self):
        """Test running actual example simulations."""
        service = LocalSimulationService()
        
        # Try monte_carlo_pi if available
        try:
            # TODO(MVP): Using local://dev for tests
            task = SimTask(
                bundle_ref="local://dev",  # PLACEHOLDER: Uses all-zeros digest for MVP
                entrypoint="examples.simulations.monte_carlo_pi/test",
                params=UniqueParameterSet.from_dict({"n_samples": 1000}),
                seed=42
            )
            result = service.submit(task)
            
            # Should return IPC bytes
            assert isinstance(result, dict)
            assert all(isinstance(v, bytes) for v in result.values())
            
            # Should have estimate and error tables
            assert "estimate" in result or "pi_estimate" in result
            
        except ImportError:
            # Examples module might not be in path during testing
            pytest.skip("examples.simulations not available")
    
    @patch('modelops.runtime.runners.ensure_bundle')
    @patch('modelops.runtime.runners.ensure_venv')
    @patch('modelops.runtime.runners.run_in_env')
    def test_bundle_runner_integration(self, mock_run, mock_venv, mock_bundle):
        """Test BundleRunner integration with service."""
        from pathlib import Path
        
        # Setup mocks
        mock_bundle.return_value = Path("/bundles/test")
        mock_venv.return_value = Path("/venvs/test")
        mock_run.return_value = {"output": b"bundled"}
        
        # Create service with BundleRunner
        runner = BundleRunner()
        service = LocalSimulationService(runner=runner)
        
        # Submit with bundle_ref
        task = SimTask(
            bundle_ref="sha256:test12345678",  # Match digest
            entrypoint="test.func/test",
            params=UniqueParameterSet.from_dict({"x": 1}),
            seed=0
        )
        result = service.submit(task)
        
        # Verify bundle operations were called
        assert mock_bundle.called
        assert mock_venv.called
        assert mock_run.called
        assert result == {"output": b"bundled"}