# Job Observability Implementation Plan v3.0

## Executive Summary

This document outlines a cloud-agnostic job observability system for ModelOps using optimistic concurrency control (OCC) and artifact-driven completion verification. The system validates job success by checking that all expected outputs exist in the ProvenanceStore, ensuring data integrity and enabling idempotent operations.

**IMPORTANT**: This is NOT a workflow engine. We are simply checking "did the outputs get written to storage?" - no complex orchestration, no task dependencies, no DAGs.

**Key Changes in v3.0**:
- **Artifact-driven completion**: Validate outputs exist in ProvenanceStore, not just K8s exit codes
- **New states**: VALIDATING (checking outputs) and PARTIAL_SUCCESS (some outputs missing)
- **ProvenanceStore integration**: Leverage existing storage for output validation
- **Idempotent execution**: Check for existing outputs before running tasks
- **Resume capability**: Re-run only missing tasks from partial jobs

**Key Changes in v2.1** (Implemented):
- Fixed Azure SDK parameter names (`if_match` instead of `etag`) ✅
- VersionedStore uses bytes, JSON handled at JobRegistry layer ✅
- Terminal state enrichment strategy ✅
- Added sync command to update job status from Kubernetes ✅

## Current State Assessment

### What Currently Exists

1. **Job Submission System** (`src/modelops/cli/jobs.py`)
   - `mops jobs submit` command for submitting simulation studies
   - JobSubmissionClient that uploads job specs to blob storage
   - Creates Kubernetes Jobs that download and execute the specs

2. **Job Runner** (`src/modelops/runners/job_runner.py`)
   - Runs inside K8s pods
   - Downloads job specs from blob storage
   - Executes SimJob or CalibrationJob types
   - No status reporting or lifecycle tracking

3. **Storage Backend** (`src/modelops/services/storage/azure.py`)
   - Basic blob storage operations (save, load, delete)
   - No concurrency control

### Problems with Initial Implementation

1. **Lease-Based Approach Failed**
   - Azure blob leases caused "LeaseIdMissing" errors
   - Leases can get stuck if holder crashes
   - Azure-specific, not portable to GCS/S3
   - Complex recovery logic with lease breaking

2. **Wrong CLI Structure**
   - Created `commands/job.py` instead of using existing `jobs.py`
   - Added to adaptive.py incorrectly

## Non-Goals (What This Is NOT)

To maintain focus and simplicity, this system explicitly does NOT:

1. **Workflow Orchestration**
   - NOT building a workflow engine (we have Dask for that)
   - NOT managing task dependencies or DAGs
   - NOT orchestrating complex pipelines
   - Just checking: "are the expected outputs in storage?"

2. **Complex State Management**
   - NOT implementing sagas or compensating transactions
   - NOT managing distributed transactions
   - NOT coordinating multi-job workflows
   - Just tracking: "what's the status of this single job?"

3. **Resource Management**
   - NOT scheduling compute resources
   - NOT managing queue priorities
   - NOT implementing backpressure or rate limiting
   - Kubernetes handles all resource scheduling

4. **Data Processing**
   - NOT transforming or validating output contents
   - NOT computing checksums (ProvenanceStore does that)
   - NOT aggregating results (that's Calabaria's job)
   - Just verifying: "does the file exist at the expected path?"

The goal is simple: **Know if a job's outputs made it to storage, and if not, which ones are missing.**

## Proposed Architecture: Optimistic Concurrency Control

### Why Optimistic Concurrency Over Pessimistic Locking?

**Problems with Pessimistic Locking (Leases)**:
1. **Stuck Leases**: Process crashes leave resources locked until timeout
2. **Cloud-Specific**: Azure leases ≠ GCS locks ≠ S3 (no native support)
3. **Complex Recovery**: Need lease breaking, timeout handling, cleanup daemons
4. **Performance**: Lock acquisition adds latency to every operation
5. **Debugging Hell**: "Why is this blob locked?" becomes common question

**Advantages of Optimistic Concurrency (CAS)**:
1. **No Stuck State**: Version tokens are stateless - no cleanup needed
2. **Cloud-Portable**: ETags (Azure), metageneration (GCS), conditional puts (DynamoDB)
3. **Simple Retry**: Just re-read and retry on version mismatch
4. **Better Performance**: No lock acquisition overhead
5. **Debuggable**: Version conflicts are explicit and loggable

### Layered Architecture

```
┌─────────────────────────────────────────┐
│            CLI Commands                  │  User Interface
│         (mops jobs status)               │
├─────────────────────────────────────────┤
│           JobRegistry                    │  Business Logic
│    (State Machine, Validation)           │  - Status transitions
│    (JSON encoding/decoding)              │  - Event aggregation
├─────────────────────────────────────────┤
│       VersionedStore[bytes]              │  Concurrency Control
│     (CAS Operations, Retries)            │  - get/put/create_if_absent
│                                          │  - Version token handling
├─────────────────────────────────────────┤
│         Storage Backend                  │  Cloud Provider
│    (Azure/GCS/DynamoDB)                  │  - Blob/DB operations
│                                          │  - Provider-specific APIs
└─────────────────────────────────────────┘
```

**Engineering Rationale for Layers**:
- **Separation of Concerns**: Each layer has single responsibility
- **Testability**: Can mock VersionedStore for JobRegistry tests
- **Portability**: Swap storage backend without changing business logic
- **Evolution**: Can add caching layer between JobRegistry and VersionedStore
- **JSON Isolation**: All JSON handling in JobRegistry, providers work with bytes

## Core Design: VersionedStore Abstraction

### Interface Design (Bytes-Based)

```python
from typing import Protocol, Optional, Any
from dataclasses import dataclass

@dataclass
class VersionToken:
    """Opaque version identifier for CAS operations."""
    value: Any  # ETag, metageneration, or DynamoDB version number

class VersionedStore(Protocol):
    """Cloud-agnostic versioned storage with CAS semantics.

    Works with bytes to avoid provider JSON quirks.
    JSON encoding/decoding happens in JobRegistry layer.
    """

    def get(self, key: str) -> Optional[tuple[bytes, VersionToken]]:
        """Get current value (as bytes) and version.

        Returns None if not exists, eliminating exists() race condition.
        Version token is required for subsequent updates.
        """
        ...

    def put(self, key: str, value: bytes, version: VersionToken) -> bool:
        """Update if version matches (Compare-And-Swap).

        Returns False on version mismatch (retry needed).
        Why not raise? Exceptions are for exceptional cases; conflicts are expected.
        """
        ...

    def create_if_absent(self, key: str, value: bytes) -> bool:
        """Create only if doesn't exist.

        Returns False if already exists.
        Atomic operation prevents duplicate job registration.
        """
        ...

    def list_keys(self, prefix: str) -> list[str]:
        """List keys with prefix for fallback when index unavailable."""
        ...
```

