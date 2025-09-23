#!/usr/bin/env python
"""Test to demonstrate Dask serialization issues with frozen dataclasses + __post_init__.

This test verifies that our frozen dataclasses with __post_init__ validation
fail to serialize properly through Dask's cloudpickle mechanism.
"""

import pickle
import cloudpickle  # What Dask actually uses
import pytest
from modelops_contracts import SimTask, SimReturn, UniqueParameterSet, TableArtifact
from modelops_contracts.simulation import AggregationTask


def test_direct_pickle_simtask():
    """Test direct pickling of SimTask - expected to fail due to MappingProxyType.

    Standard pickle cannot serialize MappingProxyType objects used in our dataclasses
    for immutability. This is fine because Dask uses cloudpickle, not standard pickle.
    """
    task = SimTask(
        bundle_ref="test://bundle",
        entrypoint="module.func/test",
        params=UniqueParameterSet.from_dict({"x": 1, "y": 2}),
        seed=42
    )

    # Try standard pickle - expect this to fail
    with pytest.raises(TypeError, match="cannot pickle 'mappingproxy' object"):
        pickle.dumps(task)

    # Document that this is expected behavior
    print("✓ Standard pickle fails as expected (MappingProxyType not supported)")


def test_cloudpickle_simtask():
    """Test cloudpickle of SimTask - what Dask actually uses."""
    task = SimTask(
        bundle_ref="test://bundle",
        entrypoint="module.func/test",
        params=UniqueParameterSet.from_dict({"x": 1, "y": 2}),
        seed=42
    )

    # Try cloudpickle
    try:
        pickled = cloudpickle.dumps(task)
        restored = cloudpickle.loads(pickled)
        assert restored.bundle_ref == task.bundle_ref
        assert restored.seed == task.seed
        print(f"✓ Cloudpickle works for SimTask")
    except Exception as e:
        pytest.fail(f"Cloudpickle failed for SimTask: {e}")


def test_nested_aggregation_task_serialization():
    """Test serialization of nested structures (AggregationTask with SimReturns)."""
    # Create SimReturns (which also have __post_init__)
    sim_returns = [
        SimReturn(
            task_id="a" * 64,
            outputs={
                "result": TableArtifact(
                    size=9,  # "test data" is 9 bytes
                    inline=b"test data",
                    checksum="b" * 64
                )
            }
        ),
        SimReturn(
            task_id="c" * 64,
            outputs={
                "result": TableArtifact(
                    size=11,  # "test data 2" is 11 bytes
                    inline=b"test data 2",
                    checksum="d" * 64
                )
            }
        )
    ]

    # Create AggregationTask with nested SimReturns
    agg_task = AggregationTask(
        bundle_ref="test://bundle",
        target_entrypoint="targets.test/compute",
        sim_returns=sim_returns
    )

    # Test with cloudpickle
    try:
        pickled = cloudpickle.dumps(agg_task)
        restored = cloudpickle.loads(pickled)
        assert restored.bundle_ref == agg_task.bundle_ref
        assert len(restored.sim_returns) == 2
        print(f"✓ Nested structure serialization works")
    except Exception as e:
        pytest.fail(f"Nested structure serialization failed: {e}")


def test_unique_parameter_set_serialization():
    """Test serialization of UniqueParameterSet with __post_init__."""
    params = UniqueParameterSet.from_dict({"x": 1, "y": 2.5, "z": "test"})

    try:
        pickled = cloudpickle.dumps(params)
        restored = cloudpickle.loads(pickled)
        assert restored.param_id == params.param_id
        assert restored.params == params.params
        print(f"✓ UniqueParameterSet serialization works")
    except Exception as e:
        pytest.fail(f"UniqueParameterSet serialization failed: {e}")


