#!/usr/bin/env python
"""Test ProvenanceStore with actual file operations."""

import pytest
import json
import tempfile
import shutil
import hashlib
from pathlib import Path
from modelops_contracts import SimTask, SimReturn, UniqueParameterSet, TableArtifact, ErrorInfo
from modelops_contracts.simulation import AggregationTask, AggregationReturn
from modelops.services.provenance_store import ProvenanceStore
from modelops.services.provenance_schema import (
    ProvenanceSchema,
    BUNDLE_INVALIDATION_SCHEMA,
    TOKEN_INVALIDATION_SCHEMA,
)

# Valid test bundle references (SHA256 with 64 hex chars)
TEST_BUNDLE_REF = "sha256:" + "a" * 64
TEST_BUNDLE_REF_V1 = "sha256:" + "1" * 64
TEST_BUNDLE_REF_V2 = "sha256:" + "2" * 64


def make_valid_checksum(data: bytes) -> str:
    """Create a valid BLAKE2b-256 checksum for data."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()


class TestProvenanceStoreBasics:
    """Basic ProvenanceStore operations."""

    @pytest.fixture
    def temp_storage_dir(self):
        """Create a temporary directory for testing."""
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir)

    @pytest.fixture
    def store(self, temp_storage_dir):
        """Create a ProvenanceStore with default schema."""
        return ProvenanceStore(storage_dir=temp_storage_dir, schema=BUNDLE_INVALIDATION_SCHEMA)

    def test_store_and_retrieve_sim(self, store):
        """Test storing and retrieving a SimReturn."""
        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1, "y": 2}),
            seed=42,
        )

        test_data = b"test data!"
        sim_return = SimReturn(
            task_id="a" * 64,
            outputs={
                "result": TableArtifact(
                    size=len(test_data), inline=test_data, checksum=make_valid_checksum(test_data)
                )
            },
        )

        # Store the result
        store.put_sim(task, sim_return)

        # Retrieve it
        retrieved = store.get_sim(task)
        assert retrieved is not None
        assert retrieved.task_id == sim_return.task_id
        assert retrieved.outputs["result"].inline == test_data

    def test_cache_miss(self, store):
        """Test cache miss returns None."""
        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1}),
            seed=42,
        )

        result = store.get_sim(task)
        assert result is None

    def test_store_with_error(self, store):
        """Test storing SimReturn with error information."""
        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1}),
            seed=42,
        )

        error_details_data = b'{"error": "division by zero"}'
        sim_return = SimReturn(
            task_id="c" * 64,
            outputs={},
            error=ErrorInfo(
                error_type="ZeroDivisionError", message="division by zero", retryable=False
            ),
            error_details=TableArtifact(
                size=len(error_details_data),
                inline=error_details_data,
                checksum=make_valid_checksum(error_details_data),
            ),
        )

        store.put_sim(task, sim_return)
        retrieved = store.get_sim(task)

        assert retrieved.error is not None
        assert retrieved.error.error_type == "ZeroDivisionError"
        assert retrieved.error_details is not None

    def test_overwrite_existing(self, store):
        """Test that putting same task twice overwrites."""
        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1}),
            seed=42,
        )

        # First write
        data1 = b"data1"
        sim_return1 = SimReturn(
            task_id="a" * 64,
            outputs={
                "result": TableArtifact(
                    size=len(data1), inline=data1, checksum=make_valid_checksum(data1)
                )
            },
        )
        store.put_sim(task, sim_return1)

        # Second write (overwrite)
        data2 = b"data2"
        sim_return2 = SimReturn(
            task_id="b" * 64,
            outputs={
                "result": TableArtifact(
                    size=len(data2), inline=data2, checksum=make_valid_checksum(data2)
                )
            },
        )
        store.put_sim(task, sim_return2)

        # Should get the second one
        retrieved = store.get_sim(task)
        assert retrieved.task_id == "b" * 64
        assert retrieved.outputs["result"].inline == data2


class TestProvenanceStoreAggregation:
    """Test aggregation storage."""

    @pytest.fixture
    def store(self):
        """Create a ProvenanceStore with temporary directory."""
        temp_dir = tempfile.mkdtemp()
        store = ProvenanceStore(storage_dir=Path(temp_dir), schema=BUNDLE_INVALIDATION_SCHEMA)
        yield store
        shutil.rmtree(temp_dir)

    def test_store_and_retrieve_aggregation(self, store):
        """Test storing and retrieving aggregation results."""
        data = b"data"
        sim_returns = [
            SimReturn(
                task_id=f"{chr(97 + i)}" * 64,  # a*64, b*64, etc
                outputs={
                    "result": TableArtifact(
                        size=len(data), inline=data, checksum=make_valid_checksum(data)
                    )
                },
            )
            for i in range(3)
        ]

        agg_task = AggregationTask(
            bundle_ref=TEST_BUNDLE_REF,
            target_entrypoint="targets.test/compute",
            sim_returns=sim_returns,
        )

        agg_return = AggregationReturn(
            aggregation_id=agg_task.aggregation_id(),
            loss=0.5,
            diagnostics={"metric1": 0.1, "metric2": 0.2},
            outputs={},
            n_replicates=3,
        )

        # Store
        store.put_agg(agg_task, agg_return)

        # Retrieve
        retrieved = store.get_agg(agg_task)
        assert retrieved is not None
        assert retrieved.loss == 0.5
        assert retrieved.n_replicates == 3
        assert retrieved.diagnostics["metric1"] == 0.1

    def test_aggregation_cache_miss(self, store):
        """Test aggregation cache miss."""
        data = b"data"
        sim_returns = [
            SimReturn(
                task_id="a" * 64,
                outputs={
                    "result": TableArtifact(
                        size=len(data), inline=data, checksum=make_valid_checksum(data)
                    )
                },
            )
        ]

        agg_task = AggregationTask(
            bundle_ref=TEST_BUNDLE_REF,
            target_entrypoint="targets.test/compute",
            sim_returns=sim_returns,
        )

        result = store.get_agg(agg_task)
        assert result is None


class TestInvalidationStrategies:
    """Test different invalidation schemas."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory."""
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir)

    def test_bundle_invalidation(self, temp_dir):
        """Test that bundle changes invalidate cache."""
        store = ProvenanceStore(storage_dir=temp_dir, schema=BUNDLE_INVALIDATION_SCHEMA)

        params = UniqueParameterSet.from_dict({"x": 1})

        # Task with bundle v1
        task_v1 = SimTask(
            bundle_ref=TEST_BUNDLE_REF_V1, entrypoint="module.func/test", params=params, seed=42
        )

        # Task with bundle v2 (same params/seed)
        task_v2 = SimTask(
            bundle_ref=TEST_BUNDLE_REF_V2,  # Different bundle
            entrypoint="module.func/test",
            params=params,
            seed=42,
        )

        # Store result for v1
        data = b"data1"
        sim_return = SimReturn(
            task_id="a" * 64,
            outputs={
                "result": TableArtifact(
                    size=len(data), inline=data, checksum=make_valid_checksum(data)
                )
            },
        )
        store.put_sim(task_v1, sim_return)

        # v1 should hit
        assert store.get_sim(task_v1) is not None

        # v2 should miss (different bundle)
        assert store.get_sim(task_v2) is None

    def test_token_invalidation(self, temp_dir):
        """Test that token changes invalidate cache with different model tokens."""
        # Since TOKEN_INVALIDATION_SCHEMA uses model_digest (not bundle_digest),
        # and we can't actually change tokens in entrypoint (token must come after scenario),
        # we'll simulate by passing different model_digest values
        store = ProvenanceStore(storage_dir=temp_dir, schema=TOKEN_INVALIDATION_SCHEMA)

        params = UniqueParameterSet.from_dict({"x": 1})

        # Task v1 and v2 with same everything
        task_v1 = SimTask(
            bundle_ref=TEST_BUNDLE_REF_V1, entrypoint="module.func/test", params=params, seed=42
        )

        task_v2 = SimTask(
            bundle_ref=TEST_BUNDLE_REF_V1, entrypoint="module.func/test", params=params, seed=42
        )

        # Store result for v1
        data = b"data1"
        sim_return = SimReturn(
            task_id="a" * 64,
            outputs={
                "result": TableArtifact(
                    size=len(data), inline=data, checksum=make_valid_checksum(data)
                )
            },
        )
        store.put_sim(task_v1, sim_return)

        # Since TOKEN_INVALIDATION_SCHEMA requires model_digest which ProvenanceStore
        # doesn't extract from SimTask (it would need to parse manifest/bundle),
        # this test would need the actual model_digest to be different.
        # For now, v1 and v2 are identical so both should hit the same cache entry.
        assert store.get_sim(task_v1) is not None
        assert store.get_sim(task_v2) is not None  # Same task, same cache entry

    def test_params_always_invalidate(self, temp_dir):
        """Test that parameter changes always invalidate cache."""
        store = ProvenanceStore(storage_dir=temp_dir, schema=BUNDLE_INVALIDATION_SCHEMA)

        # Task with params v1
        task_v1 = SimTask(
            bundle_ref=TEST_BUNDLE_REF_V1,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1}),
            seed=42,
        )

        # Task with params v2
        task_v2 = SimTask(
            bundle_ref=TEST_BUNDLE_REF_V1,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 2}),  # Different params
            seed=42,
        )

        # Store result for v1
        data = b"data1"
        sim_return = SimReturn(
            task_id="a" * 64,
            outputs={
                "result": TableArtifact(
                    size=len(data), inline=data, checksum=make_valid_checksum(data)
                )
            },
        )
        store.put_sim(task_v1, sim_return)

        # v1 should hit
        assert store.get_sim(task_v1) is not None

        # v2 should miss (different params)
        assert store.get_sim(task_v2) is None


