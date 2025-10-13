"""Integration test for job registry with CLI.

This test verifies the full flow:
1. Job submission with registry
2. Status checking via CLI
3. Listing jobs
"""

import os
import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from modelops.services.storage.memory import InMemoryVersionedStore
from modelops.services.job_registry import JobRegistry
from modelops.services.job_state import JobStatus
from modelops.client.job_submission import JobSubmissionClient
from modelops_contracts import SimulationStudy


def test_job_submission_with_registry():
    """Test that job submission integrates with registry."""
    # Create in-memory registry
    store = InMemoryVersionedStore()
    registry = JobRegistry(store)

    # Mock the Kubernetes client creation
    with patch("modelops.client.job_submission.get_k8s_client") as mock_k8s:
        # Mock K8s client methods
        mock_v1 = MagicMock()
        mock_apps_v1 = MagicMock()
        mock_k8s.return_value = (mock_v1, mock_apps_v1, None)

        # Mock batch API for Job creation
        with patch("kubernetes.client.BatchV1Api") as mock_batch:
            mock_batch_instance = MagicMock()
            mock_batch.return_value = mock_batch_instance

            # Mock storage connection retrieval and Azure services
            with patch.object(JobSubmissionClient, "_get_storage_connection", return_value="fake-connection"), \
                 patch("modelops.client.job_submission.AzureBlobBackend") as mock_storage, \
                 patch("modelops.client.job_submission.AzureVersionedStore") as mock_versioned:

                # Configure mocks
                mock_storage_instance = MagicMock()
                mock_storage.return_value = mock_storage_instance
                mock_versioned_instance = MagicMock()
                mock_versioned.return_value = mock_versioned_instance

                # Create client
                client = JobSubmissionClient(env="test")
                client.registry = registry  # Replace with our test registry

                # Mock additional methods
                client._upload_job = MagicMock(return_value="jobs/test-job.json")
                client._get_registry_url = MagicMock(return_value="test-registry.io")
                client._create_k8s_job = MagicMock()  # Mock K8s job creation

                # Create a test study with valid import path
                study = SimulationStudy(
                    model="test_module.test_model",
                    scenario="test_scenario",
                    parameter_sets=[{"param1": 1.0}, {"param2": 2.0}],
                    sampling_method="grid",
                    n_replicates=1,
                )

                # Submit the job with valid sha256 digest (64 hex chars)
                job_id = client.submit_sim_job(
                    study=study,
                    bundle_strategy="explicit",
                    bundle_ref="test-bundle@sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
                )

            # Verify job was registered
            job_state = registry.get_job(job_id)
            assert job_state is not None
            assert job_state.status == JobStatus.SCHEDULED
            assert job_state.k8s_name == f"job-{job_id}"
            assert job_state.k8s_namespace == "modelops-dask-dev"

            # Simulate job running
            registry.update_status(job_id, JobStatus.RUNNING)
            registry.update_progress(job_id, tasks_completed=1, tasks_total=2)

            # Check updated state
            job_state = registry.get_job(job_id)
            assert job_state.status == JobStatus.RUNNING
            assert job_state.tasks_completed == 1
            assert job_state.tasks_total == 2
            assert job_state.progress_percent == 50.0

            # Simulate job completion
            registry.finalize_job(
                job_id,
                JobStatus.SUCCEEDED,
                results_path="s3://bucket/results/test"
            )

            # Verify final state
            job_state = registry.get_job(job_id)
            assert job_state.status == JobStatus.SUCCEEDED
            assert job_state.results_path == "s3://bucket/results/test"
            assert job_state.is_terminal


def test_cli_status_command():
    """Test the CLI status command with mock registry."""
    from typer.testing import CliRunner
    from modelops.cli.jobs import app

    runner = CliRunner()

    # Create in-memory registry with a test job
    store = InMemoryVersionedStore()
    registry = JobRegistry(store)

    # Register a test job
    registry.register_job(
        job_id="test-123",
        k8s_name="job-test-123",
        namespace="modelops-dask-dev",
        metadata={"test": True}
    )
    registry.update_status("test-123", JobStatus.SUBMITTING)
    registry.update_status("test-123", JobStatus.SCHEDULED)
    registry.update_status("test-123", JobStatus.RUNNING)
    registry.update_progress("test-123", tasks_completed=5, tasks_total=10)

    # Mock both resolve_env and _get_registry
    with patch("modelops.cli.utils.resolve_env", return_value="dev"), \
         patch("modelops.cli.jobs._get_registry") as mock_get_registry:
        mock_get_registry.return_value = registry

        # Run the status command
        result = runner.invoke(app, ["status", "test-123"])

        # Check output contains expected information
        assert result.exit_code == 0
        assert "test-123" in result.stdout
        assert "running" in result.stdout.lower()
        assert "5/10" in result.stdout  # Progress
        assert "50.0%" in result.stdout


def test_cli_list_command():
    """Test the CLI list command with mock registry."""
    from typer.testing import CliRunner
    from modelops.cli.jobs import app

    runner = CliRunner()

    # Create in-memory registry with test jobs
    store = InMemoryVersionedStore()
    registry = JobRegistry(store)

    # Register multiple test jobs
    for i in range(3):
        job_id = f"test-{i:03d}"
        registry.register_job(
            job_id=job_id,
            k8s_name=f"job-{job_id}",
            namespace="modelops-dask-dev"
        )

        # Different statuses
        if i == 0:
            registry.update_status(job_id, JobStatus.SUBMITTING)
            registry.update_status(job_id, JobStatus.SCHEDULED)
            registry.update_status(job_id, JobStatus.RUNNING)
        elif i == 1:
            registry.update_status(job_id, JobStatus.SUBMITTING)
            registry.update_status(job_id, JobStatus.SCHEDULED)
            registry.update_status(job_id, JobStatus.RUNNING)
            # Must go through VALIDATING before SUCCEEDED (new state machine)
            registry.update_status(job_id, JobStatus.VALIDATING)
            registry.update_status(job_id, JobStatus.SUCCEEDED)
        else:
            registry.update_status(job_id, JobStatus.SUBMITTING)
            # Can go directly from SUBMITTING to FAILED
            registry.update_status(job_id, JobStatus.FAILED)

    # Mock both resolve_env and _get_registry
    with patch("modelops.cli.utils.resolve_env", return_value="dev"), \
         patch("modelops.cli.jobs._get_registry") as mock_get_registry:
        mock_get_registry.return_value = registry

        # Run the list command
        result = runner.invoke(app, ["list"])

        # Check output
        assert result.exit_code == 0
        assert "test-000" in result.stdout
        assert "test-001" in result.stdout
        assert "test-002" in result.stdout
        assert "running" in result.stdout.lower()
        assert "succeeded" in result.stdout.lower()
        assert "failed" in result.stdout.lower()

        # Test with status filter
        result = runner.invoke(app, ["list", "--status", "running"])
        assert result.exit_code == 0
        assert "test-000" in result.stdout
        assert "test-001" not in result.stdout  # succeeded
        assert "test-002" not in result.stdout  # failed


if __name__ == "__main__":
    test_job_submission_with_registry()
    test_cli_status_command()
    test_cli_list_command()
    print("✅ All integration tests passed!")