### Why This Interface?

1. **Minimal Surface**: Only 4 methods covers all use cases
2. **Race-Free**: No separate exists() check that could become stale
3. **Bytes-Based**: Avoids provider JSON quirks, enables compression
4. **Cloud-Agnostic**: Maps cleanly to all major providers
5. **Retry-Friendly**: Boolean returns make retry loops simple

### Cloud Provider Implementations

#### Azure Implementation (Corrected)
```python
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, MatchConditions

class AzureVersionedStore(VersionedStore):
    """Uses ETags for optimistic concurrency.

    ETags are HTTP standard, automatically updated by Azure on each write.
    Supported on all blob operations via if_match headers.
    """

    def __init__(self, connection_string: str, container: str):
        self.client = BlobServiceClient.from_connection_string(connection_string)
        self.container = container

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
                if_match=version.value,  # Correct parameter name
                match_condition=MatchConditions.IfNotModified,
                content_type="application/octet-stream"
            )
            return True
        except ResourceModifiedError:
            return False  # Version mismatch

    def create_if_absent(self, key: str, value: bytes) -> bool:
        try:
            blob = self.client.get_blob_client(self.container, key)
            blob.upload_blob(
                value,
                overwrite=False,  # Fail if exists
                content_type="application/octet-stream"
            )
            return True
        except ResourceExistsError:
            return False
```

**Why ETags?**
- Built into HTTP standard (RFC 7232)
- Automatically maintained by Azure
- Works with CDNs and caches
- Strong validator (changes on any modification)

#### GCS Implementation
```python
from google.cloud import storage
from google.api_core.exceptions import PreconditionFailed, NotFound

class GCSVersionedStore(VersionedStore):
    """Uses metageneration for optimistic concurrency.

    Metageneration is GCS's version number, increments on each change.
    Supported via if_metageneration_match preconditions.
    """

    def __init__(self, project: str, bucket_name: str):
        self.client = storage.Client(project=project)
        self.bucket = self.client.bucket(bucket_name)

    def get(self, key: str) -> Optional[tuple[bytes, VersionToken]]:
        blob = self.bucket.blob(key)
        try:
            content = blob.download_as_bytes()
            return (content, VersionToken(blob.metageneration))
        except NotFound:
            return None

    def put(self, key: str, value: bytes, version: VersionToken) -> bool:
        blob = self.bucket.blob(key)
        try:
            blob.upload_from_string(
                value,
                if_metageneration_match=version.value,
                content_type="application/octet-stream"
            )
            return True
        except PreconditionFailed:
            return False
```

**Why Metageneration?**
- Monotonically increasing (easier debugging)
- 64-bit integer (compact)
- Generation vs metageneration tracks content vs metadata changes

#### AWS Implementation (DynamoDB-Only for State)
```python
import boto3
from botocore.exceptions import ClientError

class DynamoVersionedStore(VersionedStore):
    """Uses DynamoDB exclusively for state storage with CAS.

    Why DynamoDB instead of S3? S3 lacks conditional writes for existing objects.
    DynamoDB provides strong consistency and atomic conditional expressions.
    S3 is used separately for large artifacts (events, results) but not state.
    """

    def __init__(self, table_name: str, region: str = 'us-east-1'):
        self.dynamo = boto3.resource('dynamodb', region_name=region)
        self.table = self.dynamo.Table(table_name)

    def get(self, key: str) -> Optional[tuple[bytes, VersionToken]]:
        try:
            response = self.table.get_item(Key={'id': key})
            if 'Item' in response:
                item = response['Item']
                return (
                    item['data'].value,  # Binary type in DynamoDB
                    VersionToken(item['version'])
                )
            return None
        except ClientError:
            return None

    def put(self, key: str, value: bytes, version: VersionToken) -> bool:
        """Atomic CAS update using DynamoDB conditional expressions."""
        try:
            self.table.update_item(
                Key={'id': key},
                UpdateExpression='SET #d = :data, #v = #v + :one',
                ConditionExpression='#v = :expected_version',
                ExpressionAttributeNames={
                    '#d': 'data',
                    '#v': 'version'
                },
                ExpressionAttributeValues={
                    ':data': value,
                    ':expected_version': version.value,
                    ':one': 1
                }
            )
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return False  # Version mismatch
            raise

    def create_if_absent(self, key: str, value: bytes) -> bool:
        """Atomic create using conditional expression."""
        try:
            self.table.put_item(
                Item={
                    'id': key,
                    'data': value,
                    'version': 1,
                    'created_at': datetime.utcnow().isoformat()
                },
                ConditionExpression='attribute_not_exists(id)'
            )
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return False  # Already exists
            raise
```

**Why DynamoDB for AWS?**
- S3 lacks conditional writes (except for new objects)
- DynamoDB has strong consistency and conditional expressions
- Single-digit millisecond latency
- Automatic scaling
- No orphaned objects from two-phase updates

### Retry Logic with Exponential Backoff and Jitter

```python
import time
import random

def update_with_retry(store: VersionedStore, key: str, update_fn, max_attempts=5):
    """Generic CAS retry loop with exponential backoff and jitter.

    Why exponential backoff? Prevents thundering herd on conflicts.
    Why jitter? Prevents synchronized retries from multiple workers.
    Why max_attempts? Prevents infinite loops on persistent conflicts.
    """

    for attempt in range(max_attempts):
        # Read current state
        result = store.get(key)
        if result is None:
            raise KeyError(f"Key {key} not found")

        current_bytes, version = result

        # Decode, update, encode (JSON handled here, not in store)
        current_value = json.loads(current_bytes.decode('utf-8'))
        new_value = update_fn(current_value)
        new_bytes = json.dumps(new_value).encode('utf-8')

        # Try to write back
        if store.put(key, new_bytes, version):
            return new_value

        # Conflict - backoff before retry
        if attempt < max_attempts - 1:
            delay = (2 ** attempt) * 0.1  # 0.1s, 0.2s, 0.4s, 0.8s, 1.6s
            jitter = random.uniform(0, 0.1)  # Up to 100ms jitter
            time.sleep(delay + jitter)

    raise TooManyRetriesError(f"Failed to update {key} after {max_attempts} attempts")
```

