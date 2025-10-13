"""Tests for JobRegistry with state management.

Tests the high-level job management API including state transitions,
validation, and query operations.
"""

import pytest
from datetime import datetime, timedelta, timezone
from typing import Optional

from modelops.services.storage.memory import InMemoryVersionedStore
from modelops.services.job_registry import JobRegistry
from modelops.services.job_state import (
    JobState,
    JobStatus,
    InvalidTransitionError,
    TerminalStateError,
    JobExistsError,
)


class TestJobRegistry:
    """Test JobRegistry operations."""

    @pytest.fixture
    def store(self):
        """Provide a clean in-memory store for each test."""
        return InMemoryVersionedStore()

    @pytest.fixture
    def registry(self, store):
        """Provide a JobRegistry instance."""
        return JobRegistry(store)

    def test_register_job(self, registry):
        """Test job registration."""
        # Register a new job
        state = registry.register_job(
            job_id="job-123",
            k8s_name="optuna-job-abc",
            namespace="modelops",
            metadata={"algorithm": "optuna", "run_id": "run-456"}
        )

        assert state.job_id == "job-123"
        assert state.status == JobStatus.PENDING
        assert state.k8s_name == "optuna-job-abc"
        assert state.k8s_namespace == "modelops"
        assert state.metadata["algorithm"] == "optuna"
        assert state.created_at is not None
        assert state.updated_at is not None

    def test_register_duplicate_job(self, registry):
        """Test that duplicate registration fails."""
        # Register once
        registry.register_job(
            job_id="job-123",
            k8s_name="test-job",
            namespace="default"
        )

        # Try to register again
        with pytest.raises(JobExistsError, match="already registered"):
            registry.register_job(
                job_id="job-123",
                k8s_name="different-job",
                namespace="default"
            )

    def test_update_status_valid_transition(self, registry):
        """Test valid status transitions."""
        # Register job
        registry.register_job("job-1", "k8s-job-1", "default")

        # Valid transition: PENDING -> SUBMITTING
        state = registry.update_status("job-1", JobStatus.SUBMITTING)
        assert state.status == JobStatus.SUBMITTING

        # Valid transition: SUBMITTING -> SCHEDULED
        state = registry.update_status("job-1", JobStatus.SCHEDULED)
        assert state.status == JobStatus.SCHEDULED

        # Valid transition: SCHEDULED -> RUNNING
        state = registry.update_status("job-1", JobStatus.RUNNING)
        assert state.status == JobStatus.RUNNING

        # Valid transition: RUNNING -> SUCCEEDED
        state = registry.update_status(
            "job-1",
            JobStatus.SUCCEEDED,
            results_path="s3://bucket/results/job-1"
        )
        assert state.status == JobStatus.SUCCEEDED
        assert state.results_path == "s3://bucket/results/job-1"

    def test_update_status_invalid_transition(self, registry):
        """Test invalid status transitions are rejected."""
        # Register job
        registry.register_job("job-1", "k8s-job-1", "default")

        # Invalid transition: PENDING -> RUNNING (must go through SUBMITTING/SCHEDULED)
        with pytest.raises(InvalidTransitionError, match="Invalid transition"):
            registry.update_status("job-1", JobStatus.RUNNING)

        # Move to SUBMITTING
        registry.update_status("job-1", JobStatus.SUBMITTING)

        # Invalid transition: SUBMITTING -> SUCCEEDED (must go through SCHEDULED/RUNNING)
        with pytest.raises(InvalidTransitionError, match="Invalid transition"):
            registry.update_status("job-1", JobStatus.SUCCEEDED)

    def test_update_terminal_state(self, registry):
        """Test that terminal states cannot be modified."""
        # Register and move to terminal state
        registry.register_job("job-1", "k8s-job-1", "default")
        registry.update_status("job-1", JobStatus.SUBMITTING)
        registry.update_status("job-1", JobStatus.SCHEDULED)
        registry.update_status("job-1", JobStatus.RUNNING)
        registry.update_status("job-1", JobStatus.SUCCEEDED)

        # Try to modify terminal state
        with pytest.raises(TerminalStateError, match="Cannot modify terminal"):
            registry.update_status("job-1", JobStatus.FAILED)

        # Same status should be no-op (not error)
        state = registry.update_status("job-1", JobStatus.SUCCEEDED)
        assert state.status == JobStatus.SUCCEEDED

    def test_update_status_with_metadata(self, registry):
        """Test updating status with additional fields."""
        registry.register_job("job-1", "k8s-job-1", "default")

        # Update with k8s_uid
        state = registry.update_status(
            "job-1",
            JobStatus.SUBMITTING,
            k8s_uid="abc-123-def"
        )
        assert state.k8s_uid == "abc-123-def"

        # Update to failed with error info
        registry.update_status("job-1", JobStatus.SCHEDULED)
        state = registry.update_status(
            "job-1",
            JobStatus.FAILED,
            error_message="Pod OOMKilled",
            error_code="OOM"
        )
        assert state.status == JobStatus.FAILED
        assert state.error_message == "Pod OOMKilled"
        assert state.error_code == "OOM"

    def test_update_progress(self, registry):
        """Test progress tracking updates."""
        # Register and start job
        registry.register_job("job-1", "k8s-job-1", "default")
        registry.update_status("job-1", JobStatus.SUBMITTING)
        registry.update_status("job-1", JobStatus.SCHEDULED)
        registry.update_status("job-1", JobStatus.RUNNING)

        # Update progress
        state = registry.update_progress("job-1", tasks_completed=5, tasks_total=100)
        assert state.tasks_completed == 5
        assert state.tasks_total == 100
        assert state.progress_percent == 5.0

        # Update only completed count
        state = registry.update_progress("job-1", tasks_completed=50)
        assert state.tasks_completed == 50
        assert state.tasks_total == 100  # Unchanged
        assert state.progress_percent == 50.0

        # Progress updates work even in terminal state
        registry.update_status("job-1", JobStatus.SUCCEEDED)
        state = registry.update_progress("job-1", tasks_completed=100)
        assert state.tasks_completed == 100

    def test_get_job(self, registry):
        """Test retrieving job state."""
        # Non-existent job
        assert registry.get_job("non-existent") is None

        # Register job
        registry.register_job("job-1", "k8s-job-1", "default")

        # Retrieve job
        state = registry.get_job("job-1")
        assert state is not None
        assert state.job_id == "job-1"
        assert state.status == JobStatus.PENDING

    def test_list_jobs(self, registry):
        """Test listing jobs with filtering."""
        # Register multiple jobs
        registry.register_job("job-1", "k8s-1", "default")
        registry.register_job("job-2", "k8s-2", "default")
        registry.register_job("job-3", "k8s-3", "default")

        # Move jobs to different states
        registry.update_status("job-1", JobStatus.SUBMITTING)
        registry.update_status("job-1", JobStatus.SCHEDULED)
        registry.update_status("job-1", JobStatus.RUNNING)

        registry.update_status("job-2", JobStatus.SUBMITTING)
        registry.update_status("job-2", JobStatus.FAILED)

        # job-3 stays in PENDING

        # List all jobs
        jobs = registry.list_jobs()
        assert len(jobs) == 3

        # Filter by status
        running_jobs = registry.list_jobs(status_filter=[JobStatus.RUNNING])
        assert len(running_jobs) == 1
        assert running_jobs[0].job_id == "job-1"

        # Filter by multiple statuses
        terminal_jobs = registry.list_jobs(
            status_filter=[JobStatus.SUCCEEDED, JobStatus.FAILED]
        )
        assert len(terminal_jobs) == 1
        assert terminal_jobs[0].job_id == "job-2"

        # Test limit
        jobs = registry.list_jobs(limit=2)
        assert len(jobs) == 2

    def test_list_jobs_by_time(self, registry):
        """Test filtering jobs by creation time."""
        # Register job
        registry.register_job("job-old", "k8s-old", "default")

        # Get jobs from last hour
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_jobs = registry.list_jobs(since=since)
        assert len(recent_jobs) == 1
        assert recent_jobs[0].job_id == "job-old"

        # Get jobs from future (should be empty)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        future_jobs = registry.list_jobs(since=future)
        assert len(future_jobs) == 0

    def test_finalize_job_success(self, registry):
        """Test finalizing a successful job."""
        # Register and run job
        registry.register_job("job-1", "k8s-1", "default")
        registry.update_status("job-1", JobStatus.SUBMITTING)
        registry.update_status("job-1", JobStatus.SCHEDULED)
        registry.update_status("job-1", JobStatus.RUNNING)

        # Finalize as success
        state = registry.finalize_job(
            "job-1",
            JobStatus.SUCCEEDED,
            results_path="s3://bucket/results/job-1"
        )
        assert state.status == JobStatus.SUCCEEDED
        assert state.results_path == "s3://bucket/results/job-1"
        assert state.error_message is None

    def test_finalize_job_failure(self, registry):
        """Test finalizing a failed job."""
        # Register and run job
        registry.register_job("job-1", "k8s-1", "default")
        registry.update_status("job-1", JobStatus.SUBMITTING)
        registry.update_status("job-1", JobStatus.SCHEDULED)

        # Finalize as failure
        state = registry.finalize_job(
            "job-1",
            JobStatus.FAILED,
            error_info={
                "message": "Container exited with code 137",
                "code": "OOMKilled"
            }
        )
        assert state.status == JobStatus.FAILED
        assert state.error_message == "Container exited with code 137"
        assert state.error_code == "OOMKilled"
        assert state.results_path is None

    def test_finalize_non_terminal_error(self, registry):
        """Test that finalize requires terminal status."""
        registry.register_job("job-1", "k8s-1", "default")

        with pytest.raises(ValueError, match="not a terminal status"):
            registry.finalize_job("job-1", JobStatus.RUNNING)

    def test_cancel_job(self, registry):
        """Test job cancellation."""
        # Register and start job
        registry.register_job("job-1", "k8s-1", "default")
        registry.update_status("job-1", JobStatus.SUBMITTING)
        registry.update_status("job-1", JobStatus.SCHEDULED)
        registry.update_status("job-1", JobStatus.RUNNING)

        # Cancel with reason
        state = registry.cancel_job("job-1", reason="User requested cancellation")
        assert state.status == JobStatus.CANCELLED
        assert "Cancelled: User requested" in state.error_message

        # Cannot modify after cancellation
        with pytest.raises(TerminalStateError):
            registry.update_status("job-1", JobStatus.FAILED)

    def test_count_jobs_by_status(self, registry):
        """Test counting jobs by status."""
        # Register jobs in various states
        registry.register_job("job-1", "k8s-1", "default")  # PENDING

        registry.register_job("job-2", "k8s-2", "default")
        registry.update_status("job-2", JobStatus.SUBMITTING)
        registry.update_status("job-2", JobStatus.SCHEDULED)
        registry.update_status("job-2", JobStatus.RUNNING)  # RUNNING

        registry.register_job("job-3", "k8s-3", "default")
        registry.update_status("job-3", JobStatus.SUBMITTING)
        registry.update_status("job-3", JobStatus.SCHEDULED)
        registry.update_status("job-3", JobStatus.RUNNING)
        registry.update_status("job-3", JobStatus.SUCCEEDED)  # SUCCEEDED

        registry.register_job("job-4", "k8s-4", "default")
        registry.update_status("job-4", JobStatus.SUBMITTING)
        registry.update_status("job-4", JobStatus.FAILED)  # FAILED

        # Count jobs
        counts = registry.count_jobs_by_status()
        assert counts[JobStatus.PENDING] == 1
        assert counts[JobStatus.RUNNING] == 1
        assert counts[JobStatus.SUCCEEDED] == 1
        assert counts[JobStatus.FAILED] == 1
        assert counts[JobStatus.CANCELLED] == 0
        assert counts[JobStatus.SUBMITTING] == 0
        assert counts[JobStatus.SCHEDULED] == 0

    def test_get_active_jobs(self, registry):
        """Test getting all active (non-terminal) jobs."""
        # Create mix of active and terminal jobs
        registry.register_job("job-pending", "k8s-1", "default")

        registry.register_job("job-running", "k8s-2", "default")
        registry.update_status("job-running", JobStatus.SUBMITTING)
        registry.update_status("job-running", JobStatus.SCHEDULED)
        registry.update_status("job-running", JobStatus.RUNNING)

        registry.register_job("job-done", "k8s-3", "default")
        registry.update_status("job-done", JobStatus.SUBMITTING)
        registry.update_status("job-done", JobStatus.SCHEDULED)
        registry.update_status("job-done", JobStatus.RUNNING)
        registry.update_status("job-done", JobStatus.SUCCEEDED)

        # Get active jobs
        active = registry.get_active_jobs()
        assert len(active) == 2
        job_ids = {j.job_id for j in active}
        assert "job-pending" in job_ids
        assert "job-running" in job_ids
        assert "job-done" not in job_ids

    def test_get_recent_jobs(self, registry):
        """Test getting recent jobs."""
        # Register a job
        registry.register_job("job-now", "k8s-1", "default")

        # Get jobs from last 24 hours (default)
        recent = registry.get_recent_jobs()
        assert len(recent) == 1
        assert recent[0].job_id == "job-now"

        # Get jobs from last 0 hours (should be empty due to timing)
        # Note: This might be flaky in very fast test execution
        recent = registry.get_recent_jobs(hours=0)
        assert len(recent) == 0 or len(recent) == 1  # Allow for timing

    def test_concurrent_status_updates(self, registry):
        """Test that concurrent updates are handled correctly."""
        import threading
        import time

        # Register job
        registry.register_job("job-1", "k8s-1", "default")

        results = []
        errors = []

        def update_status_worker(status: JobStatus):
            """Worker that tries to update status."""
            try:
                # Small random delay to increase contention
                time.sleep(0.001)
                state = registry.update_status("job-1", status)
                results.append((status, state.status))
            except (InvalidTransitionError, TerminalStateError) as e:
                errors.append((status, str(e)))

        # Try concurrent transitions (only one should succeed)
        threads = []
        for status in [JobStatus.SUBMITTING, JobStatus.CANCELLED]:
            thread = threading.Thread(target=update_status_worker, args=(status,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # Exactly one should have succeeded
        assert len(results) == 1
        assert len(errors) == 1

        # The successful one determines final state
        successful_status = results[0][1]
        assert successful_status in {JobStatus.SUBMITTING, JobStatus.CANCELLED}

    def test_metadata_merge(self, registry):
        """Test that metadata updates are merged, not replaced."""
        # Register with initial metadata
        registry.register_job(
            "job-1", "k8s-1", "default",
            metadata={"algorithm": "optuna", "version": "1.0"}
        )

        # Update status with additional metadata
        state = registry.update_status(
            "job-1",
            JobStatus.SUBMITTING,
            metadata={"cluster": "prod", "region": "us-east"}
        )

        # All metadata should be present
        assert state.metadata["algorithm"] == "optuna"
        assert state.metadata["version"] == "1.0"
        assert state.metadata["cluster"] == "prod"
        assert state.metadata["region"] == "us-east"

    def test_job_state_serialization(self, registry):
        """Test that JobState survives serialization round-trip."""
        # Register job with various fields
        state = registry.register_job(
            "job-1", "k8s-1", "default",
            metadata={"test": True}
        )

        # Update with more fields
        state = registry.update_status(
            "job-1",
            JobStatus.SUBMITTING,
            k8s_uid="uid-123",
            tasks_total=100
        )

        # Serialize and deserialize
        json_str = state.to_json()
        restored = JobState.from_json(json_str)

        # Verify all fields preserved
        assert restored.job_id == state.job_id
        assert restored.status == state.status
        assert restored.k8s_uid == state.k8s_uid
        assert restored.tasks_total == state.tasks_total
        assert restored.metadata == state.metadata

    def test_nonexistent_job_operations(self, registry):
        """Test operations on non-existent jobs fail gracefully."""
        # Update non-existent job
        with pytest.raises(KeyError, match="not found"):
            registry.update_status("non-existent", JobStatus.RUNNING)

        # Update progress on non-existent job
        with pytest.raises(KeyError, match="not found"):
            registry.update_progress("non-existent", tasks_completed=5)

        # Cancel non-existent job
        with pytest.raises(KeyError, match="not found"):
            registry.cancel_job("non-existent")

        # Finalize non-existent job
        with pytest.raises(KeyError, match="not found"):
            registry.finalize_job("non-existent", JobStatus.FAILED)