@pytest.mark.skipif(True, reason="Requires Dask cluster running")
def test_dask_submission():
    """Test actual Dask serialization - this is where it might fail.

    Run this test manually with: pytest tests/test_dask_serialization.py::test_dask_submission -s
    after starting Dask cluster with: make dask-local
    """
    from dask.distributed import Client

    task = SimTask(
        bundle_ref="test://bundle",
        entrypoint="module.func/test",
        params=UniqueParameterSet.from_dict({"x": 1, "y": 2}),
        seed=42
    )

    try:
        # Connect to local cluster
        client = Client("tcp://localhost:8786")

        # Function that will run on worker
        def process_task(t: SimTask):
            return f"Processed {t.bundle_ref} with seed {t.seed}"

        # Submit task - this is where serialization happens
        future = client.submit(process_task, task, pure=False)
        result = future.result()

        assert "Processed test://bundle" in result
        print(f"✓ Dask serialization works: {result}")

        client.close()

    except Exception as e:
        pytest.fail(f"Dask serialization failed: {e}")


@pytest.mark.skipif(True, reason="Requires Dask cluster running")
def test_through_dask_worker_with_validation():
    """Test the actual path through Dask workers with objects that have validation.

    Run this test manually with: pytest tests/test_dask_serialization.py::test_through_dask_worker_with_validation -s
    after starting Dask cluster with: make dask-local
    """
    from dask.distributed import Client

    client = Client("tcp://localhost:8786")

    # Create tasks with different parameters
    tasks = []
    for i in range(3):
        task = SimTask(
            bundle_ref=f"test://bundle{i}",
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": i, "y": i * 2}),
            seed=42 + i
        )
        tasks.append(task)

    # Function that will run on worker after deserialization
    def worker_process(task: SimTask):
        # Access various fields to ensure deserialization worked
        return {
            "bundle": task.bundle_ref,
            "entrypoint": str(task.entrypoint),
            "param_id": task.params.param_id[:8],
            "seed": task.seed,
            "outputs": task.outputs  # Should be None or tuple
        }

    try:
        # Submit all tasks
        futures = [client.submit(worker_process, t, pure=False) for t in tasks]

        # Gather results
        results = client.gather(futures)

        # Verify results
        for i, result in enumerate(results):
            assert result["bundle"] == f"test://bundle{i}"
            assert result["seed"] == 42 + i

        print(f"✓ Worker execution succeeded: {results}")

    except Exception as e:
        pytest.fail(f"Worker execution failed: {e}")

    finally:
        client.close()


def test_simreturn_with_error_serialization():
    """Test SimReturn with error field serialization."""
    from modelops_contracts import ErrorInfo

    # When error is present, error_details is required
    error_details = TableArtifact(
        size=len(b'{"details": "error context"}'),
        inline=b'{"details": "error context"}',
        checksum="f" * 64
    )

    sim_return = SimReturn(
        task_id="e" * 64,
        outputs={},  # Empty outputs allowed when error is present
        error=ErrorInfo(
            error_type="RuntimeError",
            message="Test error",
            retryable=False
        ),
        error_details=error_details  # Required when error is present
    )

    try:
        pickled = cloudpickle.dumps(sim_return)
        restored = cloudpickle.loads(pickled)
        assert restored.error.message == "Test error"
        assert restored.error_details is not None
        print("✓ SimReturn with error serialization works")
    except Exception as e:
        pytest.fail(f"SimReturn with error serialization failed: {e}")


if __name__ == "__main__":
    # Run non-Dask tests
    print("Running serialization tests...\n")

    try:
        test_direct_pickle_simtask()
    except AssertionError as e:
        print(f"✗ {e}")

    try:
        test_cloudpickle_simtask()
    except AssertionError as e:
        print(f"✗ {e}")

    try:
        test_unique_parameter_set_serialization()
    except AssertionError as e:
        print(f"✗ {e}")

    try:
        test_nested_aggregation_task_serialization()
    except AssertionError as e:
        print(f"✗ {e}")

    try:
        test_simreturn_with_error_serialization()
    except AssertionError as e:
        print(f"✗ {e}")

    print("\n" + "="*60)
    print("To test with Dask, start cluster with: make dask-local")
    print("Then run: pytest tests/test_dask_serialization.py -k dask -s")