**Engineering Decisions**:
- **Exponential backoff**: Reduces contention under load
- **Jitter**: Prevents synchronized retries from multiple workers
- **Max attempts**: Fails fast on persistent conflicts
- **Read in loop**: Always work with latest state
- **JSON at boundary**: Store handles bytes, registry handles JSON

## Job State Management

### State Machine Design

```python
class JobStatus(str, Enum):
    """Job lifecycle states with clear transitions.

    Why str subclass? JSON serializable without custom encoder.
    Why uppercase? Follows K8s convention (Running, Succeeded, Failed).
    """

    # Initial states
    PENDING = "pending"          # Created but not submitted to K8s
    SUBMITTING = "submitting"    # Being submitted to Kubernetes

    # Running states
    SCHEDULED = "scheduled"      # K8s Job exists, waiting for pod
    RUNNING = "running"          # Pod running, executing tasks
    AGGREGATING = "aggregating"  # Computing target evaluation/loss

    # Terminal states (no transitions out)
    SUCCEEDED = "succeeded"      # Completed successfully
    FAILED = "failed"           # Failed with error
    CANCELLED = "cancelled"     # User-cancelled or SIGTERM
    TIMEOUT = "timeout"         # Heartbeat timeout

# Legal state transitions (prevents invalid states)
TRANSITIONS = {
    JobStatus.PENDING: {JobStatus.SUBMITTING, JobStatus.CANCELLED},
    JobStatus.SUBMITTING: {JobStatus.SCHEDULED, JobStatus.FAILED},
    JobStatus.SCHEDULED: {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.AGGREGATING, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMEOUT},
    JobStatus.AGGREGATING: {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},
    # Terminal states - no outbound transitions
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
    JobStatus.TIMEOUT: set(),
}

def validate_transition(from_status: JobStatus, to_status: JobStatus) -> bool:
    """Validate state machine transitions.

    Why validate? Catches bugs early, prevents invalid states.
    Why in registry not store? Business logic vs storage concern.
    """
    return to_status in TRANSITIONS.get(from_status, set())
```

### JobState Data Structure with Enrichment Support

```python
@dataclass
class JobState:
    """Immutable job state for storage.

    Why immutable? Prevents accidental mutations, easier reasoning.
    Why flat structure? Simpler JSON, better for queries.
    """
    job_id: str
    status: JobStatus
    created_at: str  # ISO 8601
    updated_at: str

    # Monotonic sequence for ordering (handles clock skew)
    event_seq: int = 0

    # Kubernetes metadata
    k8s_name: Optional[str] = None
    k8s_namespace: Optional[str] = None
    k8s_uid: Optional[str] = None  # For correlation

    # Observability metadata
    dask_scheduler_id: Optional[str] = None
    k8s_pod: Optional[str] = None
    worker_addr: Optional[str] = None

    # Progress tracking
    tasks_total: int = 0
    tasks_completed: int = 0

    # Health monitoring
    heartbeat_at: Optional[str] = None
    heartbeat_count: int = 0

    # Error information
    error_message: Optional[str] = None
    error_code: Optional[str] = None

    # Results and costs (enriched before terminal state)
    results_path: Optional[str] = None
    cost_seconds: Optional[float] = None
    cost_dollars: Optional[float] = None

    def to_json(self) -> str:
        """Serialize to JSON for storage."""
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, data: str) -> 'JobState':
        """Deserialize from JSON."""
        obj = json.loads(data)
        obj['status'] = JobStatus(obj['status'])
        return cls(**obj)
```

### Terminal State Enrichment Strategy

```python
class JobRegistry:
    """Handles terminal state enrichment properly."""

    def finalize_job(
        self,
        job_id: str,
        final_status: JobStatus,
        results_path: Optional[str] = None,
        cost_seconds: Optional[float] = None,
        error_info: Optional[dict] = None
    ) -> None:
        """Enrich job state THEN transition to terminal state atomically.

        Why atomic? Prevents partial updates leaving job in inconsistent state.
        Why enrich before terminal? Terminal states reject all updates.
        """

        assert final_status in {JobStatus.SUCCEEDED, JobStatus.FAILED,
                               JobStatus.CANCELLED, JobStatus.TIMEOUT}

        def update_fn(state_dict: dict) -> dict:
            current_status = JobStatus(state_dict['status'])

            # Validate we can transition to terminal
            if not validate_transition(current_status, final_status):
                raise InvalidTransitionError(
                    f"Cannot transition from {current_status} to {final_status}"
                )

            # Don't modify if already terminal
            if current_status in {JobStatus.SUCCEEDED, JobStatus.FAILED,
                                 JobStatus.CANCELLED, JobStatus.TIMEOUT}:
                return state_dict  # No-op

            # Enrich with all metadata BEFORE setting terminal status
            state_dict['updated_at'] = now_iso()
            state_dict['event_seq'] += 1

            if results_path:
                state_dict['results_path'] = results_path
            if cost_seconds is not None:
                state_dict['cost_seconds'] = cost_seconds
                # Estimate cost (example: $0.10 per compute hour)
                state_dict['cost_dollars'] = cost_seconds * 0.10 / 3600
            if error_info:
                state_dict['error_message'] = error_info.get('message')
                state_dict['error_code'] = error_info.get('code')

            # NOW transition to terminal state
            state_dict['status'] = final_status.value

            return state_dict

        # Single atomic update with enrichment + transition
        key = f"jobs/{job_id}/state.json"
        update_with_retry(self.store, key, update_fn)
```

## Simplified JobRegistry Implementation (v3.0)

For the MVP, we focus on state management and artifact validation without complex event logging (moved to future enhancements):

## JobRegistry Implementation

### Core Registry with State Validation

