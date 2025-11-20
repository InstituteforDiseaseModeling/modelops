# Azure Job Observability MVP - Implementation Plan

## Executive Summary

This document provides a staged implementation plan for the **Azure-only MVP** of job observability using optimistic concurrency control. We focus on delivering immediate value with the minimum loveable product that can be shipped and tested.

## MVP Scope (What We're Building)

### Core Features
1. **Job status tracking** without kubectl
2. **Progress monitoring** with task counts
3. **Error reporting** with details
4. **CLI commands** (`mops jobs status`, `list`, `events`)
5. **Non-blocking integration** - registry failures don't break job submission

### What We're NOT Building (Yet)
- GCS/AWS support
- Daily indices
- Compression (can add later)
- Multi-tenancy
- Cost tracking
- Prometheus metrics

## Staged Implementation Plan

### Stage 1: Core Storage Layer (Day 1-2)
**Goal**: Get the storage abstraction working with tests

#### 1.1 Create VersionedStore Protocol
```python
# src/modelops/services/storage/versioned.py
from typing import Protocol, Optional, Any
from dataclasses import dataclass

@dataclass
class VersionToken:
    """Opaque version identifier for CAS operations."""
    value: str  # ETag for Azure

class VersionedStore(Protocol):
    """Cloud-agnostic versioned storage with CAS semantics."""

    def get(self, key: str) -> Optional[tuple[bytes, VersionToken]]:
        """Get current value and version."""
        ...

    def put(self, key: str, value: bytes, version: VersionToken) -> bool:
        """Update if version matches (Compare-And-Swap)."""
        ...

    def create_if_absent(self, key: str, value: bytes) -> bool:
        """Create only if doesn't exist."""
        ...

    def list_keys(self, prefix: str) -> list[str]:
        """List keys with prefix."""
        ...
```

#### 1.2 Implement InMemoryVersionedStore
```python
# src/modelops/services/storage/memory.py
class InMemoryVersionedStore:
    """In-memory implementation for testing."""

    def __init__(self):
        self._data = {}
        self._versions = {}
        self._version_counter = 0
```

#### 1.3 Implement AzureVersionedStore
```python
# src/modelops/services/storage/azure_versioned.py
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, MatchConditions

class AzureVersionedStore:
    """Azure blob storage with ETags for CAS."""

    def __init__(self, connection_string: str, container: str):
        self.client = BlobServiceClient.from_connection_string(connection_string)
        self.container = container
        self._ensure_container()

    def get(self, key: str) -> Optional[tuple[bytes, VersionToken]]:
        try:
            blob = self.client.get_blob_client(self.container, key)
            props = blob.get_blob_properties()
            content = blob.download_blob().readall()
            return (content, VersionToken(props.etag))
        except ResourceNotFoundError:
            return None

    def put(self, key: str, value: bytes, version: VersionToken) -> bool:
        try:
            blob = self.client.get_blob_client(self.container, key)
            blob.upload_blob(
                value,
                overwrite=True,
                if_match=version.value,  # Correct parameter
                match_condition=MatchConditions.IfNotModified
            )
            return True
        except ResourceModifiedError:
            return False
```

#### 1.4 Add Retry Logic
```python
# src/modelops/services/storage/retry.py
import time
import random
import json

def update_with_retry(store: VersionedStore, key: str, update_fn, max_attempts=5):
    """CAS retry with exponential backoff and jitter."""

    for attempt in range(max_attempts):
        result = store.get(key)
        if result is None:
            raise KeyError(f"Key {key} not found")

        current_bytes, version = result
        current_value = json.loads(current_bytes.decode('utf-8'))
        new_value = update_fn(current_value)
        new_bytes = json.dumps(new_value).encode('utf-8')

        if store.put(key, new_bytes, version):
            return new_value

        if attempt < max_attempts - 1:
            delay = (2 ** attempt) * 0.1
            jitter = random.uniform(0, 0.1)
            time.sleep(delay + jitter)

    raise TooManyRetriesError(f"Failed after {max_attempts} attempts")
```

#### Tests for Stage 1
```python
# tests/test_versioned_store.py
def test_cas_semantics():
    """Test CAS works correctly."""
    store = InMemoryVersionedStore()

    # Create
    assert store.create_if_absent("key1", b'{"v": 1}')
    assert not store.create_if_absent("key1", b'{"v": 2}')

    # Update with CAS
    data, version = store.get("key1")
    assert store.put("key1", b'{"v": 2}', version)
    assert not store.put("key1", b'{"v": 3}', version)  # Stale
```

### Stage 2: Job State Management (Day 2-3)
**Goal**: Add business logic layer with state machine