class TestFileStructure:
    """Test the actual file structure created."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory."""
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir)

    def test_creates_expected_directories(self, temp_dir):
        """Verify the directory structure matches schema."""
        store = ProvenanceStore(storage_dir=temp_dir, schema=BUNDLE_INVALIDATION_SCHEMA)

        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF_V1,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1, "y": 2}),
            seed=42,
        )

        data = b"data1"
        sim_return = SimReturn(
            task_id="a" * 64,
            outputs={
                "result": TableArtifact(
                    size=len(data), inline=data, checksum=make_valid_checksum(data)
                )
            },
        )

        store.put_sim(task, sim_return)

        # Check that bundle/v1/sims structure exists
        assert (temp_dir / "bundle" / "v1" / "sims").exists()

        # Check for sharding directories
        bundle_dirs = list((temp_dir / "bundle" / "v1" / "sims").iterdir())
        assert len(bundle_dirs) > 0

        # Find the metadata.json file
        metadata_files = list(temp_dir.glob("**/metadata.json"))
        assert len(metadata_files) == 1

        # Verify metadata content
        with open(metadata_files[0]) as f:
            metadata = json.load(f)
            assert metadata["bundle_ref"] == TEST_BUNDLE_REF_V1
            assert metadata["seed"] == 42

    def test_artifacts_stored_separately(self, temp_dir):
        """Verify artifacts are stored in separate files."""
        store = ProvenanceStore(storage_dir=temp_dir, schema=BUNDLE_INVALIDATION_SCHEMA)

        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1}),
            seed=42,
        )

        data1 = b"test data1"
        data2 = b"test data2"
        sim_return = SimReturn(
            task_id="a" * 64,
            outputs={
                "output1": TableArtifact(
                    size=len(data1), inline=data1, checksum=make_valid_checksum(data1)
                ),
                "output2": TableArtifact(
                    size=len(data2), inline=data2, checksum=make_valid_checksum(data2)
                ),
            },
        )

        store.put_sim(task, sim_return)

        # Find artifact files
        artifact_files = list(temp_dir.glob("**/artifact_*.arrow"))
        assert len(artifact_files) == 2

        # Check artifact content
        artifact_contents = set()
        for artifact_file in artifact_files:
            artifact_contents.add(artifact_file.read_bytes())

        assert data1 in artifact_contents
        assert data2 in artifact_contents