```python
class JobRegistry:
    """High-level job state management with business logic.

    Built on VersionedStore for portability.
    Enforces state machine, validates transitions.
    """

    def __init__(self, store: VersionedStore[JobState], events: EventLog):
        self.store = store
        self.events = events

    def register_job(
        self,
        job_id: str,
        k8s_name: str,
        namespace: str
    ) -> None:
        """Register new job atomically.

        Why create_if_absent? Prevents duplicate registration.
        Why not put? Would overwrite existing job.
        """

        state = JobState(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=now_iso(),
            updated_at=now_iso(),
            k8s_name=k8s_name,
            k8s_namespace=namespace
        )

        if not self.store.create_if_absent(f"jobs/{job_id}", state):
            raise JobExistsError(f"Job {job_id} already registered")

        # Log event (best effort, don't fail job registration)
        try:
            self.events.append_event(job_id, {
                "timestamp": now_iso(),
                "type": "registered",
                "status": "pending"
            })
        except Exception as e:
            logger.warning(f"Failed to log event: {e}")

    def update_status(
        self,
        job_id: str,
        new_status: JobStatus,
        **kwargs
    ) -> None:
        """Update job status with validation.

        Why validation? Prevents invalid state transitions.
        Why kwargs? Flexible metadata updates.
        """

        def update_fn(state: JobState) -> JobState:
            # Validate transition
            if not validate_transition(state.status, new_status):
                raise InvalidTransitionError(
                    f"Cannot transition from {state.status} to {new_status}"
                )

            # Never modify terminal states
            if state.status in {JobStatus.SUCCEEDED, JobStatus.FAILED,
                               JobStatus.CANCELLED, JobStatus.TIMEOUT}:
                raise TerminalStateError(
                    f"Cannot modify terminal state {state.status}"
                )

            # Update state
            state.status = new_status
            state.updated_at = now_iso()

            # Update additional fields
            for key, value in kwargs.items():
                if hasattr(state, key):
                    setattr(state, key, value)

            return state

        # Update with CAS retry
        key = f"jobs/{job_id}"
        update_with_retry(self.store, key, update_fn)

        # Log event
        self.events.append_event(job_id, {
            "timestamp": now_iso(),
            "type": "status_change",
            "status": new_status.value,
            "metadata": kwargs
        })

    def heartbeat(
        self,
        job_id: str,
        tasks_completed: Optional[int] = None
    ) -> None:
        """Update heartbeat timestamp and progress.

        Why separate from update_status? Different retry semantics.
        Heartbeats are optional, shouldn't fail the job.
        """

        def update_fn(state: JobState) -> JobState:
            state.heartbeat_at = now_iso()
            state.heartbeat_count += 1

            if tasks_completed is not None:
                state.tasks_completed = tasks_completed

            return state

        try:
            key = f"jobs/{job_id}"
            update_with_retry(self.store, key, update_fn, max_attempts=2)
        except Exception as e:
            # Don't fail job on heartbeat failure
            logger.warning(f"Heartbeat failed for {job_id}: {e}")
```

### Failure Detection

```python
class HealthMonitor:
    """Detect and handle stale jobs.

    Why separate class? Single responsibility, testability.
    Why not in JobRegistry? Different lifecycle, runs periodically.
    """

    HEARTBEAT_TIMEOUT = 120  # seconds

    def check_health(self, registry: JobRegistry) -> None:
        """Mark stale jobs as timeout.

        Why timeout not failed? Different failure mode, retriable.
        Why 120 seconds? Balance between detection speed and false positives.
        """

        # List all running/aggregating jobs
        active_jobs = registry.list_jobs(
            status_filter=[JobStatus.RUNNING, JobStatus.AGGREGATING]
        )

        now = datetime.now(timezone.utc)
        for job in active_jobs:
            if job.heartbeat_at:
                heartbeat_time = datetime.fromisoformat(job.heartbeat_at)
                elapsed = (now - heartbeat_time).total_seconds()

                if elapsed > self.HEARTBEAT_TIMEOUT:
                    logger.warning(
                        f"Job {job.job_id} heartbeat stale "
                        f"({elapsed:.0f}s), marking as timeout"
                    )

                    try:
                        registry.update_status(
                            job.job_id,
                            JobStatus.TIMEOUT,
                            error_message=f"No heartbeat for {elapsed:.0f}s"
                        )
                    except (InvalidTransitionError, TerminalStateError):
                        # Job already transitioned, race condition
                        pass
```

## Integration Points

### Job Submission Integration

```python
class JobSubmissionClient:
    """Enhanced client with optional registry integration.

    Registry failures don't block job submission.
    Graceful degradation to pre-registry behavior.
    """

    def submit_job(self, job_spec: dict, image: str) -> str:
        job_id = str(uuid.uuid4())

        # 1. Try to register (optional)
        if self.registry:
            try:
                self.registry.register_job(
                    job_id,
                    k8s_name=f"job-{job_id[:8]}",
                    namespace=self.namespace
                )
            except Exception as e:
                logger.warning(f"Registry registration failed: {e}")
                # Continue without registry

        # 2. Upload spec to blob (required)
        spec_key = f"jobs/{job_id}/spec.json"
        self.storage.save(spec_key, job_spec)

        # 3. Update status (optional)
        if self.registry:
            try:
                self.registry.update_status(job_id, JobStatus.SUBMITTING)
            except Exception:
                pass  # Non-fatal

        # 4. Create K8s Job (required)
        k8s_job = self.create_k8s_job(job_id, spec_key, image)

        # 5. Update with K8s info (optional)
        if self.registry:
            try:
                self.registry.update_status(
                    job_id,
                    JobStatus.SCHEDULED,
                    k8s_uid=k8s_job.metadata.uid
                )
            except Exception:
                pass

        return job_id
```

### Job Runner Integration

```python
class JobRunner:
    """Enhanced runner with lifecycle reporting.

    Reports progress without blocking execution.
    Handles registry failures gracefully.
    """

    def run(self) -> None:
        """Main execution loop with status reporting."""

        # 1. Mark as running
        self._update_status(JobStatus.RUNNING)

        # 2. Start heartbeat thread
        heartbeat_thread = self._start_heartbeat()

        try:
            # 3. Download and parse spec
            spec = self._download_spec()
            total_tasks = len(spec.simulations)

            # 4. Execute simulations
            for i, sim in enumerate(spec.simulations):
                self._execute_simulation(sim)

                # Update progress
                self._update_progress(i + 1, total_tasks)

            # 5. Aggregate results
            self._update_status(JobStatus.AGGREGATING)
            results = self._aggregate_results()

            # 6. Mark success
            self._update_status(
                JobStatus.SUCCEEDED,
                results_path=self._upload_results(results)
            )

        except Exception as e:
            logger.error(f"Job failed: {e}")
            self._update_status(
                JobStatus.FAILED,
                error_message=str(e),
                error_code=type(e).__name__
            )
            raise

        finally:
            # Stop heartbeat
            heartbeat_thread.stop()

    def _start_heartbeat(self) -> HeartbeatThread:
        """Start background heartbeat thread.

        Why thread not process? Shares memory, low overhead.
        Why daemon? Exits with main process.
        """

        thread = HeartbeatThread(
            registry=self.registry,
            job_id=self.job_id,
            interval=30  # seconds
        )
        thread.daemon = True
        thread.start()
        return thread
```

