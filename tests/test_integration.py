"""End-to-end integration tests for the runner system."""

import pytest
from unittest.mock import Mock, patch, MagicMock
import os
from pathlib import Path

from modelops.services.simulation import LocalSimulationService, _worker_run_sim
from modelops.runtime.runners import DirectRunner, get_runner
from modelops.services.ipc import from_ipc_tables, to_ipc_tables


def integration_test_sim(params: dict, seed: int) -> dict:
    """Test simulation for integration testing."""
    import random
    from modelops.services.ipc import to_ipc_tables
    
    random.seed(seed)
    
    # Simulate some computation
    value = params.get("value", 1.0)
    iterations = params.get("iterations", 100)
    
    results = []
    for i in range(iterations):
        results.append(value * random.random())
    
    # Return data that will be converted to IPC format
    data = {
        "results": results,
        "stats": {
            "mean": [sum(results) / len(results)],
            "max": [max(results)],
            "min": [min(results)],
            "seed": [seed],
        }
    }
    
    # Convert to IPC format (bytes)
    return to_ipc_tables(data)


class TestEndToEndIntegration:
    """Full integration tests with all components."""
    
    def test_direct_runner_full_pipeline(self):
        """Test complete pipeline with DirectRunner."""
        # Create service with DirectRunner
        service = LocalSimulationService(runner=DirectRunner())
        
        # Submit simulation
        result = service.submit(
            "tests.test_integration:integration_test_sim",
            {"value": 10.0, "iterations": 50},
            seed=42,
            bundle_ref=""
        )
        
        # Result is already in IPC format from the simulation
        decoded = from_ipc_tables(result)
        
        # Verify results
        assert "results" in decoded
        assert "stats" in decoded
        
        # Check stats
        stats = decoded["stats"]
        assert "mean" in stats
        assert "seed" in stats
        
        # Seed should be preserved
        if hasattr(stats["seed"], 'item'):  # Polars
            seed_val = stats["seed"].item()
        elif hasattr(stats["seed"], 'iloc'):  # Pandas
            seed_val = stats["seed"].iloc[0]
        else:  # List
            seed_val = stats["seed"][0]
        
        assert seed_val == 42
    
    def test_runner_selection_via_environment(self):
        """Test runner selection through environment variable."""
        # Test with direct runner
        with patch.dict(os.environ, {"MODELOPS_RUNNER_TYPE": "direct"}):
            runner = get_runner()
            assert isinstance(runner, DirectRunner)
            
            # Test in worker function
            result = _worker_run_sim(
                "tests.test_integration:integration_test_sim",
                {"value": 5.0},
                seed=123,
                bundle_ref=""
            )
            assert isinstance(result, dict)
    
    @patch('modelops.runtime.runners.ensure_bundle')
    @patch('modelops.runtime.runners.ensure_venv')
    @patch('modelops.runtime.runners.run_in_env')
    def test_bundle_runner_mock_integration(self, mock_run, mock_venv, mock_bundle):
        """Test integration with mocked BundleRunner."""
        # Setup mocks
        mock_bundle.return_value = Path("/test/bundle")
        mock_venv.return_value = Path("/test/venv")
        mock_run.return_value = {
            "output": b"bundled_result",
            "info": b"from_isolated_env"
        }
        
        # Test with bundle runner via environment
        with patch.dict(os.environ, {"MODELOPS_RUNNER_TYPE": "bundle"}):
            result = _worker_run_sim(
                "test:func",
                {"param": "value"},
                seed=999,
                bundle_ref="sha256:test123"
            )
            
            # Verify bundle operations were called
            assert mock_bundle.called
            assert mock_venv.called
            assert mock_run.called
            
            assert result == {
                "output": b"bundled_result",
                "info": b"from_isolated_env"
            }
    
    def test_service_with_different_runners(self):
        """Test LocalSimulationService with different runner types."""
        # Test with DirectRunner
        direct_service = LocalSimulationService(runner=DirectRunner())
        result1 = direct_service.submit(
            "tests.test_integration:integration_test_sim",
            {"value": 1.0},
            seed=1,
            bundle_ref=""
        )
        assert isinstance(result1, dict)
        
        # Test with mocked BundleRunner
        mock_runner = Mock()
        mock_runner.run.return_value = {"mocked": b"result"}
        
        custom_service = LocalSimulationService(runner=mock_runner)
        result2 = custom_service.submit(
            "any:func",
            {},
            seed=0,
            bundle_ref="test"
        )
        
        mock_runner.run.assert_called_once()
        assert result2 == {"mocked": b"result"}
    
    def test_gather_preserves_order(self):
        """Test that gather preserves result order."""
        service = LocalSimulationService()
        
        # Submit multiple simulations
        futures = []
        for i in range(5):
            result = service.submit(
                "tests.test_integration:integration_test_sim",
                {"value": float(i), "iterations": 10},
                seed=i,
                bundle_ref=""
            )
            futures.append(result)
        
        # Gather should preserve order
        results = service.gather(futures)
        
        assert len(results) == 5
        
        # Check seeds are in order
        for i, result in enumerate(results):
            # Result is already in IPC format
            decoded = from_ipc_tables(result)
            stats = decoded["stats"]
            
            if hasattr(stats["seed"], 'item'):  # Polars
                seed_val = stats["seed"].item()
            elif hasattr(stats["seed"], 'iloc'):  # Pandas
                seed_val = stats["seed"].iloc[0]
            else:  # List
                seed_val = stats["seed"][0]
            
            assert seed_val == i
    
    def test_error_handling_integration(self):
        """Test error handling through the full pipeline."""
        service = LocalSimulationService()
        
        # Test with non-existent module
        with pytest.raises(ImportError):
            service.submit(
                "nonexistent.module:func",
                {},
                seed=0,
                bundle_ref=""
            )
        
        # Test with non-existent function
        with pytest.raises(AttributeError):
            service.submit(
                "tests.test_integration:nonexistent_func",
                {},
                seed=0,
                bundle_ref=""
            )


class TestIPCIntegration:
    """Test IPC conversion with different data types."""
    
    def test_ipc_with_simulation_results(self):
        """Test IPC conversion with actual simulation results."""
        service = LocalSimulationService()
        
        # Run simulation
        result = service.submit(
            "tests.test_integration:integration_test_sim",
            {"value": 2.5, "iterations": 20},
            seed=100,
            bundle_ref=""
        )
        
        # Result is already in IPC format
        assert all(isinstance(v, bytes) for v in result.values())
        
        # Convert back
        decoded = from_ipc_tables(result)
        
        # Check structure preserved
        assert "results" in decoded
        assert "stats" in decoded
        
        # Check data integrity
        stats = decoded["stats"]
        assert "mean" in stats
        assert "max" in stats
        assert "min" in stats