#### 2.1 Create JobStatus Enum
```python
# src/modelops/services/job_state.py
from enum import Enum

class JobStatus(str, Enum):
    PENDING = "pending"
    SUBMITTING = "submitting"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

TRANSITIONS = {
    JobStatus.PENDING: {JobStatus.SUBMITTING, JobStatus.CANCELLED},
    JobStatus.SUBMITTING: {JobStatus.SCHEDULED, JobStatus.FAILED},
    JobStatus.SCHEDULED: {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},
    # Terminal states
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}
```

#### 2.2 Create JobState Dataclass
```python
# src/modelops/services/job_state.py
from dataclasses import dataclass
from typing import Optional

@dataclass
class JobState:
    job_id: str
    status: JobStatus
    created_at: str
    updated_at: str

    # K8s metadata
    k8s_name: Optional[str] = None
    k8s_namespace: Optional[str] = None

    # Progress
    tasks_total: int = 0
    tasks_completed: int = 0

    # Error info
    error_message: Optional[str] = None
    error_code: Optional[str] = None

    # Results
    results_path: Optional[str] = None
```

#### 2.3 Implement JobRegistry
```python
# src/modelops/services/job_registry.py
class JobRegistry:
    """Job state management with business logic."""

    def __init__(self, store: VersionedStore):
        self.store = store

    def register_job(self, job_id: str, k8s_name: str, namespace: str) -> None:
        """Register new job."""
        state = JobState(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=now_iso(),
            updated_at=now_iso(),
            k8s_name=k8s_name,
            k8s_namespace=namespace
        )

        state_bytes = json.dumps(asdict(state)).encode('utf-8')
        if not self.store.create_if_absent(f"jobs/{job_id}/state.json", state_bytes):
            raise JobExistsError(f"Job {job_id} already registered")

    def update_status(self, job_id: str, new_status: JobStatus, **kwargs) -> None:
        """Update job status with validation."""

        def update_fn(state_dict: dict) -> dict:
            current_status = JobStatus(state_dict['status'])

            # Validate transition
            if new_status not in TRANSITIONS.get(current_status, set()):
                raise InvalidTransitionError(
                    f"Cannot transition from {current_status} to {new_status}"
                )

            # Terminal state check
            if current_status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return state_dict  # No-op

            state_dict['status'] = new_status.value
            state_dict['updated_at'] = now_iso()

            # Update additional fields
            for key, value in kwargs.items():
                if key in state_dict:
                    state_dict[key] = value

            return state_dict

        key = f"jobs/{job_id}/state.json"
        update_with_retry(self.store, key, update_fn)
```

### Stage 3: CLI Integration (Day 3-4)
**Goal**: Add commands to existing CLI

#### 3.1 Add Status Command
```python
# src/modelops/cli/jobs.py (ADD to existing file)

@app.command()
def status(
    job_id: str = typer.Argument(..., help="Job ID to query"),
    watch: bool = typer.Option(False, "--watch", "-w", help="Watch for changes"),
    json_output: bool = typer.Option(False, "--json", help="JSON output")
):
    """Get status of a specific job."""
    registry = get_job_registry()  # Uses env for Azure connection

    if watch:
        while True:
            state = registry.get_job(job_id)
            if state:
                display_job_status(state, json_output)
                if state.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                    break
            time.sleep(2)
    else:
        state = registry.get_job(job_id)
        if not state:
            error(f"Job {job_id} not found")
            raise typer.Exit(1)
        display_job_status(state, json_output)
```

#### 3.2 Add List Command
```python
@app.command("list")
def list_jobs(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of jobs to show"),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status")
):
    """List recent jobs."""
    registry = get_job_registry()

    jobs = registry.list_jobs(limit=limit)
    if status:
        jobs = [j for j in jobs if j.status == status]

    # Display table
    table = Table(title="Recent Jobs")
    table.add_column("Job ID", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Progress")
    table.add_column("Created", style="dim")

    for job in jobs:
        progress = f"{job.tasks_completed}/{job.tasks_total}" if job.tasks_total > 0 else "-"
        table.add_row(
            job.job_id[:8],
            job.status,
            progress,
            job.created_at
        )

    console.print(table)
```

### Stage 4: Job Submission Integration (Day 4)
**Goal**: Integrate registry with existing job submission (non-blocking)