## CLI Commands

### Status Command

```python
@app.command()
def status(
    job_id: str,
    watch: bool = False,
    json_output: bool = False
):
    """Show job status with optional watch mode.

    Why watch mode? Avoid constant re-running.
    Why JSON? Machine-readable for automation.
    """

    registry = get_registry()  # Uses VersionedStore based on env

    if watch:
        while True:
            state = registry.get_job(job_id)
            display_status(state, json_output)

            if state.status in TERMINAL_STATES:
                break

            time.sleep(2)
    else:
        state = registry.get_job(job_id)
        display_status(state, json_output)
```

## Infrastructure Provisioning

### Pulumi ComponentResource

```python
class JobRegistryInfra(pulumi.ComponentResource):
    """Provisions storage for job registry.

    Why ComponentResource? Encapsulates related resources.
    Why separate from workspace? Different lifecycle.
    """

    def __init__(self, name: str, cloud_provider: str):
        super().__init__("modelops:JobRegistry", name)

        if cloud_provider == "azure":
            # Create storage account
            self.storage_account = storage.StorageAccount(
                f"{name}-storage",
                resource_group_name=resource_group.name,
                sku=storage.SkuArgs(name="Standard_LRS"),
                opts=pulumi.ResourceOptions(parent=self)
            )

            # Create container for registry
            self.container = storage.BlobContainer(
                f"{name}-registry",
                account_name=self.storage_account.name,
                resource_group_name=resource_group.name,
                opts=pulumi.ResourceOptions(parent=self)
            )

            # Export connection info
            pulumi.export("registry_connection",
                         self.storage_account.primary_connection_string)

        elif cloud_provider == "aws":
            # Create S3 bucket for data
            self.bucket = s3.Bucket(
                f"{name}-registry",
                versioning=s3.BucketVersioningArgs(enabled=True),
                opts=pulumi.ResourceOptions(parent=self)
            )

            # Create DynamoDB table for CAS
            self.table = dynamodb.Table(
                f"{name}-registry-index",
                billing_mode="PAY_PER_REQUEST",
                attributes=[
                    dynamodb.TableAttributeArgs(
                        name="id",
                        type="S"
                    )
                ],
                hash_key="id",
                opts=pulumi.ResourceOptions(parent=self)
            )

            # Export connection info
            pulumi.export("registry_bucket", self.bucket.id)
            pulumi.export("registry_table", self.table.name)
```

## Implementation Status (October 2024)

### ✅ COMPLETED (v2.1 - Basic Observability)

1. **Storage Layer**:
   - VersionedStore protocol with bytes interface
   - AzureVersionedStore using ETags for CAS
   - InMemoryVersionedStore for testing
   - Retry logic with exponential backoff

2. **Job State Management**:
   - JobStatus enum with state transitions
   - JobState dataclass with backward compatibility
   - JobRegistry with business logic enforcement
   - State machine validation

3. **CLI Commands**:
   - `mops jobs submit` - Integrated with JobRegistry
   - `mops jobs status <job-id>` - Check individual job
   - `mops jobs list` - List recent jobs with filtering
   - `mops jobs sync` - Update status from Kubernetes

4. **Testing**:
   - 21 unit tests for JobRegistry
   - 14 unit tests for VersionedStore
   - 3 integration tests

### 🚧 IN PROGRESS (v3.0 - Artifact-Driven Completion)

1. **Extended State Machine**:
   - [ ] Add VALIDATING state
   - [ ] Add PARTIAL_SUCCESS terminal state
   - [ ] Update state transitions

2. **ProvenanceStore Integration**:
   - [ ] Add expected_outputs manifest to JobState
   - [ ] Implement validate_outputs() in JobRegistry
   - [ ] Check outputs using ProvenanceStore.get_sim()

3. **CLI Enhancements**:
   - [ ] Add `mops jobs resume <job-id>` command
   - [ ] Add `mops jobs validate <job-id>` command
   - [ ] Enhance sync with output validation

### 📋 TODO (Future Enhancements)

1. **Idempotent Execution**:
   - [ ] Check existing outputs before task execution
   - [ ] Skip completed tasks automatically
   - [ ] IdempotentSimulationService wrapper

2. **Job Runner Integration**:
   - [ ] Add validation step after K8s completion
   - [ ] Report status to registry from runner
   - [ ] Handle partial completion gracefully

3. **Advanced Features**:
   - [ ] Event streaming with NDJSON chunks
   - [ ] AWS/GCS provider implementations
   - [ ] Metrics export for monitoring
   - [ ] Web dashboard for visualization

## Artifact-Driven Completion Design

### Motivation

**Problem**: Kubernetes exit codes don't guarantee data completeness
- K8s job can succeed but outputs may be missing
- Network failures during output upload
- Partial writes or corrupted data
- No visibility into what outputs exist

**Solution**: Validate outputs exist in ProvenanceStore
- Check actual data presence, not just exit codes
- Enable partial job resumption
- Provide idempotent execution
- Clear visibility into completion status

### Output Manifest Generation with Real Paths

When a job is submitted, generate expected outputs from the job specification:

