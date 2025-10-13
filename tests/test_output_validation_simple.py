"""Simplified tests for artifact-driven job completion validation.

Tests core functionality without complex contract dependencies.
"""

import pytest
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict

from modelops.services.job_state import JobState, JobStatus, now_iso, validate_transition, is_terminal
from modelops.services.job_registry import JobRegistry, ValidationResult
from modelops.services.output_manifest import OutputSpec, generate_output_manifest, reconstruct_task_from_spec
from modelops.services.provenance_store import ProvenanceStore
from modelops.services.provenance_schema import ProvenanceSchema
from modelops.services.storage.memory import InMemoryVersionedStore


class TestStateTransitions:
    """Test new state transitions for validation."""

    def test_validating_state_transitions(self):
        """Test transitions involving VALIDATING state."""
        # Can transition from RUNNING to VALIDATING
        assert validate_transition(JobStatus.RUNNING, JobStatus.VALIDATING)

        # Can transition from VALIDATING to terminal states
        assert validate_transition(JobStatus.VALIDATING, JobStatus.SUCCEEDED)
        assert validate_transition(JobStatus.VALIDATING, JobStatus.PARTIAL_SUCCESS)
        assert validate_transition(JobStatus.VALIDATING, JobStatus.FAILED)

        # Cannot transition TO validating from other states
        assert not validate_transition(JobStatus.PENDING, JobStatus.VALIDATING)
        assert not validate_transition(JobStatus.SUCCEEDED, JobStatus.VALIDATING)

        # Cannot transition FROM validating to non-terminal states
        assert not validate_transition(JobStatus.VALIDATING, JobStatus.RUNNING)
        assert not validate_transition(JobStatus.VALIDATING, JobStatus.PENDING)

    def test_partial_success_is_terminal(self):
        """Test that PARTIAL_SUCCESS is a terminal state."""
        assert is_terminal(JobStatus.PARTIAL_SUCCESS)
        assert is_terminal(JobStatus.SUCCEEDED)
        assert is_terminal(JobStatus.FAILED)
        assert is_terminal(JobStatus.CANCELLED)

        assert not is_terminal(JobStatus.VALIDATING)
        assert not is_terminal(JobStatus.RUNNING)


class TestOutputManifest:
    """Test output manifest generation with simplified inputs."""

    def test_generate_manifest_simple(self):
        """Test generating manifest from simplified job spec."""
        # Create a simple job spec using SimpleNamespace
        job_spec = SimpleNamespace(
            metadata={"bundle_digest": "abc123"},
            parameter_sets=[
                SimpleNamespace(param_id="p1", params={"x": 1}, replicate_count=2),
                SimpleNamespace(param_id="p2", params={"y": 2}, replicate_count=3),
            ]
        )

        schema = ProvenanceSchema()
        manifest = generate_output_manifest(job_spec, schema)

        # Should have 5 outputs (2 + 3)
        assert len(manifest) == 5

        # Check outputs for first param set
        p1_outputs = [o for o in manifest if o.param_id == "p1"]
        assert len(p1_outputs) == 2
        assert all(o.output_type == "simulation" for o in p1_outputs)
        assert [o.seed for o in p1_outputs] == [0, 1]

        # Check outputs for second param set
        p2_outputs = [o for o in manifest if o.param_id == "p2"]
        assert len(p2_outputs) == 3
        assert [o.seed for o in p2_outputs] == [0, 1, 2]

    def test_reconstruct_task(self):
        """Test reconstructing a task from output spec."""
        spec = OutputSpec(
            param_id="test123",
            seed=42,
            output_type="simulation",
            bundle_digest="sha256:" + "0" * 64,  # Valid sha256 format
            replicate_count=5,
            provenance_path="some/path",
            param_values={"alpha": 0.1, "beta": 0.2}
        )

        task = reconstruct_task_from_spec(spec)

        assert task is not None
        assert task.params.param_id == "test123"  # param_id is in the UniqueParameterSet
        assert task.seed == 42
        assert task.params.params == {"alpha": 0.1, "beta": 0.2}  # params are nested


