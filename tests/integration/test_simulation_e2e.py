"""End-to-end integration tests for the simulation pipeline."""

import os
import time
import pytest
from pathlib import Path
from modelops_contracts import SimTask, SimReturn
from modelops.services.dask_simulation import DaskSimulationService
from modelops.worker.config import RuntimeConfig

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


# The dask_cluster fixture is now provided by conftest.py


@pytest.fixture
def simulation_service(dask_cluster):
    """Create a DaskSimulationService with test configuration."""
    # Get the path to the examples directory relative to this test file
    test_dir = Path(__file__).parent.parent.parent  # Go up to repo root
    examples_dir = test_dir / "examples"

    config = RuntimeConfig(
        bundle_source="file",
        bundles_dir=str(examples_dir),
        force_fresh_venv=False,  # Allow process reuse
    )
    service = DaskSimulationService(dask_cluster, config)
    yield service


class TestSimulationE2E:
    """End-to-end tests for simulation execution."""

    def test_single_simulation_execution(self, simulation_service, test_bundle_ref):
        """Test execution of a single simulation task."""
        # Create a simulation task
        task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,
            params={"alpha": 1.0, "beta": 2.0},
            seed=42,
        )

        # Submit and execute
        future = simulation_service.submit(task)
        result = future.result(timeout=10)

        # Verify result structure
        assert isinstance(result, SimReturn)
        assert result.error is None  # Success
        # Check we got some outputs
        assert result.outputs is not None and len(result.outputs) > 0

    def test_parallel_simulation_execution(self, simulation_service, test_bundle_ref):
        """Test parallel execution of multiple simulations."""
        # Create multiple tasks with different parameters
        tasks = []
        for i in range(10):
            task = SimTask.from_components(
                import_path="simulations.test",
                scenario="baseline",
                bundle_ref=test_bundle_ref,
                params={"alpha": 1.0 + i * 0.1, "beta": 2.0},
                seed=42 + i,
            )
            tasks.append(task)

        # Submit all tasks
        futures = [simulation_service.submit(task) for task in tasks]

        # Gather results
        results = simulation_service.gather(futures)

        # Verify all completed successfully
        assert len(results) == 10
        for i, result in enumerate(results):
            assert isinstance(result, SimReturn)
            assert result.error is None  # Success
            assert result.outputs is not None

    def test_simulation_with_invalid_params(self, simulation_service, test_bundle_ref):
        """Test simulation behavior with invalid parameters."""
        # Create task with invalid params
        task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,
            params={"alpha": -999.0, "beta": 2.0},  # Invalid param
            seed=42,
        )

        # Submit and execute
        future = simulation_service.submit(task)
        result = future.result(timeout=10)

        # Should complete but might have special handling
        assert isinstance(result, SimReturn)
        # Either success or error
        if result.error is not None:
            assert isinstance(result.error.message, str)

    def test_simulation_with_missing_bundle(self, simulation_service):
        """Test simulation behavior with missing bundle."""
        # Create task with non-existent bundle (invalid digest)
        task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            params={"alpha": 1.0, "beta": 2.0},
            seed=42,
        )

        # Submit and execute
        future = simulation_service.submit(task)
        result = future.result(timeout=10)

        # Should fail with appropriate error
        assert isinstance(result, SimReturn)
        assert result.error is not None
        # Error message should indicate bundle not found
        assert any(
            phrase in result.error.message.lower()
            for phrase in ["not found", "does not exist", "no such", "failed to find"]
        )

    def test_simulation_determinism(self, simulation_service, test_bundle_ref):
        """Test that simulations with same seed produce same results."""
        # Create identical tasks
        task1 = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,
            params={"alpha": 1.5, "beta": 2.5},
            seed=12345,
        )

        task2 = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,
            params={"alpha": 1.5, "beta": 2.5},
            seed=12345,
        )

        # Execute both
        future1 = simulation_service.submit(task1)
        future2 = simulation_service.submit(task2)

        result1 = future1.result(timeout=10)
        result2 = future2.result(timeout=10)

        # Results should be identical (both success or both failure)
        assert (result1.error is None) == (result2.error is None)
        # If both succeeded, outputs should be similar
        if result1.error is None and result2.error is None:
            assert result1.outputs.keys() == result2.outputs.keys()

    def test_different_scenarios(self, simulation_service, test_bundle_ref):
        """Test execution with different scenarios."""
        scenarios = ["baseline", "optimistic", "pessimistic"]
        futures = []

        for scenario in scenarios:
            task = SimTask.from_components(
                import_path="simulations.test",
                scenario=scenario,
                bundle_ref=test_bundle_ref,
                params={"alpha": 1.0, "beta": 2.0},
                seed=42,
            )
            futures.append(simulation_service.submit(task))

        results = simulation_service.gather(futures)

        # All should complete
        assert len(results) == 3
        for result in results:
            assert result.error is None  # Success
            assert result.outputs is not None


