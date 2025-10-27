"""Tests for the result indexer module."""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
import pyarrow.parquet as pq

from modelops_contracts import SimJob, SimTask, UniqueParameterSet, TargetSpec
from modelops_contracts.utils import canonical_task_id

from modelops.services.results_indexer import IndexerConfig, ResultIndexer
from modelops.services import provenance_paths as paths
from modelops.services.job_registry import JobRegistry
from modelops.services.job_state import JobState, JobStatus
from modelops.services.provenance_store import ProvenanceStore


@pytest.fixture
def temp_storage_dir():
    """Create temporary storage directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_job():
    """Create a mock SimJob for testing."""
    tasks = []
    for param_id in ["param001", "param002"]:
        params = UniqueParameterSet(
            param_id=param_id,
            params={"beta": 0.5, "gamma": 0.1}
        )
        for seed in range(3):  # 3 replicates per param
            tasks.append(SimTask(
                bundle_ref="sha256:abcd1234" + "0" * 56,
                entrypoint="models.seir:run",
                params=params,
                seed=seed
            ))

    return SimJob(
        job_id="job-test123",
        bundle_ref="sha256:abcd1234" + "0" * 56,
        tasks=tasks,
        metadata={"targets": ["targets.prevalence:loss"]},
    )


@pytest.fixture
def mock_registry(mock_job):
    """Create mock job registry."""
    registry = Mock(spec=JobRegistry)
    job_state = JobState(
        job_id="job-test123",
        status=JobStatus.SUCCEEDED,
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    # Add the job_spec as a custom attribute for testing
    job_state.job_spec = mock_job
    registry.get_job.return_value = job_state
    return registry


@pytest.fixture
def prov_store(temp_storage_dir):
    """Create ProvenanceStore instance."""
    return ProvenanceStore(temp_storage_dir)


class TestResultIndexer:
    """Test ResultIndexer functionality."""

    def test_path_generation(self, mock_job):
        """Test that path generation is consistent."""
        task = mock_job.tasks[0]
        task_id = canonical_task_id(task)

        # Verify task ID is stable
        task_id2 = canonical_task_id(task)
        assert task_id == task_id2

        # Test aggregation path generation
        agg_path = paths.agg_path(
            mock_job.bundle_ref,
            "targets.prevalence:loss",
            [task_id]
        )
        assert "aggs" in agg_path
        assert "target_targets.prevalence__loss" in agg_path

    def test_indexer_with_complete_results(
        self, mock_job, mock_registry, prov_store, temp_storage_dir
    ):
        """Test indexing when all results are available."""
        # Setup aggregation results
        for param_id in ["param001", "param002"]:
            # Get tasks for this param_id
            param_tasks = [t for t in mock_job.tasks if t.params.param_id == param_id]
            task_ids = [canonical_task_id(t) for t in param_tasks]

            # Create aggregation result
            agg_path = paths.agg_path(
                mock_job.bundle_ref,
                "targets.prevalence:loss",
                task_ids
            )

            # Ensure relative path
            rel_path = agg_path.lstrip("/")
            result_dir = temp_storage_dir / rel_path
            result_dir.mkdir(parents=True, exist_ok=True)

            # Write result files
            result = {
                "aggregation_id": f"agg_{param_id}",
                "loss": 0.123 if param_id == "param001" else 0.456,
                "n_replicates": 3,
                "diagnostics": {},
            }
            metadata = {
                "bundle_ref": mock_job.bundle_ref,
                "param_id": param_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            with open(result_dir / "result.json", "w") as f:
                json.dump(result, f)
            with open(result_dir / "metadata.json", "w") as f:
                json.dump(metadata, f)

        # Run indexer
        config = IndexerConfig(
            job_id="job-test123",
            prov_root=str(temp_storage_dir),
        )
        indexer = ResultIndexer(config, mock_registry, prov_store)
        result_path = indexer.run()

        # Verify Parquet file was created
        assert "losses.parquet" in result_path
        parquet_dir = temp_storage_dir / result_path.lstrip("/")
        assert parquet_dir.exists()

        # Read and verify Parquet content
        target_file = parquet_dir / "target=targets.prevalence:loss" / "part-0.parquet"
        assert target_file.exists()

        df = pq.read_table(target_file).to_pandas()
        assert len(df) == 2  # Two parameter sets
        assert set(df["param_id"]) == {"param001", "param002"}
        assert df.loc[df["param_id"] == "param001", "loss"].iloc[0] == 0.123
        assert df.loc[df["param_id"] == "param002", "loss"].iloc[0] == 0.456
        assert all(df["status"] == "available")

    def test_indexer_with_missing_results(
        self, mock_job, mock_registry, prov_store, temp_storage_dir
    ):
        """Test indexing when some results are missing."""
        # Only create result for first parameter set
        param_id = "param001"
        param_tasks = [t for t in mock_job.tasks if t.params.param_id == param_id]
        task_ids = [canonical_task_id(t) for t in param_tasks]

        agg_path = paths.agg_path(
            mock_job.bundle_ref,
            "targets.prevalence:loss",
            task_ids
        )

        rel_path = agg_path.lstrip("/")
        result_dir = temp_storage_dir / rel_path
        result_dir.mkdir(parents=True, exist_ok=True)

        result = {
            "loss": 0.123,
            "n_replicates": 3,
        }
        metadata = {
            "param_id": param_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        with open(result_dir / "result.json", "w") as f:
            json.dump(result, f)
        with open(result_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        # Run indexer
        config = IndexerConfig(job_id="job-test123", prov_root=str(temp_storage_dir))
        indexer = ResultIndexer(config, mock_registry, prov_store)
        result_path = indexer.run()

        # Read Parquet and verify
        parquet_dir = temp_storage_dir / result_path.lstrip("/")
        target_file = parquet_dir / "target=targets.prevalence:loss" / "part-0.parquet"
        df = pq.read_table(target_file).to_pandas()

        assert len(df) == 2
        available = df[df["status"] == "available"]
        missing = df[df["status"] == "missing"]

        assert len(available) == 1
        assert len(missing) == 1
        assert available["param_id"].iloc[0] == "param001"
        assert missing["param_id"].iloc[0] == "param002"
        assert available["loss"].iloc[0] == 0.123
        assert missing["loss"].isna().all()

    def test_idempotency(
        self, mock_job, mock_registry, prov_store, temp_storage_dir
    ):
        """Test that running indexer twice produces same result."""
        # Create one result
        param_id = "param001"
        param_tasks = [t for t in mock_job.tasks if t.params.param_id == param_id]
        task_ids = [canonical_task_id(t) for t in param_tasks]

        agg_path = paths.agg_path(
            mock_job.bundle_ref,
            "targets.prevalence:loss",
            task_ids
        )

        rel_path = agg_path.lstrip("/")
        result_dir = temp_storage_dir / rel_path
        result_dir.mkdir(parents=True, exist_ok=True)

        result = {"loss": 0.123, "n_replicates": 3}
        metadata = {"param_id": param_id}

        with open(result_dir / "result.json", "w") as f:
            json.dump(result, f)
        with open(result_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        # Run indexer twice
        config = IndexerConfig(job_id="job-test123", prov_root=str(temp_storage_dir))
        indexer = ResultIndexer(config, mock_registry, prov_store)

        path1 = indexer.run()
        manifest1_path = temp_storage_dir / path1.replace("losses.parquet", "manifest.json").lstrip("/")
        with open(manifest1_path, "r") as f:
            manifest1 = json.load(f)

        path2 = indexer.run()
        manifest2_path = temp_storage_dir / path2.replace("losses.parquet", "manifest.json").lstrip("/")
        with open(manifest2_path, "r") as f:
            manifest2 = json.load(f)

        # Verify same paths and fingerprints
        assert path1 == path2
        assert manifest1["input_fingerprint"] == manifest2["input_fingerprint"]
        assert manifest1["row_counts"] == manifest2["row_counts"]

    def test_manifest_generation(
        self, mock_job, mock_registry, prov_store, temp_storage_dir
    ):
        """Test that manifest and summary files are created correctly."""
        # Run indexer with no results (all missing)
        config = IndexerConfig(job_id="job-test123", prov_root=str(temp_storage_dir))
        indexer = ResultIndexer(config, mock_registry, prov_store)
        result_path = indexer.run()

        # Check manifest
        view_root = temp_storage_dir / paths.job_view_root("job-test123").lstrip("/")
        manifest_path = view_root / "manifest.json"
        assert manifest_path.exists()

        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        assert manifest["job_id"] == "job-test123"
        assert manifest["index_version"] == 1
        assert manifest["row_counts"]["total"] == 2  # 2 param sets
        assert manifest["row_counts"]["available"] == 0
        assert manifest["row_counts"]["missing"] == 2

        # Check summary
        summary_path = view_root / "summary.json"
        assert summary_path.exists()

        with open(summary_path, "r") as f:
            summary = json.load(f)

        assert "targets.prevalence:loss" in summary
        assert summary["targets.prevalence:loss"]["count"] == 2
        assert summary["targets.prevalence:loss"]["available"] == 0

        # Check schema
        schema_path = view_root / "schema.json"
        assert schema_path.exists()

    def test_job_not_found(self, prov_store, temp_storage_dir):
        """Test error when job doesn't exist."""
        mock_registry = Mock(spec=JobRegistry)
        mock_registry.get_job.return_value = None

        config = IndexerConfig(job_id="nonexistent", prov_root=str(temp_storage_dir))
        indexer = ResultIndexer(config, mock_registry, prov_store)

        with pytest.raises(ValueError, match="Job nonexistent not found"):
            indexer.run()

    def test_multiple_targets(self, mock_registry, prov_store, temp_storage_dir):
        """Test indexing with multiple targets."""
        # Create job with multiple targets
        tasks = []
        params = UniqueParameterSet(param_id="param001", params={"beta": 0.5})
        for seed in range(2):
            tasks.append(SimTask(
                bundle_ref="sha256:abcd1234" + "0" * 56,
                entrypoint="models.seir:run",
                params=params,
                seed=seed
            ))

        job = SimJob(
            job_id="job-multi",
            bundle_ref="sha256:abcd1234" + "0" * 56,
            tasks=tasks,
            metadata={"targets": ["target1:loss", "target2:loss"]},
        )

        job_state = JobState(
            job_id="job-multi",
            status=JobStatus.SUCCEEDED,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        # Add the job_spec as a custom attribute for testing
        job_state.job_spec = job
        mock_registry.get_job.return_value = job_state

        # Create results for both targets
        task_ids = [canonical_task_id(t) for t in tasks]
        for target in ["target1:loss", "target2:loss"]:
            agg_path = paths.agg_path(job.bundle_ref, target, task_ids)
            rel_path = agg_path.lstrip("/")
            result_dir = temp_storage_dir / rel_path
            result_dir.mkdir(parents=True, exist_ok=True)

            loss_value = 0.1 if target == "target1:loss" else 0.2
            result = {"loss": loss_value, "n_replicates": 2}
            metadata = {"param_id": "param001"}

            with open(result_dir / "result.json", "w") as f:
                json.dump(result, f)
            with open(result_dir / "metadata.json", "w") as f:
                json.dump(metadata, f)

        # Run indexer
        config = IndexerConfig(job_id="job-multi", prov_root=str(temp_storage_dir))
        indexer = ResultIndexer(config, mock_registry, prov_store)
        result_path = indexer.run()

        # Verify partitions for both targets
        parquet_dir = temp_storage_dir / result_path.lstrip("/")
        assert (parquet_dir / "target=target1:loss").exists()
        assert (parquet_dir / "target=target2:loss").exists()

        # Read and verify data
        df1 = pq.read_table(parquet_dir / "target=target1:loss" / "part-0.parquet").to_pandas()
        df2 = pq.read_table(parquet_dir / "target=target2:loss" / "part-0.parquet").to_pandas()

        assert len(df1) == 1
        assert len(df2) == 1
        assert df1["loss"].iloc[0] == 0.1
        assert df2["loss"].iloc[0] == 0.2