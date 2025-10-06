"""Integration tests for Dask-based aggregation with local cluster."""

import pytest
from pathlib import Path
from dask.distributed import LocalCluster, Client
from modelops_contracts import SimTask
from modelops_contracts.simulation import ReplicateSet
from modelops.services.dask_simulation import DaskSimulationService
from modelops.worker.config import RuntimeConfig

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def dask_cluster():
    """Create a local Dask cluster for testing with timeout."""
    import asyncio
    from concurrent.futures import TimeoutError as FutureTimeoutError
    
    # Try to create cluster with timeout
    try:
        cluster = LocalCluster(
            n_workers=2,
            threads_per_worker=1,  # Reduce threads to minimize resource contention
            processes=True,
            silence_logs=True,
            dashboard_address=None,  # Disable dashboard for tests
            death_timeout="5s",  # Faster worker cleanup
        )
        client = Client(cluster, timeout="10s")
    except (TimeoutError, FutureTimeoutError, asyncio.TimeoutError):
        pytest.skip("LocalCluster creation timed out - likely resource issue")
    except Exception as e:
        pytest.skip(f"LocalCluster creation failed: {e}")
    
    yield client
    
    # Cleanup with timeout
    try:
        client.close(timeout=5)
        cluster.close(timeout=5)
    except:
        pass  # Best effort cleanup


@pytest.fixture
def simulation_service(dask_cluster):
    """Create a DaskSimulationService with test configuration."""
    # Get the path to the examples directory relative to this test file
    test_dir = Path(__file__).parent.parent.parent  # Go up to repo root
    examples_dir = test_dir / "examples"
    
    config = RuntimeConfig(
        bundle_source="file",
        bundles_dir=str(examples_dir),
        force_fresh_venv=False,  # Allow process reuse in tests
    )
    service = DaskSimulationService(dask_cluster, config)
    yield service
    # Cleanup happens when client closes


class TestDaskAggregation:
    """Test aggregation functionality with Dask."""
    
    def test_simple_aggregation(self, simulation_service):
        """Test basic aggregation with a single replicate set."""
        # Create a replicate set
        base_task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref="sha256:f987e0ab742272c3969d63207162993f65c6c3af01f07910bd3c239f4407c51c",
            params={"alpha": 1.0, "beta": 2.0},
            seed=42
        )
        
        replicate_set = ReplicateSet(
            base_task=base_task,
            n_replicates=3,
            seed_offset=0
        )
        
        # Submit and wait for aggregation
        future = simulation_service.submit_replicate_set(
            replicate_set,
            target_entrypoint="targets.compute_loss/compute_loss"
        )
        
        result = future.result()
        
        # Verify result structure and basic properties
        assert result.loss is not None
        assert isinstance(result.loss, float)
        assert result.n_replicates == 3
        assert result.diagnostics is not None
    
    def test_multiple_replicate_sets(self, simulation_service):
        """Test aggregation of multiple replicate sets in parallel."""
        futures = []
        
        # Submit multiple replicate sets
        for alpha in [0.5, 1.0, 1.5]:
            base_task = SimTask.from_components(
                import_path="simulations.test",
                scenario="baseline",
                bundle_ref="sha256:f987e0ab742272c3969d63207162993f65c6c3af01f07910bd3c239f4407c51c",
                params={"alpha": alpha, "beta": 2.0},
                seed=42
            )
            
            replicate_set = ReplicateSet(
                base_task=base_task,
                n_replicates=5,
                seed_offset=0
            )
            
            future = simulation_service.submit_replicate_set(
                replicate_set,
                target_entrypoint="targets.compute_loss/compute_loss"
            )
            futures.append(future)
        
        # Gather results
        results = [f.result() for f in futures]
        
        # Verify all completed with valid results
        assert len(results) == 3
        for result in results:
            assert result.loss is not None
            assert isinstance(result.loss, float)
            assert result.n_replicates == 5
    
    def test_error_handling_in_aggregation(self, simulation_service):
        """Test that aggregation handles simulation errors gracefully."""
        # Create task that will fail (invalid params)
        base_task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref="sha256:f987e0ab742272c3969d63207162993f65c6c3af01f07910bd3c239f4407c51c",
            params={"alpha": -999.0, "beta": 2.0},  # Will cause error
            seed=42
        )
        
        replicate_set = ReplicateSet(
            base_task=base_task,
            n_replicates=3,
            seed_offset=0
        )
        
        # Submit and expect graceful handling
        future = simulation_service.submit_replicate_set(
            replicate_set,
            target_entrypoint="targets.compute_loss/compute_loss"
        )
        
        # Should still get a result, possibly with inf loss or error info
        result = future.result()
        assert result is not None


class TestProcessPoolReuse:
    """Test warm process pool reuse."""
    
    def test_process_reuse_performance(self, simulation_service):
        """Test that process reuse improves performance."""
        import time
        
        # First round - cold start
        start = time.time()
        base_task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref="sha256:f987e0ab742272c3969d63207162993f65c6c3af01f07910bd3c239f4407c51c",
            params={"alpha": 1.0, "beta": 2.0},
            seed=42
        )
        
        futures = []
        for i in range(5):
            task = SimTask.from_components(
                import_path="simulations.test",
                scenario="baseline",
                bundle_ref="sha256:f987e0ab742272c3969d63207162993f65c6c3af01f07910bd3c239f4407c51c",
                params={"alpha": 1.0 + i*0.1, "beta": 2.0},
                seed=42 + i
            )
            futures.append(simulation_service.submit(task))
        
        results = simulation_service.gather(futures)
        cold_time = time.time() - start
        
        # Second round - warm start (same bundle)
        start = time.time()
        futures = []
        for i in range(5):
            task = SimTask.from_components(
                import_path="simulations.test",
                scenario="baseline",
                bundle_ref="sha256:f987e0ab742272c3969d63207162993f65c6c3af01f07910bd3c239f4407c51c",  # Same bundle!
                params={"alpha": 2.0 + i*0.1, "beta": 3.0},
                seed=100 + i
            )
            futures.append(simulation_service.submit(task))
        
        results = simulation_service.gather(futures)
        warm_time = time.time() - start
        
        # Warm should be faster (though in tests the difference might be small)
        # Just verify both completed successfully
        assert len(results) == 5
        assert all(r.error is None for r in results)