"""Job state management with state machine validation.

Defines the job lifecycle states and valid transitions, along with
the data structure for tracking job state.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Set, Dict, Any
import json


class JobStatus(str, Enum):
    """Job lifecycle states.

    String enum for JSON serialization compatibility.
    Follows Kubernetes naming conventions.
    """

    # Initial states
    PENDING = "pending"          # Job created, not yet submitted to K8s
    SUBMITTING = "submitting"    # Being submitted to Kubernetes

    # Running states
    SCHEDULED = "scheduled"      # K8s Job created, waiting for pod
    RUNNING = "running"          # Pod running, executing tasks

    # Terminal states (no transitions out)
    SUCCEEDED = "succeeded"      # Completed successfully
    FAILED = "failed"           # Failed with error
    CANCELLED = "cancelled"     # User-cancelled or SIGTERM


# Legal state transitions to prevent invalid states
TRANSITIONS: Dict[JobStatus, Set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.SUBMITTING, JobStatus.CANCELLED},
    JobStatus.SUBMITTING: {JobStatus.SCHEDULED, JobStatus.FAILED},
    JobStatus.SCHEDULED: {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},

    # Terminal states - no outbound transitions
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


def is_terminal(status: JobStatus) -> bool:
    """Check if a status is terminal (no transitions out)."""
    return status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


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
    return datetime.now(timezone.utc).isoformat()


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
    k8s_name: Optional[str] = None
    k8s_namespace: Optional[str] = None
    k8s_uid: Optional[str] = None  # For correlation with K8s events

    # Progress tracking
    tasks_total: int = 0
    tasks_completed: int = 0

    # Error information
    error_message: Optional[str] = None
    error_code: Optional[str] = None

    # Results
    results_path: Optional[str] = None

    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        # Convert enum to string
        data['status'] = self.status.value if isinstance(self.status, JobStatus) else self.status
        return data

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> 'JobState':
        """Create from dictionary."""
        # Convert status string to enum
        if 'status' in data and isinstance(data['status'], str):
            data['status'] = JobStatus(data['status'])

        # Filter out unknown fields for backward compatibility
        # This allows old job records with extra fields to still be read
        from dataclasses import fields
        valid_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}

        return cls(**filtered_data)

    @classmethod
    def from_json(cls, json_str: str) -> 'JobState':
        """Deserialize from JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)

    def copy_with(self, **changes) -> 'JobState':
        """Create a copy with specified changes.

        Useful for updates while maintaining immutability.
        """
        data = self.to_dict()
        data.update(changes)
        return self.from_dict(data)

    @property
    def progress_percent(self) -> Optional[float]:
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