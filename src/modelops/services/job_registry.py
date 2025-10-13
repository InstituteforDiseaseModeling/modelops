"""Job registry for tracking job lifecycle.

Provides high-level job state management with business logic,
built on top of the VersionedStore for cloud-agnostic storage.
"""

import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone

from modelops_contracts import SimJob, SimTask, UniqueParameterSet

from .storage.versioned import VersionedStore
from .storage.retry import update_with_retry, create_with_retry, get_json
from .job_state import (
    JobState,
    JobStatus,
    now_iso,
    validate_transition,
    is_terminal,
    InvalidTransitionError,
    TerminalStateError,
    JobExistsError
)
from .output_manifest import OutputSpec, generate_output_manifest, reconstruct_task_from_spec
from .provenance_store import ProvenanceStore
from .provenance_schema import ProvenanceSchema

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of output validation for a job.

    Tracks which outputs exist and which are missing,
    along with overall completion status.
    """
    status: str  # "complete", "partial", "failed", "unavailable"
    verified_count: int = 0
    missing_count: int = 0
    verified_outputs: Optional[List[str]] = None
    missing_outputs: Optional[List[str]] = None
    error: Optional[str] = None


class JobRegistry:
    """Job state management with business logic.

    Built on VersionedStore for portability across cloud providers.
    Enforces state machine transitions and provides query operations.
    """

    def __init__(
        self,
        store: VersionedStore,
        prefix: str = "jobs",
        provenance_store: Optional[ProvenanceStore] = None,
        provenance_schema: Optional[ProvenanceSchema] = None
    ):
        """Initialize registry.

        Args:
            store: VersionedStore implementation (Azure, GCS, etc.)
            prefix: Key prefix for job state (default: "jobs")
            provenance_store: ProvenanceStore for output validation
            provenance_schema: Schema for generating paths
        """
        self.store = store
        self.prefix = prefix
        self.provenance = provenance_store
        self.provenance_schema = provenance_schema

    def _make_key(self, job_id: str) -> str:
        """Construct storage key for a job."""
        return f"{self.prefix}/{job_id}/state.json"

    def register_job(
        self,
        job_id: str,
        k8s_name: str,
        namespace: str,
        job_spec: Optional[SimJob] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> JobState:
        """Register a new job with pending status.

        Args:
            job_id: Unique job identifier
            k8s_name: Kubernetes job name
            namespace: Kubernetes namespace
            job_spec: Optional job specification for manifest generation
            metadata: Optional additional metadata

        Returns:
            Created JobState

        Raises:
            JobExistsError: If job already registered
        """
        # Generate output manifest if job spec provided
        expected_outputs = []
        if job_spec and self.provenance_schema:
            try:
                output_specs = generate_output_manifest(job_spec, self.provenance_schema)
                expected_outputs = [asdict(spec) for spec in output_specs]
            except Exception as e:
                logger.warning(f"Failed to generate manifest for job {job_id}: {e}")

        state = JobState(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=now_iso(),
            updated_at=now_iso(),
            k8s_name=k8s_name,
            k8s_namespace=namespace,
            expected_outputs=expected_outputs,
            tasks_total=len(expected_outputs),
            metadata=metadata or {}
        )

        key = self._make_key(job_id)
        if not create_with_retry(self.store, key, state.to_dict()):
            raise JobExistsError(f"Job {job_id} already registered")

        logger.info(f"Registered job {job_id} in namespace {namespace} with {len(expected_outputs)} expected outputs")
        return state

    def update_status(
        self,
        job_id: str,
        new_status: JobStatus,
        **kwargs
    ) -> JobState:
        """Update job status with validation.

        Args:
            job_id: Job identifier
            new_status: New status to transition to
            **kwargs: Additional fields to update

        Returns:
            Updated JobState

        Raises:
            KeyError: If job doesn't exist
            InvalidTransitionError: If transition is invalid
            TerminalStateError: If job is already in terminal state
        """
        key = self._make_key(job_id)

        def update_fn(state_dict: dict) -> dict:
            state = JobState.from_dict(state_dict)

            # Check if already in terminal state
            if state.is_terminal:
                if state.status == new_status:
                    return state_dict  # No-op if same status
                raise TerminalStateError(
                    f"Cannot modify terminal state {state.status}"
                )

            # Validate transition
            if not validate_transition(state.status, new_status):
                raise InvalidTransitionError(
                    f"Invalid transition from {state.status} to {new_status}"
                )

            # Apply updates
            updates = {
                'status': new_status.value,
                'updated_at': now_iso()
            }

            # Add any additional fields
            for key, value in kwargs.items():
                if key in {'error_message', 'error_code', 'results_path',
                          'tasks_completed', 'tasks_total', 'k8s_uid',
                          'validation_started_at', 'validation_completed_at',
                          'validation_attempts', 'last_validation_error',
                          'tasks_verified', 'verified_outputs', 'missing_outputs'}:
                    updates[key] = value
                elif key == 'metadata' and isinstance(value, dict):
                    # Merge metadata
                    current_metadata = state_dict.get('metadata', {})
                    current_metadata.update(value)
                    updates['metadata'] = current_metadata

            state_dict.update(updates)
            return state_dict

        updated = update_with_retry(self.store, key, update_fn)
        logger.info(f"Updated job {job_id} status to {new_status.value}")
        return JobState.from_dict(updated)

    def update_progress(
        self,
        job_id: str,
        tasks_completed: Optional[int] = None,
        tasks_total: Optional[int] = None
    ) -> JobState:
        """Update job progress counters.

        This is a convenience method that doesn't validate state transitions,
        allowing progress updates at any time.

        Args:
            job_id: Job identifier
            tasks_completed: Number of completed tasks
            tasks_total: Total number of tasks

        Returns:
            Updated JobState
        """
        key = self._make_key(job_id)

        def update_fn(state_dict: dict) -> dict:
            if tasks_completed is not None:
                state_dict['tasks_completed'] = tasks_completed
            if tasks_total is not None:
                state_dict['tasks_total'] = tasks_total
            state_dict['updated_at'] = now_iso()
            return state_dict

        updated = update_with_retry(self.store, key, update_fn, max_attempts=3)

        if tasks_completed is not None:
            logger.debug(f"Updated job {job_id} progress: {tasks_completed}/{tasks_total or '?'}")

        return JobState.from_dict(updated)

    def get_job(self, job_id: str) -> Optional[JobState]:
        """Get current job state.

        Args:
            job_id: Job identifier

        Returns:
            JobState if exists, None otherwise
        """
        key = self._make_key(job_id)
        state_dict = get_json(self.store, key)

        if state_dict is None:
            return None

        return JobState.from_dict(state_dict)

    def list_jobs(
        self,
        limit: int = 100,
        status_filter: Optional[List[JobStatus]] = None,
        since: Optional[datetime] = None
    ) -> List[JobState]:
        """List jobs with optional filtering.

        Args:
            limit: Maximum number of jobs to return
            status_filter: Only return jobs with these statuses
            since: Only return jobs created after this time

        Returns:
            List of JobState objects, sorted by creation time (newest first)
        """
        # List all job state files
        keys = self.store.list_keys(f"{self.prefix}/")

        jobs = []
        for key in keys:
            if not key.endswith("/state.json"):
                continue

            state_dict = get_json(self.store, key)
            if state_dict is None:
                continue

            try:
                state = JobState.from_dict(state_dict)

                # Apply filters
                if status_filter and state.status not in status_filter:
                    continue

                if since:
                    created_at = datetime.fromisoformat(state.created_at)
                    if created_at < since:
                        continue

                jobs.append(state)

            except Exception as e:
                logger.warning(f"Failed to parse job state from {key}: {e}")
                continue

        # Sort by creation time (newest first)
        jobs.sort(key=lambda j: j.created_at, reverse=True)

        # Apply limit
        return jobs[:limit]

    def finalize_job(
        self,
        job_id: str,
        final_status: JobStatus,
        results_path: Optional[str] = None,
        error_info: Optional[Dict[str, str]] = None
    ) -> JobState:
        """Finalize a job with terminal status.

        This method enriches the job state with final information before
        transitioning to a terminal state. This ensures all metadata is
        captured atomically with the terminal transition.

        Args:
            job_id: Job identifier
            final_status: Terminal status (SUCCEEDED, FAILED, CANCELLED)
            results_path: Path to results (for successful jobs)
            error_info: Error details (for failed jobs)

        Returns:
            Updated JobState

        Raises:
            ValueError: If status is not terminal
            InvalidTransitionError: If transition is invalid
        """
        if not is_terminal(final_status):
            raise ValueError(f"{final_status} is not a terminal status")

        kwargs = {}
        if results_path:
            kwargs['results_path'] = results_path
        if error_info:
            kwargs['error_message'] = error_info.get('message')
            kwargs['error_code'] = error_info.get('code')

        return self.update_status(job_id, final_status, **kwargs)

    def cancel_job(self, job_id: str, reason: Optional[str] = None) -> JobState:
        """Cancel a job.

        Args:
            job_id: Job identifier
            reason: Optional cancellation reason

        Returns:
            Updated JobState
        """
        kwargs = {}
        if reason:
            kwargs['error_message'] = f"Cancelled: {reason}"

        return self.update_status(job_id, JobStatus.CANCELLED, **kwargs)

    def count_jobs_by_status(self) -> Dict[JobStatus, int]:
        """Get count of jobs grouped by status.

        Returns:
            Dictionary mapping status to count
        """
        counts = {status: 0 for status in JobStatus}

        jobs = self.list_jobs(limit=10000)  # Get all jobs
        for job in jobs:
            counts[job.status] += 1

        return counts

    def get_active_jobs(self) -> List[JobState]:
        """Get all non-terminal jobs.

        Returns:
            List of active JobState objects
        """
        active_statuses = [
            JobStatus.PENDING,
            JobStatus.SUBMITTING,
            JobStatus.SCHEDULED,
            JobStatus.RUNNING,
            JobStatus.VALIDATING
        ]
        return self.list_jobs(status_filter=active_statuses)

    def get_recent_jobs(self, hours: int = 24) -> List[JobState]:
        """Get jobs created in the last N hours.

        Args:
            hours: Number of hours to look back

        Returns:
            List of recent JobState objects
        """
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        return self.list_jobs(since=since)

    def validate_outputs(self, job_id: str) -> ValidationResult:
        """Check if all expected outputs exist in ProvenanceStore.

        Args:
            job_id: Job identifier to validate

        Returns:
            ValidationResult with status and details
        """
        # Check if we have ProvenanceStore configured
        if not self.provenance:
            logger.warning(f"ProvenanceStore not configured for job {job_id} validation")
            return ValidationResult(
                status="unavailable",
                error="ProvenanceStore not configured"
            )

        # Get job state
        job_state = self.get_job(job_id)
        if not job_state:
            return ValidationResult(
                status="failed",
                error=f"Job {job_id} not found"
            )

        # Check if we have expected outputs
        if not job_state.expected_outputs:
            logger.info(f"Job {job_id} has no expected outputs, skipping validation")
            return ValidationResult(
                status="unavailable",
                error="No expected outputs defined"
            )

        # Check each expected output
        verified_outputs = []
        missing_outputs = []

        for output_dict in job_state.expected_outputs:
            try:
                output_spec = OutputSpec(**output_dict)
                path = self.provenance.storage_dir / output_spec.provenance_path

                if path.exists():
                    verified_outputs.append(output_spec.provenance_path)
                else:
                    missing_outputs.append(output_spec.provenance_path)

            except Exception as e:
                logger.warning(f"Error checking output: {e}")
                missing_outputs.append(output_dict.get('provenance_path', 'unknown'))

        # Determine overall status
        if len(missing_outputs) == 0:
            status = "complete"
        elif len(verified_outputs) > 0:
            status = "partial"
        else:
            status = "failed"

        return ValidationResult(
            status=status,
            verified_count=len(verified_outputs),
            missing_count=len(missing_outputs),
            verified_outputs=verified_outputs,
            missing_outputs=missing_outputs
        )

    def transition_to_validating(self, job_id: str) -> JobState:
        """Transition job to VALIDATING state when K8s completes.

        Args:
            job_id: Job identifier

        Returns:
            Updated JobState

        Raises:
            InvalidTransitionError: If transition is not allowed
        """
        # Update status and mark validation started
        return self.update_status(
            job_id,
            JobStatus.VALIDATING,
            validation_started_at=now_iso(),
            validation_attempts=1
        )

    def finalize_with_validation(self, job_id: str, validation_result: ValidationResult) -> JobState:
        """Finalize job based on validation results.

        Args:
            job_id: Job identifier
            validation_result: Results from validate_outputs

        Returns:
            Updated JobState with final status
        """
        # Determine final status based on validation
        if validation_result.status == "complete":
            final_status = JobStatus.SUCCEEDED
        elif validation_result.status == "partial":
            final_status = JobStatus.PARTIAL_SUCCESS
        else:
            final_status = JobStatus.FAILED

        # Update job with validation results
        key = self._make_key(job_id)

        def update_fn(state_dict: dict) -> dict:
            state = JobState.from_dict(state_dict)

            # Only update if in VALIDATING state
            if state.status != JobStatus.VALIDATING:
                logger.warning(f"Job {job_id} not in VALIDATING state, skipping finalization")
                return state_dict

            # Apply updates
            state_dict['status'] = final_status.value
            state_dict['updated_at'] = now_iso()
            state_dict['validation_completed_at'] = now_iso()
            state_dict['tasks_verified'] = validation_result.verified_count
            state_dict['verified_outputs'] = validation_result.verified_outputs or []
            state_dict['missing_outputs'] = validation_result.missing_outputs or []

            if validation_result.error:
                state_dict['last_validation_error'] = validation_result.error

            return state_dict

        updated = update_with_retry(self.store, key, update_fn)
        logger.info(
            f"Finalized job {job_id} with status {final_status.value} "
            f"({validation_result.verified_count} verified, {validation_result.missing_count} missing)"
        )
        return JobState.from_dict(updated)

    def get_resumable_tasks(self, job_id: str) -> List[SimTask]:
        """Get list of tasks that need to be re-run.

        Args:
            job_id: Job identifier

        Returns:
            List of SimTask objects for missing outputs
        """
        job_state = self.get_job(job_id)
        if not job_state:
            return []

        # Only resume partial success jobs
        if job_state.status != JobStatus.PARTIAL_SUCCESS:
            logger.warning(f"Job {job_id} is not in PARTIAL_SUCCESS state, cannot get resumable tasks")
            return []

        resumable_tasks = []

        # Parse missing outputs to reconstruct tasks
        for output_path in job_state.missing_outputs:
            # Find the corresponding OutputSpec
            for output_dict in job_state.expected_outputs:
                output_spec = OutputSpec(**output_dict)
                if output_spec.provenance_path == output_path:
                    # Reconstruct the task
                    task = reconstruct_task_from_spec(output_spec)
                    if task:
                        resumable_tasks.append(task)
                    break

        logger.info(f"Found {len(resumable_tasks)} resumable tasks for job {job_id}")
        return resumable_tasks