class TestJobRegistryValidation:
    """Test validation functionality in JobRegistry."""

    @pytest.fixture
    def registry_setup(self, tmp_path):
        """Set up registry with provenance store."""
        versioned_store = InMemoryVersionedStore()
        provenance_dir = tmp_path / "provenance"
        provenance_dir.mkdir()

        schema = ProvenanceSchema()
        provenance_store = ProvenanceStore(provenance_dir, schema)

        registry = JobRegistry(
            versioned_store,
            provenance_store=provenance_store,
            provenance_schema=schema
        )

        return registry, provenance_dir

    def test_transition_to_validating(self, registry_setup):
        """Test moving job to VALIDATING state."""
        registry, _ = registry_setup

        # Create and advance job to RUNNING
        state = registry.register_job("job1", "k8s-job", "namespace")
        registry.update_status("job1", JobStatus.SUBMITTING)
        registry.update_status("job1", JobStatus.SCHEDULED)
        registry.update_status("job1", JobStatus.RUNNING)

        # Transition to validating
        updated = registry.transition_to_validating("job1")

        assert updated.status == JobStatus.VALIDATING
        assert updated.validation_started_at is not None
        assert updated.validation_attempts == 1

    def test_validate_outputs_complete(self, registry_setup):
        """Test validation when all outputs exist."""
        registry, provenance_dir = registry_setup

        # Register job with expected outputs
        job_spec = SimpleNamespace(
            metadata={"bundle_digest": "test123"},
            parameter_sets=[
                SimpleNamespace(param_id="p1", params={"x": 1}, replicate_count=2)
            ]
        )

        state = registry.register_job(
            "test-job",
            "k8s-job",
            "namespace",
            job_spec=job_spec
        )

        # Create all expected output files
        for output_dict in state.expected_outputs:
            spec = OutputSpec(**output_dict)
            path = provenance_dir / spec.provenance_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

        # Validate
        result = registry.validate_outputs("test-job")

        assert result.status == "complete"
        assert result.verified_count == 2
        assert result.missing_count == 0

    def test_validate_outputs_partial(self, registry_setup):
        """Test validation with missing outputs."""
        registry, provenance_dir = registry_setup

        # Register job
        job_spec = SimpleNamespace(
            metadata={"bundle_digest": "test456"},
            parameter_sets=[
                SimpleNamespace(param_id="p2", params={"y": 2}, replicate_count=4)
            ]
        )

        state = registry.register_job(
            "partial-job",
            "k8s-job",
            "namespace",
            job_spec=job_spec
        )

        # Create only 2 of 4 outputs
        for output_dict in state.expected_outputs[:2]:
            spec = OutputSpec(**output_dict)
            path = provenance_dir / spec.provenance_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

        # Validate
        result = registry.validate_outputs("partial-job")

        assert result.status == "partial"
        assert result.verified_count == 2
        assert result.missing_count == 2

    def test_finalize_with_validation(self, registry_setup):
        """Test finalizing job based on validation."""
        registry, _ = registry_setup

        # Create job and move to VALIDATING
        state = registry.register_job("job1", "k8s-job", "namespace")
        registry.update_status("job1", JobStatus.SUBMITTING)
        registry.update_status("job1", JobStatus.SCHEDULED)
        registry.update_status("job1", JobStatus.RUNNING)
        registry.transition_to_validating("job1")

        # Finalize with complete validation
        validation = ValidationResult(
            status="complete",
            verified_count=5,
            missing_count=0
        )

        final = registry.finalize_with_validation("job1", validation)
        assert final.status == JobStatus.SUCCEEDED
        assert final.tasks_verified == 5

        # Test partial success (new job)
        registry.register_job("job2", "k8s-job2", "namespace")
        registry.update_status("job2", JobStatus.SUBMITTING)
        registry.update_status("job2", JobStatus.SCHEDULED)
        registry.update_status("job2", JobStatus.RUNNING)
        registry.transition_to_validating("job2")

        validation_partial = ValidationResult(
            status="partial",
            verified_count=3,
            missing_count=2,
            missing_outputs=["path1", "path2"]
        )

        final2 = registry.finalize_with_validation("job2", validation_partial)
        assert final2.status == JobStatus.PARTIAL_SUCCESS
        assert len(final2.missing_outputs) == 2

    def test_get_resumable_tasks(self, registry_setup):
        """Test extracting resumable tasks."""
        registry, _ = registry_setup

        # Create job with outputs
        job_spec = SimpleNamespace(
            metadata={"bundle_digest": "sha256:" + "0" * 64},  # Valid sha256 format
            parameter_sets=[
                SimpleNamespace(param_id="p1", params={"x": 1, "y": 2}, replicate_count=3)
            ]
        )

        state = registry.register_job(
            "resume-job",
            "k8s-job",
            "namespace",
            job_spec=job_spec
        )

        # Move to PARTIAL_SUCCESS
        registry.update_status("resume-job", JobStatus.SUBMITTING)
        registry.update_status("resume-job", JobStatus.SCHEDULED)
        registry.update_status("resume-job", JobStatus.RUNNING)
        registry.transition_to_validating("resume-job")

        # Mark first output as missing
        missing_spec = OutputSpec(**state.expected_outputs[0])
        validation = ValidationResult(
            status="partial",
            verified_count=2,
            missing_count=1,
            missing_outputs=[missing_spec.provenance_path]
        )

        registry.finalize_with_validation("resume-job", validation)

        # Get resumable tasks
        tasks = registry.get_resumable_tasks("resume-job")

        assert len(tasks) == 1
        assert tasks[0].params.param_id == "p1"  # param_id is in the UniqueParameterSet
        assert tasks[0].seed == 0
        assert tasks[0].params.params == {"x": 1, "y": 2}  # params are nested


class TestJobStateFields:
    """Test JobState with new validation fields."""

    def test_validation_fields(self):
        """Test JobState handles validation fields."""
        state = JobState(
            job_id="test",
            status=JobStatus.VALIDATING,
            created_at=now_iso(),
            updated_at=now_iso(),
            expected_outputs=[{"path": "test"}],
            verified_outputs=["path1"],
            missing_outputs=["path2"],
            tasks_verified=10,
            validation_started_at=now_iso(),
            validation_attempts=3
        )

        # Should serialize correctly
        data = state.to_dict()
        assert data["status"] == "validating"
        assert data["tasks_verified"] == 10
        assert data["validation_attempts"] == 3

        # Should deserialize correctly
        state2 = JobState.from_dict(data)
        assert state2.status == JobStatus.VALIDATING
        assert state2.tasks_verified == 10

    def test_backward_compatibility(self):
        """Test old job records still work."""
        old_data = {
            "job_id": "old-job",
            "status": "succeeded",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "tasks_total": 5,
            "tasks_completed": 5
        }

        state = JobState.from_dict(old_data)
        assert state.job_id == "old-job"
        assert state.expected_outputs == []
        assert state.tasks_verified == 0