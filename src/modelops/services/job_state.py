"""Job state management with state machine validation.

Defines the job lifecycle states and valid transitions, along with
the data structure for tracking job state.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    """Job lifecycle states.

    String enum for JSON serialization compatibility.
    Follows Kubernetes naming conventions.
    """

    # Initial states
    PENDING = "pending"  # Job created, not yet submitted to K8s
    SUBMITTING = "submitting"  # Being submitted to Kubernetes

    # Running states
    SCHEDULED = "scheduled"  # K8s Job created, waiting for pod
    RUNNING = "running"  # Pod running, executing tasks
    VALIDATING = "validating"  # K8s complete, checking outputs exist

    # Terminal states (no transitions out)
    SUCCEEDED = "succeeded"  # All outputs verified present
    PARTIAL_SUCCESS = "partial"  # Some outputs missing (resumable)
    FAILED = "failed"  # Infrastructure or execution failure
    CANCELLED = "cancelled"  # User-cancelled or SIGTERM


# Legal state transitions to prevent invalid states
TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.SUBMITTING, JobStatus.CANCELLED},
    JobStatus.SUBMITTING: {JobStatus.SCHEDULED, JobStatus.FAILED},
    JobStatus.SCHEDULED: {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.RUNNING: {
        JobStatus.VALIDATING,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },  # SUCCEEDED for backward compat
    JobStatus.VALIDATING: {
        JobStatus.SUCCEEDED,
        JobStatus.PARTIAL_SUCCESS,
        JobStatus.FAILED,
    },
    # Terminal states - no outbound transitions
    JobStatus.SUCCEEDED: set(),
    JobStatus.PARTIAL_SUCCESS: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


def is_terminal(status: JobStatus) -> bool:
    """Check if a status is terminal (no transitions out)."""
    return status in {
        JobStatus.SUCCEEDED,
        JobStatus.PARTIAL_SUCCESS,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }


def validate_transition(from_status: JobStatus, to_status: JobStatus) -> bool:
    """Validate a state transition is legal.

    Args:
        from_status: Current status
        to_status: Desired new status

    Returns:
        True if transition is valid, False otherwise
    """
    return to_status in TRANSITIONS.get(from_status, set())


def now_iso() -> str:
    """Get current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


@dataclass
class JobState:
    """Job state for storage.

    Immutable data structure that captures all job metadata.
    Designed to be JSON serializable for storage.
    """

    # Core identification
    job_id: str
    status: JobStatus
    created_at: str
    updated_at: str

    # Kubernetes metadata
    k8s_name: str | None = None
    k8s_namespace: str | None = None
    k8s_uid: str | None = None  # For correlation with K8s events

    # Progress tracking
    tasks_total: int = 0
    tasks_completed: int = 0

    # Error information
    error_message: str | None = None
    error_code: str | None = None

    # Results
    results_path: str | None = None

    # Validation tracking
    expected_outputs: list[dict] = field(default_factory=list)  # OutputSpec dicts
    verified_outputs: list[str] = field(default_factory=list)  # Paths that exist
    missing_outputs: list[str] = field(default_factory=list)  # Paths not found
    tasks_verified: int = 0  # Count of verified tasks
    validation_started_at: str | None = None  # When validation began
    validation_completed_at: str | None = None  # When validation finished
    validation_attempts: int = 0  # Number of validation attempts
    last_validation_error: str | None = None  # Last validation error

    # Additional metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        # Convert enum to string
        data["status"] = self.status.value if isinstance(self.status, JobStatus) else self.status
        return data

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "JobState":
        """Create from dictionary."""
        # Convert status string to enum
        if "status" in data and isinstance(data["status"], str):
            data["status"] = JobStatus(data["status"])

        # Filter out unknown fields for backward compatibility
        # This allows old job records with extra fields to still be read
        from dataclasses import fields

        valid_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}

        return cls(**filtered_data)

    @classmethod
    def from_json(cls, json_str: str) -> "JobState":
        """Deserialize from JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)

    def copy_with(self, **changes) -> "JobState":
        """Create a copy with specified changes.

        Useful for updates while maintaining immutability.
        """
        data = self.to_dict()
        data.update(changes)
        return self.from_dict(data)

    @property
    def progress_percent(self) -> float | None:
        """Calculate progress percentage if applicable."""
        if self.tasks_total > 0:
            return (self.tasks_completed / self.tasks_total) * 100
        return None

    @property
    def is_terminal(self) -> bool:
        """Check if job is in terminal state."""
        return is_terminal(self.status)


class InvalidTransitionError(Exception):
    """Raised when attempting an invalid state transition."""

    pass


class TerminalStateError(Exception):
    """Raised when attempting to modify a terminal state."""

    pass


class JobExistsError(Exception):
    """Raised when attempting to register a job that already exists."""

    pass