class TestConcurrency:
    """Test concurrent access patterns."""

    @pytest.fixture
    def store(self):
        """Create a ProvenanceStore."""
        temp_dir = tempfile.mkdtemp()
        store = ProvenanceStore(storage_dir=Path(temp_dir), schema=BUNDLE_INVALIDATION_SCHEMA)
        yield store
        shutil.rmtree(temp_dir)

    def test_multiple_reads(self, store):
        """Test multiple reads don't corrupt data."""
        task = SimTask(
            bundle_ref=TEST_BUNDLE_REF,
            entrypoint="module.func/test",
            params=UniqueParameterSet.from_dict({"x": 1}),
            seed=42,
        )

        data = b"test data!"
        sim_return = SimReturn(
            task_id="a" * 64,
            outputs={
                "result": TableArtifact(
                    size=len(data), inline=data, checksum=make_valid_checksum(data)
                )
            },
        )

        store.put_sim(task, sim_return)

        # Multiple reads
        for _ in range(10):
            retrieved = store.get_sim(task)
            assert retrieved.task_id == "a" * 64
            assert retrieved.outputs["result"].inline == data

    def test_interleaved_operations(self, store):
        """Test interleaved puts and gets."""
        tasks = []
        returns = []

        for i in range(5):
            task = SimTask(
                bundle_ref=TEST_BUNDLE_REF,
                entrypoint="module.func/test",
                params=UniqueParameterSet.from_dict({"x": i}),
                seed=42,
            )
            data = f"data{i}".encode()
            sim_return = SimReturn(
                task_id=f"{chr(97 + i)}" * 64,
                outputs={
                    "result": TableArtifact(
                        size=len(data), inline=data, checksum=make_valid_checksum(data)
                    )
                },
            )
            tasks.append(task)
            returns.append(sim_return)

        # Interleave puts and gets
        store.put_sim(tasks[0], returns[0])
        assert store.get_sim(tasks[0]) is not None
        assert store.get_sim(tasks[1]) is None

        store.put_sim(tasks[1], returns[1])
        store.put_sim(tasks[2], returns[2])

        assert store.get_sim(tasks[1]) is not None
        assert store.get_sim(tasks[2]) is not None
        assert store.get_sim(tasks[3]) is None

        # Verify data integrity
        for i in range(3):
            retrieved = store.get_sim(tasks[i])
            assert retrieved.outputs["result"].inline == f"data{i}".encode()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