```python
@dataclass
class OutputSpec:
    """Specification for an expected output."""
    param_id: str           # Parameter set ID
    seed: int              # Simulation seed
    output_type: str       # "simulation" or "aggregation"
    bundle_digest: str     # For path generation
    replicate_count: int   # Number of replicates
    provenance_path: str   # Expected path in ProvenanceStore

def generate_output_manifest(job: SimJob) -> List[OutputSpec]:
    """Generate expected outputs from job specification.

    Example: For a job with 2 parameter sets and 3 replicates each:
    - 6 simulation outputs (2 params × 3 seeds)
    - 2 aggregation outputs (1 per param set)

    Paths follow ProvenanceStore schema:
    - Sim: token/v1/sims/{bundle_hash[:12]}/{param_shard}/params_{param[:8]}/seed_{seed}/
    - Agg: token/v1/aggs/{bundle_hash[:12]}/target_{target}/agg_{aggregation_id}/
    """
    outputs = []

    # Group tasks by param_id to identify replicates
    tasks_by_param = {}
    for task in job.tasks:
        param_id = task.params.param_id
        if param_id not in tasks_by_param:
            tasks_by_param[param_id] = []
        tasks_by_param[param_id].append(task)

    for param_id, replicate_tasks in tasks_by_param.items():
        bundle_digest = hashlib.blake2b(
            job.bundle_ref.encode(), digest_size=32
        ).hexdigest()

        # Add simulation outputs
        for task in replicate_tasks:
            # Example path: token/v1/sims/a3f2b8c9d1e5/4d/2c/params_7fab3c21/seed_42/
            path_context = {
                "bundle_digest": bundle_digest,
                "param_id": param_id,
                "seed": task.seed
            }

            expected_path = provenance_schema.sim_path(**path_context)
            # Real example: "token/v1/sims/a3f2b8c9d1e5/4d/2c/params_7fab3c21/seed_42/"

            outputs.append(OutputSpec(
                param_id=param_id,
                seed=task.seed,
                output_type="simulation",
                bundle_digest=bundle_digest,
                replicate_count=1,
                provenance_path=expected_path
            ))

        # Add aggregation output if multiple replicates
        if len(replicate_tasks) > 1 and job.target_spec:
            # Example path: token/v1/aggs/a3f2b8c9d1e5/target_prevalence/agg_8d3f2a1b/
            agg_id = hashlib.blake2b(
                f"{param_id}-{len(replicate_tasks)}".encode(),
                digest_size=8
            ).hexdigest()

            agg_path = f"token/v1/aggs/{bundle_digest[:12]}/target_prevalence/agg_{agg_id}/"

            outputs.append(OutputSpec(
                param_id=param_id,
                seed=-1,  # Aggregations don't have seeds
                output_type="aggregation",
                bundle_digest=bundle_digest,
                replicate_count=len(replicate_tasks),
                provenance_path=agg_path
            ))

    return outputs
```

### Validation Process

After K8s job completes, validate outputs:

```python
def validate_outputs(self, job_id: str) -> ValidationResult:
    """Check if all expected outputs exist in ProvenanceStore."""

    job_state = self.get_job(job_id)
    if not job_state:
        return ValidationResult(status="not_found")

    verified = []
    missing = []

    for output_spec in job_state.expected_outputs:
        # Reconstruct task for ProvenanceStore lookup
        task = SimTask(
            bundle_ref=job_state.metadata.get("bundle_ref"),
            entrypoint="",  # Not needed for lookup
            params=UniqueParameterSet(
                param_id=output_spec.param_id,
                params={}  # Not needed for lookup
            ),
            seed=output_spec.seed,
            outputs=None
        )

        # Check if output exists
        result = self.provenance.get_sim(task)

        if result is not None:
            verified.append(output_spec.provenance_path)
        else:
            missing.append(output_spec.provenance_path)

    # Determine validation status
    if not missing:
        return ValidationResult(
            status="complete",
            verified_count=len(verified),
            missing_count=0,
            verified_outputs=verified
        )
    elif verified:
        return ValidationResult(
            status="partial",
            verified_count=len(verified),
            missing_count=len(missing),
            verified_outputs=verified,
            missing_outputs=missing
        )
    else:
        return ValidationResult(
            status="failed",
            verified_count=0,
            missing_count=len(missing),
            missing_outputs=missing
        )
```

### State Transition Flow

```
K8s Job Running
      ↓
K8s Job Completes (exit 0)
      ↓
Transition to VALIDATING
      ↓
Check ProvenanceStore for all expected outputs
      ↓
All present? → SUCCEEDED
Some missing? → PARTIAL_SUCCESS (can resume)
None present? → FAILED
Validation error? → Stay in VALIDATING (retry later)
```

### Resume Capability

For PARTIAL_SUCCESS jobs, enable targeted retry:

```python
@app.command()
def resume(
    job_id: str,
    env: Optional[str] = None,
    dry_run: bool = False
):
    """Resume a partially completed job."""

    registry = _get_registry(env)
    job_state = registry.get_job(job_id)

    if job_state.status != JobStatus.PARTIAL_SUCCESS:
        error(f"Job is {job_state.status.value}, not resumable")
        return

    # Get tasks for missing outputs only
    resumable_tasks = registry.get_resumable_tasks(job_id)

    if dry_run:
        info(f"Would retry {len(resumable_tasks)} tasks")
        return

    # Create new job with only missing tasks
    resume_job = SimJob(
        job_id=f"{job_id}-resume-{uuid.uuid4().hex[:4]}",
        bundle_ref=job_state.metadata.get("bundle_ref"),
        tasks=resumable_tasks,
        metadata={
            "resumed_from": job_id,
            "resume_attempt": job_state.metadata.get("resume_attempts", 0) + 1
        }
    )

    # Submit the resume job
    client = JobSubmissionClient(env=env)
    new_job_id = client.submit_job(resume_job)

    success(f"Resume job submitted: {new_job_id}")
```

### Idempotency Support

Check for existing outputs before execution:

```python
class IdempotentSimulationService:
    """Wrapper that checks ProvenanceStore before execution."""

    def submit(self, task: SimTask) -> FutureLike:
        # Check if output already exists
        existing_result = self.provenance.get_sim(task)

        if existing_result:
            # Return completed future with cached result
            logger.info(f"Task {task.params.param_id} already complete, skipping")
            return CompletedFuture(existing_result)

        # Submit for execution
        return self.sim_service.submit(task)
```

### Edge Cases

1. **ProvenanceStore Unavailable During Validation**:
   - Stay in VALIDATING state
   - Retry with exponential backoff
   - Manual override option via CLI

2. **Outputs Appear After Initial Validation**:
   - Periodic re-validation of PARTIAL_SUCCESS jobs
   - Manual `mops jobs validate --force`
   - Transition to SUCCEEDED when complete

3. **Corrupt or Incomplete Outputs**:
   - ProvenanceStore checksums detect corruption
   - Treat corrupt outputs as missing
   - Resume regenerates them

4. **Race Condition in Validation**:
   - CAS ensures atomic state updates
   - Last validation wins
   - Track validation attempts

## Failure Modes and Recovery Strategies

### Comprehensive Failure Analysis

