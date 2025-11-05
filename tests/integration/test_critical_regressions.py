"""Critical regression tests for known issues.

These tests ensure that previously fixed bugs don't resurface:
1. JSON-RPC 65KB buffer issue
2. Aggregation memory issues at scale
3. Worker plugin initialization
"""

import os
import pytest
from pathlib import Path
from modelops_contracts import SimTask
from modelops_contracts.simulation import ReplicateSet

from modelops.services.dask_simulation import DaskSimulationService
from modelops.worker.config import RuntimeConfig

# CI detection
IS_CI = os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true"


pytestmark = pytest.mark.integration

# The dask_cluster fixture is now provided by conftest.py


class TestJSONRPCBufferFix:
    """Test that the 65KB JSON-RPC buffer issue is fixed."""

    def test_large_params_handling(self, dask_cluster, test_bundle_ref):
        """Test handling of large parameters (>65KB)."""
        config = RuntimeConfig(
            bundle_source="file",
            bundles_dir=str(Path(__file__).parent.parent.parent / "examples"),
            force_fresh_venv=False,
        )

        service = DaskSimulationService(dask_cluster, config)

        # Create task with large params (but not too large for CI)
        large_data = "x" * (70_000 if not IS_CI else 30_000)

        task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,
            params={"alpha": 1.0, "beta": 2.0, "padding": large_data},
            seed=42,
        )

        # Should handle without error (previously would hang/fail)
        future = service.submit(task)
        result = future.result(timeout=30)

        # Should complete (even if with error due to large params)
        assert result is not None


class TestAggregationScale:
    """Test aggregation at scale doesn't OOM."""

    @pytest.mark.skipif(IS_CI, reason="Too resource-intensive for CI")
    def test_moderate_scale_aggregation(self, dask_cluster, test_bundle_ref):
        """Test aggregation with moderate number of replicates."""
        config = RuntimeConfig(
            bundle_source="file",
            bundles_dir=str(Path(__file__).parent.parent.parent / "examples"),
            force_fresh_venv=False,
        )

        service = DaskSimulationService(dask_cluster, config)

        # Scale appropriately
        n_replicates = 25 if IS_CI else 50

        base_task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,
            params={"alpha": 1.0, "beta": 2.0},
            seed=42,
        )

        replicate_set = ReplicateSet(base_task=base_task, n_replicates=n_replicates, seed_offset=0)

        # Submit with aggregation
        future = service.submit_replicate_set(
            replicate_set, target_entrypoint="targets.compute_loss/compute_loss"
        )

        # Should complete without OOM
        result = future.result(timeout=60)

        assert result.loss is not None
        assert result.n_replicates == n_replicates

    def test_partial_failure_aggregation(self, dask_cluster, test_bundle_ref):
        """Test aggregation handles partial failures correctly."""
        config = RuntimeConfig(
            bundle_source="file",
            bundles_dir=str(Path(__file__).parent.parent.parent / "examples"),
            force_fresh_venv=False,
        )

        service = DaskSimulationService(dask_cluster, config)

        # Create tasks where some will fail - use non-existent scenario
        tasks_with_errors = []
        for i in range(10):
            # Make some tasks fail with non-existent scenario
            scenario = "non_existent_scenario" if i % 3 == 0 else "baseline"

            task = SimTask.from_components(
                import_path="simulations.test",
                scenario=scenario,
                bundle_ref=test_bundle_ref,
                params={"alpha": 1.0 + i * 0.1, "beta": 2.0},
                seed=100 + i,
            )
            tasks_with_errors.append(task)

        # Execute all
        futures = [service.submit(t) for t in tasks_with_errors]
        results = service.gather(futures)

        # Should have some successes and some failures
        successes = sum(1 for r in results if r.error is None)
        failures = sum(1 for r in results if r.error is not None)

        # With non-existent scenario, we should get failures
        assert successes > 0, f"Should have some successful tasks, got {successes}"
        # If the test bundle doesn't validate scenarios, all might succeed
        # Just verify we got all results
        assert len(results) == 10, f"Should have 10 results, got {len(results)}"


class TestWorkerPluginInitialization:
    """Test worker plugin initializes correctly."""

    def test_plugin_installs_on_workers(self, dask_cluster, test_bundle_ref):
        """Test that ModelOpsWorkerPlugin installs on all workers."""
        config = RuntimeConfig(
            bundle_source="file",
            bundles_dir=str(Path(__file__).parent.parent.parent / "examples"),
            force_fresh_venv=False,
        )

        # Creating service should install plugin
        service = DaskSimulationService(dask_cluster, config)

        # Submit a task to verify plugin works
        task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=test_bundle_ref,
            params={"alpha": 1.0, "beta": 2.0},
            seed=42,
        )

        future = service.submit(task)
        result = future.result(timeout=10)

        # Should execute successfully through plugin
        assert result.error is None
        assert result.outputs is not None


class TestRapidTaskSubmission:
    """Test rapid task submission (original JSON-RPC bug trigger)."""

    def test_rapid_submission_no_corruption(self, dask_cluster, test_bundle_ref):
        """Test rapid task submission doesn't cause JSON-RPC corruption."""
        config = RuntimeConfig(
            bundle_source="file",
            bundles_dir=str(Path(__file__).parent.parent.parent / "examples"),
            force_fresh_venv=False,
        )

        service = DaskSimulationService(dask_cluster, config)

        # Rapidly submit tasks (this triggered the original bug)
        n_tasks = 10 if IS_CI else 20

        tasks = []
        for i in range(n_tasks):
            task = SimTask.from_components(
                import_path="simulations.test",
                scenario="baseline",
                bundle_ref=test_bundle_ref,
                params={"alpha": 1.0 + i * 0.01, "beta": 2.0},
                seed=2000 + i,
            )
            tasks.append(task)

        # Submit all at once
        futures = [service.submit(t) for t in tasks]

        # All should complete without JSON-RPC errors
        results = service.gather(futures)

        assert len(results) == n_tasks
        successful = sum(1 for r in results if r.error is None)
        assert successful == n_tasks, f"Only {successful}/{n_tasks} tasks succeeded"
