# Artifact-Driven Completion Implementation Plan

## Overview
Implement v3.0 of the Job Observability system to validate job completion by checking that all expected outputs exist in ProvenanceStore, rather than trusting Kubernetes exit codes.

**Key Principle**: This is NOT a workflow engine. We're simply checking "did the outputs get written to storage?"

## Current State (v2.1 Completed)
- ✅ VersionedStore with CAS (Azure implementation)
- ✅ JobRegistry with state management
- ✅ Basic JobStatus states (PENDING, RUNNING, SUCCEEDED, FAILED, etc.)
- ✅ CLI commands: submit, status, list, sync
- ✅ Integration with JobSubmissionClient

## Implementation Phases

### Phase 1: Extended State Machine (2 hours)
**Goal**: Add VALIDATING and PARTIAL_SUCCESS states to support artifact validation flow.

#### 1.1 Update JobStatus Enum
**File**: `src/modelops/services/job_state.py`
```python
# Add new states
VALIDATING = "validating"       # K8s complete, checking outputs
PARTIAL_SUCCESS = "partial"     # Some outputs missing

# Update TRANSITIONS dict
JobStatus.RUNNING: {JobStatus.VALIDATING, JobStatus.FAILED, JobStatus.CANCELLED},
JobStatus.VALIDATING: {JobStatus.SUCCEEDED, JobStatus.PARTIAL_SUCCESS, JobStatus.FAILED},

# Update terminal states
JobStatus.PARTIAL_SUCCESS: set(),

# Update is_terminal function
return status in {JobStatus.SUCCEEDED, JobStatus.PARTIAL_SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED}
```

#### 1.2 Add Output Manifest to JobState
**File**: `src/modelops/services/job_state.py`
```python
# Add fields to JobState dataclass
expected_outputs: List[Dict] = field(default_factory=list)  # OutputSpec dicts
verified_outputs: List[str] = field(default_factory=list)
missing_outputs: List[str] = field(default_factory=list)
tasks_verified: int = 0
validation_started_at: Optional[str] = None
validation_completed_at: Optional[str] = None
validation_attempts: int = 0
last_validation_error: Optional[str] = None
```

### Phase 2: Output Manifest Generation (3 hours)
**Goal**: Generate expected output paths when job is submitted.

#### 2.1 Create OutputSpec Classes
**New File**: `src/modelops/services/output_manifest.py`
```python
from dataclasses import dataclass
from typing import List, Dict, Any
import hashlib
from modelops_contracts import SimJob, SimTask

@dataclass
class OutputSpec:
    """Specification for an expected output."""
    param_id: str
    seed: int
    output_type: str  # "simulation" or "aggregation"
    bundle_digest: str
    replicate_count: int
    provenance_path: str

def generate_output_manifest(job: SimJob, provenance_schema) -> List[OutputSpec]:
    """Generate expected outputs from job specification."""
    # Implementation as per plan
```

#### 2.2 Integrate with Job Registration
**File**: `src/modelops/services/job_registry.py`
```python
def register_job(self, job_id: str, job_spec: SimJob, k8s_name: str, namespace: str):
    # Generate manifest
    from .output_manifest import generate_output_manifest
    expected_outputs = generate_output_manifest(job_spec, self.provenance_schema)

    # Add to JobState
    state = JobState(
        job_id=job_id,
        expected_outputs=[asdict(o) for o in expected_outputs],
        tasks_total=len(expected_outputs),
        # ... rest of fields
    )
```

### Phase 3: ProvenanceStore Integration (4 hours)
**Goal**: Add validation logic that checks ProvenanceStore for expected outputs.

#### 3.1 Add Validation Method to JobRegistry
**File**: `src/modelops/services/job_registry.py`
```python
from ..services.provenance_store import ProvenanceStore
from modelops_contracts import SimTask, UniqueParameterSet

def __init__(self, store: VersionedStore, provenance_store: Optional[ProvenanceStore] = None):
    self.store = store
    self.provenance = provenance_store

def validate_outputs(self, job_id: str) -> ValidationResult:
    """Check if all expected outputs exist in ProvenanceStore."""
    # Implementation as per plan

def transition_to_validating(self, job_id: str) -> None:
    """Transition job to VALIDATING state when K8s completes."""
    # Implementation

def finalize_with_validation(self, job_id: str, validation_result: ValidationResult) -> None:
    """Finalize job based on validation results."""
    # Implementation
```

#### 3.2 Create ValidationResult Class
**File**: `src/modelops/services/job_registry.py`
```python
@dataclass
class ValidationResult:
    status: str  # "complete", "partial", "failed", "unavailable"
    verified_count: int = 0
    missing_count: int = 0
    verified_outputs: Optional[List[str]] = None
    missing_outputs: Optional[List[str]] = None
    error: Optional[str] = None
```

### Phase 4: Update Sync Command (2 hours)
**Goal**: Enhance sync command to validate outputs when K8s job completes.

#### 4.1 Update Sync Command
**File**: `src/modelops/cli/jobs.py`
```python
@app.command()
def sync(env: Optional[str] = env_option(),
         validate: bool = typer.Option(True, help="Validate outputs after sync"),
         dry_run: bool = typer.Option(False)):
    # When K8s job succeeded
    if k8s_job.status.succeeded > 0:
        if job_state.status == JobStatus.RUNNING:
            # Transition to VALIDATING
            registry.transition_to_validating(job_state.job_id)

            if validate:
                # Validate outputs
                validation_result = registry.validate_outputs(job_state.job_id)
                registry.finalize_with_validation(job_state.job_id, validation_result)
```