| Failure Mode | Detection | Recovery Strategy | User Action |
|-------------|-----------|-------------------|-------------|
| **K8s job succeeds, no outputs** | Validation finds 0 outputs | Mark as FAILED | Investigate logs, resubmit |
| **K8s job succeeds, partial outputs** | Some outputs missing | Mark as PARTIAL_SUCCESS | Run `mops jobs resume` |
| **ProvenanceStore down during validation** | Connection error | Stay in VALIDATING | Wait for retry or `validate --force` |
| **ProvenanceStore schema change** | Path mismatch | Validation error logged | Update manifest generation |
| **Corrupt output file** | Checksum mismatch | Treat as missing | Resume to regenerate |
| **Output written after validation** | Manual check finds it | Re-validate | Run `mops jobs validate` |
| **K8s job deleted** | 404 from K8s API | Keep registry state | Check outputs manually |
| **Registry unavailable** | Connection error | Job runs anyway | Use kubectl fallback |
| **Duplicate job submission** | create_if_absent fails | Return existing job ID | Check existing status |
| **Network partition during write** | Partial write | ProvenanceStore atomic | Output missing, resume |

### Recovery Patterns

#### Pattern 1: Automatic Retry with Backoff
```python
def validate_with_retry(job_id: str, max_attempts: int = 3):
    """Retry validation on transient failures."""
    for attempt in range(max_attempts):
        try:
            return validate_outputs(job_id)
        except ProvenanceStoreUnavailable:
            if attempt < max_attempts - 1:
                delay = 2 ** attempt * 10  # 10s, 20s, 40s
                logger.info(f"ProvenanceStore unavailable, retry in {delay}s")
                time.sleep(delay)
            else:
                # Stay in VALIDATING for manual intervention
                return ValidationResult(
                    status="unavailable",
                    error="ProvenanceStore unavailable after retries"
                )
```

#### Pattern 2: Idempotent Job Submission
```python
def submit_job_idempotent(job_spec: SimJob) -> str:
    """Submit job only if outputs don't already exist."""

    # Generate expected outputs
    manifest = generate_output_manifest(job_spec)

    # Check if all outputs already exist
    all_exist = True
    for output_spec in manifest:
        task = reconstruct_task(output_spec)
        if not provenance_store.get_sim(task):
            all_exist = False
            break

    if all_exist:
        logger.info("All outputs already exist, skipping job submission")
        return f"cached-{job_spec.job_id}"

    # Submit normally
    return submit_job(job_spec)
```

#### Pattern 3: Partial Recovery
```python
def create_resume_job(original_job_id: str) -> SimJob:
    """Create job with only missing tasks."""

    job_state = registry.get_job(original_job_id)
    if job_state.status != JobStatus.PARTIAL_SUCCESS:
        raise ValueError(f"Cannot resume {job_state.status} job")

    # Identify missing tasks from paths
    missing_tasks = []
    for missing_path in job_state.missing_outputs:
        # Parse path to extract param_id and seed
        # Example: token/v1/sims/a3f2/4d/2c/params_7fab3c21/seed_42/
        match = re.match(
            r".*/params_([^/]+)/seed_(\d+)/",
            missing_path
        )
        if match:
            param_id = match.group(1)
            seed = int(match.group(2))

            # Reconstruct task (would need original params)
            task = SimTask(
                bundle_ref=job_state.metadata['bundle_ref'],
                entrypoint=job_state.metadata['entrypoint'],
                params=UniqueParameterSet(
                    param_id=param_id,
                    params={}  # Need to retrieve from original
                ),
                seed=seed,
                outputs=None
            )
            missing_tasks.append(task)

    return SimJob(
        job_id=f"{original_job_id}-resume",
        bundle_ref=job_state.metadata['bundle_ref'],
        tasks=missing_tasks,
        metadata={
            "resumed_from": original_job_id,
            "original_total": job_state.tasks_total,
            "resuming_count": len(missing_tasks)
        }
    )
```

### ProvenanceStore Schema Evolution

To handle schema changes gracefully:

1. **Version the Schema**: Include version in paths (already done: `token/v1/...`)
2. **Migration Strategy**: Support reading old versions during transition
3. **Dual Write Period**: Write to both old and new paths temporarily
4. **Validation Flexibility**: Try multiple path patterns if needed

```python
def find_output_any_version(task: SimTask) -> Optional[SimReturn]:
    """Try multiple schema versions to find output."""

    # Try current version
    result = provenance_store.get_sim(task)
    if result:
        return result

    # Try legacy path format
    legacy_path = generate_legacy_path(task)
    if storage.exists(legacy_path):
        return load_legacy_output(legacy_path)

    return None
```

## Testing Strategy

### Unit Tests for VersionedStore

```python
@pytest.mark.parametrize("store_class", [
    AzureVersionedStore,
    GCSVersionedStore,
    AWSVersionedStore
])
def test_cas_semantics(store_class, tmp_path):
    """Test CAS behavior is consistent across providers.

    Why parametrize? Same tests for all implementations.
    Why tmp_path? Isolated test environment.
    """

    store = store_class(tmp_path)

    # Test create_if_absent
    assert store.create_if_absent("key1", {"value": 1})
    assert not store.create_if_absent("key1", {"value": 2})

    # Test CAS update
    data, version = store.get("key1")
    assert data == {"value": 1}

    # Concurrent update simulation
    assert store.put("key1", {"value": 2}, version)
    assert not store.put("key1", {"value": 3}, version)  # Stale version

    # Get fresh version and retry
    data, new_version = store.get("key1")
    assert data == {"value": 2}
    assert store.put("key1", {"value": 3}, new_version)
```

### Integration Tests for JobRegistry

```python
def test_concurrent_status_updates():
    """Test registry handles concurrent updates correctly.

    Why threads? Simulates real concurrent access.
    Why barriers? Ensures true concurrency.
    """

    store = InMemoryVersionedStore()  # Test implementation
    registry = JobRegistry(store, EventLog(store))

    # Register job
    registry.register_job("job1", "k8s-job1", "default")

    # Concurrent update attempts
    barrier = threading.Barrier(3)
    results = []

    def update_task(new_status):
        barrier.wait()  # Synchronize start
        try:
            registry.update_status("job1", new_status)
            results.append(("success", new_status))
        except InvalidTransitionError as e:
            results.append(("invalid", str(e)))
        except TooManyRetriesError as e:
            results.append(("retry_exhausted", str(e)))

    # Try concurrent transitions
    threads = [
        Thread(target=update_task, args=(JobStatus.SUBMITTING,)),
        Thread(target=update_task, args=(JobStatus.SUBMITTING,)),
        Thread(target=update_task, args=(JobStatus.CANCELLED,))
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Verify exactly one succeeded
    successes = [r for r in results if r[0] == "success"]
    assert len(successes) == 1

    # Verify final state is consistent
    job = registry.get_job("job1")
    assert job.status == successes[0][1]
```