#### 4.1 Update JobSubmissionClient
```python
# src/modelops/client/job_submission.py
class JobSubmissionClient:
    def __init__(self, ...):
        # ... existing init ...
        self.registry = self._init_registry()  # Optional

    def _init_registry(self) -> Optional[JobRegistry]:
        """Initialize registry if Azure connection available."""
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if not conn_str:
            return None

        try:
            store = AzureVersionedStore(conn_str, "job-registry")
            return JobRegistry(store)
        except Exception as e:
            logger.warning(f"Registry unavailable: {e}")
            return None

    def submit_job(self, job_spec: dict, image: str) -> str:
        job_id = str(uuid.uuid4())

        # Try to register (non-blocking)
        if self.registry:
            try:
                self.registry.register_job(
                    job_id,
                    k8s_name=f"job-{job_id[:8]}",
                    namespace=self.namespace
                )
            except Exception as e:
                logger.warning(f"Registry registration failed: {e}")

        # Upload spec (existing flow - MUST NOT FAIL)
        spec_key = f"jobs/{job_id}/spec.json"
        self.storage.save(spec_key, job_spec)

        # Update status (optional)
        if self.registry:
            try:
                self.registry.update_status(job_id, JobStatus.SUBMITTING)
            except:
                pass

        # Create K8s job (existing flow)
        k8s_job = self._create_k8s_job(job_id, spec_key, image)

        # Update with K8s info (optional)
        if self.registry:
            try:
                self.registry.update_status(
                    job_id,
                    JobStatus.SCHEDULED,
                    k8s_name=k8s_job.metadata.name,
                    k8s_namespace=k8s_job.metadata.namespace
                )
            except:
                pass

        return job_id
```

### Stage 5: Job Runner Integration (Day 5)
**Goal**: Add lifecycle reporting from running jobs

#### 5.1 Update JobRunner
```python
# src/modelops/runners/job_runner.py
class JobRunner:
    def __init__(self, job_id: str, ...):
        self.job_id = job_id
        self.registry = self._init_registry()

    def run(self):
        """Main execution with status reporting."""

        # Mark as running
        self._update_status(JobStatus.RUNNING)

        try:
            # Download spec
            spec = self._download_spec()
            total_tasks = len(spec.get('simulations', []))

            # Execute simulations
            for i, sim in enumerate(spec['simulations']):
                self._execute_simulation(sim)

                # Update progress
                if self.registry:
                    try:
                        self.registry.update_progress(
                            self.job_id,
                            tasks_completed=i+1,
                            tasks_total=total_tasks
                        )
                    except:
                        pass

            # Mark success
            self._update_status(JobStatus.SUCCEEDED)

        except Exception as e:
            logger.error(f"Job failed: {e}")
            self._update_status(
                JobStatus.FAILED,
                error_message=str(e),
                error_code=type(e).__name__
            )
            raise

    def _update_status(self, status: JobStatus, **kwargs):
        """Update job status (non-blocking)."""
        if self.registry:
            try:
                self.registry.update_status(self.job_id, status, **kwargs)
            except Exception as e:
                logger.warning(f"Status update failed: {e}")
```

## Testing Strategy

### Unit Tests (Stage 1-2)
```bash
# Test storage layer
pytest tests/test_versioned_store.py

# Test state machine
pytest tests/test_job_registry.py
```

### Integration Tests (Stage 3-4)
```bash
# Test with real Azure storage (requires AZURE_STORAGE_CONNECTION_STRING)
pytest tests/integration/test_azure_registry.py

# Test job submission with registry
pytest tests/integration/test_job_submission.py
```

### End-to-End Test (Stage 5)
```bash
# Submit a real job and check status
mops jobs submit examples/test_job.yaml
mops jobs status <job-id> --watch
mops jobs list
```

## Environment Setup

```bash
# Required environment variable
export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=..."

# Optional: Create registry container
az storage container create --name job-registry --connection-string $AZURE_STORAGE_CONNECTION_STRING
```

## Success Criteria

1. ✅ Job submission still works if registry unavailable
2. ✅ Can check job status without kubectl
3. ✅ Can see progress (tasks completed/total)
4. ✅ Can list recent jobs
5. ✅ Registry operations < 100ms
6. ✅ No stuck states (version tokens are stateless)

## Next Steps (After MVP)

1. Add event logging (separate from state)
2. Add compression for events
3. Add daily indices for faster listing
4. Add heartbeat monitoring
5. Add GCS and AWS support
6. Add Prometheus metrics export

## Key Design Decisions for MVP

1. **No events initially** - Just state tracking, can add events later
2. **No compression** - Keep it simple, add when needed
3. **No separate heartbeat key** - Use main state, optimize later if conflicts
4. **Azure-only** - Prove it works, then add other clouds
5. **Non-blocking everywhere** - Registry must never break job submission

This MVP can be implemented in 5 days and provides immediate value for job observability.