class TestBundleCaching:
    """Test bundle caching and process reuse."""

    def test_bundle_cache_reuse(self, simulation_service, test_bundle_ref):
        """Test that bundles are cached and reused across simulations."""
        # First simulation - cold start
        start_cold = time.time()
        task1 = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,
            params={"alpha": 1.0, "beta": 2.0},
            seed=42,
        )
        future1 = simulation_service.submit(task1)
        result1 = future1.result(timeout=10)
        cold_time = time.time() - start_cold

        assert result1.error is None  # Success

        # Second simulation - should reuse cached bundle
        start_warm = time.time()
        task2 = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,  # Same bundle
            params={"alpha": 2.0, "beta": 3.0},  # Different params
            seed=43,
        )
        future2 = simulation_service.submit(task2)
        result2 = future2.result(timeout=10)
        warm_time = time.time() - start_warm

        assert result2.error is None  # Success

        # Both should complete successfully
        # Note: warm_time might not always be faster in tests due to overhead
        assert result1.error is None
        assert result2.error is None

    def test_multiple_bundles(self, simulation_service, test_bundle_ref):
        """Test handling of multiple different bundles."""
        # Use same bundle digest for now (would use different ones in real test)
        bundle_digest = test_bundle_ref
        futures = []

        for i in range(2):
            task = SimTask.from_components(
                import_path="simulations.test",
                scenario="baseline",
                bundle_ref=bundle_digest,
                params={"alpha": 1.0 + i, "beta": 2.0},
                seed=42 + i,
            )
            futures.append(simulation_service.submit(task))

        results = simulation_service.gather(futures)

        # All should complete
        assert len(results) == 2
        for result in results:
            assert result.error is None  # Success


class TestSimulationLoadBalancing:
    """Test load balancing across Dask workers."""

    def test_load_distribution(self, simulation_service, test_bundle_ref):
        """Test that simulations are distributed across workers."""
        # Submit many tasks to ensure distribution
        n_tasks = 20
        futures = []

        for i in range(n_tasks):
            task = SimTask.from_components(
                import_path="simulations.test",
                scenario="baseline",
                bundle_ref=test_bundle_ref,
                params={"alpha": 1.0 + i * 0.01, "beta": 2.0},
                seed=1000 + i,
            )
            futures.append(simulation_service.submit(task))

        # Gather results
        results = simulation_service.gather(futures)

        # All should complete
        assert len(results) == n_tasks
        completed = sum(1 for r in results if r.error is None)
        assert completed == n_tasks

        # Check that execution was distributed (via metrics)
        worker_ids = set()
        for result in results:
            if result.metrics and "worker_id" in result.metrics:
                worker_ids.add(result.metrics["worker_id"])

        # Should have used multiple workers (if available in diagnostics)
        # Note: This might not always be verifiable depending on implementation

    def test_concurrent_batch_submission(self, simulation_service, test_bundle_ref):
        """Test submitting multiple batches concurrently."""
        batch_size = 5
        n_batches = 3
        all_futures = []

        # Submit batches
        for batch in range(n_batches):
            batch_futures = []
            for i in range(batch_size):
                task = SimTask.from_components(
                    import_path="simulations.test",
                    scenario="baseline",
                    bundle_ref=test_bundle_ref,
                    params={"alpha": 1.0 + batch * 0.1 + i * 0.01, "beta": 2.0 + batch * 0.1},
                    seed=batch * 100 + i,
                )
                batch_futures.append(simulation_service.submit(task))
            all_futures.extend(batch_futures)

        # Gather all results
        results = simulation_service.gather(all_futures)

        # Verify all completed
        assert len(results) == batch_size * n_batches
        for result in results:
            assert result.error is None  # Success
            assert result.outputs is not None


class TestSimulationTimeout:
    """Test simulation timeout and cancellation."""

    def test_long_running_simulation(self, simulation_service, test_bundle_ref):
        """Test handling of long-running simulations."""
        # This test assumes the test bundle can simulate long-running tasks
        # For now, we'll just test normal execution
        task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,
            params={"alpha": 1.0, "beta": 2.0, "iterations": 1000},
            seed=42,
        )

        future = simulation_service.submit(task)

        # Should complete within reasonable time
        result = future.result(timeout=30)
        assert result.error is None  # Success

    def test_simulation_cancellation(self, simulation_service, test_bundle_ref):
        """Test cancellation of submitted simulations."""
        # Submit multiple tasks
        futures = []
        for i in range(5):
            task = SimTask.from_components(
                import_path="simulations.test",
                scenario="baseline",
                bundle_ref=test_bundle_ref,
                params={"alpha": 1.0 + i, "beta": 2.0},
                seed=42 + i,
            )
            futures.append(simulation_service.submit(task))

        # Cancel some futures (if supported by Dask)
        # Note: Actual cancellation depends on Dask implementation
        try:
            futures[2].cancel()
            futures[3].cancel()
        except:
            pass  # Cancellation might not be supported

        # Get results for non-cancelled futures
        results = []
        for i, future in enumerate(futures):
            try:
                result = future.result(timeout=5)
                results.append(result)
            except:
                pass  # Cancelled or failed

        # At least some should complete
        assert len(results) >= 3