### Phase 5: Add Resume Command (3 hours)
**Goal**: Enable resuming partial jobs by resubmitting only missing tasks.

#### 5.1 Add Resume Command
**File**: `src/modelops/cli/jobs.py`
```python
@app.command()
def resume(
    job_id: str = typer.Argument(..., help="Job ID to resume"),
    env: Optional[str] = env_option(),
    bundle: Optional[str] = typer.Option(None, help="Override bundle reference"),
    dry_run: bool = typer.Option(False)
):
    """Resume a partially completed job."""
    # Implementation as per plan
```

#### 5.2 Add Get Resumable Tasks Method
**File**: `src/modelops/services/job_registry.py`
```python
def get_resumable_tasks(self, job_id: str) -> List[SimTask]:
    """Get list of tasks that need to be re-run."""
    # Parse missing_outputs to reconstruct tasks
```

### Phase 6: Add Validate Command (2 hours)
**Goal**: Allow manual validation/re-validation of job outputs.

#### 6.1 Add Validate Command
**File**: `src/modelops/cli/jobs.py`
```python
@app.command()
def validate(
    job_id: str = typer.Argument(..., help="Job ID to validate"),
    env: Optional[str] = env_option(),
    force: bool = typer.Option(False, help="Force re-validation")
):
    """Manually validate job outputs."""
    # Implementation
```

### Phase 7: Idempotent Execution (Optional, 4 hours)
**Goal**: Check for existing outputs before task execution.

#### 7.1 Create IdempotentSimulationService
**New File**: `src/modelops/services/idempotent_sim.py`
```python
class IdempotentSimulationService:
    """Wrapper that checks ProvenanceStore before execution."""
    def __init__(self, sim_service, provenance_store):
        self.sim_service = sim_service
        self.provenance = provenance_store

    def submit(self, task: SimTask):
        # Check if output exists
        existing = self.provenance.get_sim(task)
        if existing:
            return CompletedFuture(existing)
        return self.sim_service.submit(task)
```

### Phase 8: Testing (4 hours)
**Goal**: Comprehensive tests for all new functionality.

#### 8.1 Unit Tests
**New File**: `tests/test_output_validation.py`
- Test manifest generation
- Test validation logic
- Test state transitions to VALIDATING and PARTIAL_SUCCESS
- Test resume task generation

#### 8.2 Integration Tests
**File**: `tests/test_job_registry_validation.py`
- Test full validation flow
- Test with mock ProvenanceStore
- Test partial completion scenarios

## Implementation Order & Time Estimates

| Phase | Description | Time | Priority | Dependencies |
|-------|-------------|------|----------|--------------|
| 1 | Extended State Machine | 2h | HIGH | None |
| 2 | Output Manifest Generation | 3h | HIGH | Phase 1 |
| 3 | ProvenanceStore Integration | 4h | HIGH | Phase 2 |
| 4 | Update Sync Command | 2h | HIGH | Phase 3 |
| 5 | Add Resume Command | 3h | MEDIUM | Phase 3 |
| 6 | Add Validate Command | 2h | MEDIUM | Phase 3 |
| 7 | Idempotent Execution | 4h | LOW | Phase 3 |
| 8 | Testing | 4h | HIGH | All phases |

**Total Estimated Time**: 24 hours (3-4 days)

## Files to Modify/Create

### Files to Modify
1. `src/modelops/services/job_state.py` - Add new states and fields
2. `src/modelops/services/job_registry.py` - Add validation methods
3. `src/modelops/cli/jobs.py` - Update sync, add resume/validate commands
4. `src/modelops/client/job_submission.py` - Generate manifest on submission

### Files to Create
1. `src/modelops/services/output_manifest.py` - Manifest generation
2. `src/modelops/services/idempotent_sim.py` - Idempotent wrapper (optional)
3. `tests/test_output_validation.py` - Unit tests
4. `tests/test_job_registry_validation.py` - Integration tests

## Testing Strategy

### Manual Testing Workflow
1. Submit a job with known outputs
2. Let it complete
3. Run `mops jobs sync` - should transition to VALIDATING then SUCCEEDED
4. Delete one output file manually
5. Run `mops jobs validate --force` - should show PARTIAL_SUCCESS
6. Run `mops jobs resume <job-id>` - should resubmit missing task
7. Verify completion

### Automated Tests
- Mock ProvenanceStore for unit tests
- Test all state transitions
- Test manifest generation with various job types
- Test idempotency with existing outputs

## Rollback Plan

Since this is additive:
1. New states don't affect existing jobs
2. Validation is optional (can disable with flag)
3. Old jobs without manifests skip validation
4. Can revert to v2.1 behavior by not calling validation

## Success Criteria

1. **Correctness**: Jobs only marked SUCCEEDED if all outputs present
2. **Resilience**: PARTIAL_SUCCESS jobs can be resumed
3. **Performance**: Validation completes in < 5s for 100 outputs
4. **Compatibility**: Existing jobs continue to work
5. **Observability**: Clear status about what outputs are missing

## Notes

- Keep it simple - just checking file existence, not content
- ProvenanceStore already handles checksums
- This is NOT a workflow engine
- Focus on data integrity, not complex orchestration