## Performance Considerations

### Operation Latencies

| Operation | Azure (ETag) | GCS (Metageneration) | AWS (DynamoDB) |
|-----------|-------------|---------------------|----------------|
| get() | ~20ms | ~30ms | ~10ms |
| put() with CAS | ~30ms | ~40ms | ~15ms |
| create_if_absent() | ~25ms | ~35ms | ~12ms |
| List 1000 jobs | ~200ms | ~300ms | ~100ms |

**Optimization Strategies**:
1. **Batch reads**: Get multiple jobs in single operation
2. **Caching**: Local cache with TTL for read-heavy workloads
3. **Async operations**: Don't block on registry updates
4. **Connection pooling**: Reuse HTTP connections

## Migration and Rollout

### Phase 1: VersionedStore Implementation
1. Implement VersionedStore for Azure (current provider)
2. Add comprehensive tests
3. Deploy alongside existing system (no integration yet)

### Phase 2: JobRegistry on VersionedStore
1. Rewrite JobRegistry to use VersionedStore
2. Remove all lease-based code
3. Test with synthetic workloads

### Phase 3: Integration
1. Update JobSubmissionClient to use new registry
2. Update JobRunner with lifecycle reporting
3. Add CLI commands to existing jobs.py

### Phase 4: Multi-Cloud
1. Implement GCS VersionedStore
2. Implement AWS VersionedStore with DynamoDB
3. Add provider selection logic

### Rollback Plan
- Registry is optional - can disable via environment variable
- Old jobs continue working without registry
- Can read old state format for migration

## Success Metrics

1. **Zero Regressions**: Job submission works without registry
2. **Conflict Resolution**: <5% of updates require retry
3. **Performance**: Registry operations <100ms p99
4. **Reliability**: No stuck states, automatic recovery
5. **Portability**: Same code works on Azure/GCS/AWS

## Engineering Trade-offs and Decisions

### Why Not Use Distributed Coordination Service?

**Options Considered**:
- etcd: Requires separate cluster, operational overhead
- Consul: Similar complexity, another system to manage
- Zookeeper: JVM dependency, complex operations

**Decision**: Blob storage with CAS because:
- Already have blob storage for job specs
- No new infrastructure required
- Managed service (no operations)
- Sufficient for our consistency needs

### Why Not Event Sourcing?

**Event Sourcing**: Store only events, compute state from events

**Decision**: Hybrid approach (state + events) because:
- Fast status queries (don't replay events)
- Bounded state size
- Events for audit/debugging
- Simpler implementation

### Why Not Database?

**Options**: PostgreSQL, DynamoDB global tables, CosmosDB

**Decision**: Blob storage because:
- No schema migrations
- Cheaper at scale
- Works offline/local with file system
- One less service to provision/manage

## Future Enhancements

### Near-term (Next Sprint)
1. **Prometheus Metrics**: Export job states as metrics
2. **Batch Operations**: Get/update multiple jobs atomically
3. **Query Index**: Secondary indices for filtering

### Medium-term (Next Quarter)
1. **Event Log System**: Chunked NDJSON event storage with compression
   - Immutable audit trail of all state changes
   - Separate from state for performance
   - Support for event replay and debugging
2. **WebSocket Streaming**: Real-time status updates
3. **Cost Attribution**: Track compute costs per job
4. **SLA Monitoring**: Alert on stuck jobs

### Long-term (Next Year)
1. **Multi-Region**: Geo-replicated registries
2. **Federated Queries**: Query across environments
3. **ML Predictions**: Predict job completion times
4. **Workflow Engine Integration**: If needed, integrate with Airflow/Prefect
5. **Complex State Orchestration**: Multi-job dependencies and coordination

## Appendix: Complete Interface Definitions

```python
# Complete VersionedStore protocol
from typing import Protocol, Optional, TypeVar, Generic
from dataclasses import dataclass

T = TypeVar('T')

@dataclass
class VersionToken:
    """Opaque version identifier."""
    value: Any

class VersionedStore(Protocol[T]):
    """Cloud-agnostic versioned storage."""

    def get(self, key: str) -> Optional[tuple[T, VersionToken]]:
        """Get value and version."""
        ...

    def put(self, key: str, value: T, version: VersionToken) -> bool:
        """Update if version matches."""
        ...

    def create_if_absent(self, key: str, value: T) -> bool:
        """Create if not exists."""
        ...

    def delete(self, key: str, version: VersionToken) -> bool:
        """Delete if version matches."""
        ...

    def list_keys(self, prefix: str) -> list[str]:
        """List keys with prefix."""
        ...

# Factory for creating appropriate store
def create_versioned_store(provider: str, config: dict) -> VersionedStore:
    """Factory to create provider-specific store.

    Args:
        provider: One of 'azure', 'gcs', 'aws', 'memory'
        config: Provider-specific configuration

    Returns:
        Configured VersionedStore implementation
    """

    if provider == 'azure':
        return AzureVersionedStore(
            connection_string=config['connection_string'],
            container=config['container']
        )
    elif provider == 'gcs':
        return GCSVersionedStore(
            project=config['project'],
            bucket=config['bucket']
        )
    elif provider == 'aws':
        return AWSVersionedStore(
            s3_bucket=config['s3_bucket'],
            dynamo_table=config['dynamo_table']
        )
    elif provider == 'memory':
        return InMemoryVersionedStore()  # For testing
    else:
        raise ValueError(f"Unknown provider: {provider}")
```

## Conclusion

This design replaces problematic lease-based concurrency with proven optimistic concurrency control patterns. The VersionedStore abstraction provides cloud portability while maintaining strong consistency guarantees. The layered architecture separates concerns and enables independent testing and evolution of components.

Key benefits:
- **No stuck leases**: Version tokens are stateless
- **Cloud portable**: Works on Azure, GCS, and AWS
- **Simple retries**: Clear conflict resolution
- **Operational simplicity**: No cleanup daemons needed
- **Performance**: No lock acquisition overhead

The implementation can be rolled out incrementally with zero impact on existing job submission flows.
