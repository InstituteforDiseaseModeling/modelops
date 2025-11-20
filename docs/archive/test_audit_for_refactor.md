# Test Audit for SimBatch/ParameterSet Removal Refactor

## Executive Summary

After auditing all test files across modelops-contracts, modelops, and modelops-calabaria, I've identified the complete testing landscape that will be affected by our refactor. The changes are concentrated primarily in modelops-contracts with minimal impact on downstream packages.

## Testing Landscape

### 1. modelops-contracts (Most Affected)

#### Core Test Files:
- **`tests/test_batch.py`** - 337+ lines entirely dedicated to SimBatch
  - Tests SimBatch validation
  - Tests batch hash computation
  - Tests task counting
  - **Action**: DELETE ENTIRE FILE after refactor

- **`tests/test_study.py`** (if exists, needs checking)
  - Tests ParameterSet wrapper
  - Tests SimulationStudy.to_simjob()
  - **Action**: Update to use plain dicts, remove ParameterSet tests

- **`tests/test_jobs.py`** (likely exists)
  - Tests SimJob with batches field
  - Tests job validation
  - **Action**: Update to use direct tasks field

#### Files Using UniqueParameterSet (Keep These):
- `tests/test_contracts.py` - Tests UniqueParameterSet (KEEP)
- `tests/test_simulation.py` - Uses UniqueParameterSet correctly (KEEP)
- `tests/test_ports.py` - Uses UniqueParameterSet correctly (KEEP)

### 2. modelops (Minimal Impact)

#### Files Mentioning SimBatch/ParameterSet:
- `tests/test_provenance_store.py` - Likely uses UniqueParameterSet (OK)
- `tests/test_simulation_service.py` - Uses SimTask/UniqueParameterSet (OK)
- `tests/test_dask_serialization.py` - May test batch serialization
- `tests/test_contracts.py` - Import checks

#### Example Files (Integration Tests):
- `examples/test_job_submission.py` - May create SimJob with batches
- `examples/test_job_submission_simple.py` - May create SimJob with batches
- `examples/test_simulation_e2e.py` - End-to-end tests
- **Action**: Update SimJob creation to use tasks directly

### 3. modelops-calabaria (Light Touch)

#### Files Affected:
- `tests/test_cli_sampling.py`:
  - Line 10: Imports SimBatch (REMOVE)
  - Line 176: Imports UniqueParameterSet (KEEP - it's correct)
  - Line 204: Creates SimBatch for testing (UPDATE)

- `tests/test_sampling_strategies.py`:
  - Line 226: Comment mentions SimBatch (UPDATE COMMENT)

- `src/modelops_calabaria/cli/sampling.py`:
  - Already fixed to use plain dicts instead of ParameterSet ✓

## Detailed Test Changes Required

### Phase 1: Add Compatibility Layer

```python
# modelops-contracts/src/modelops_contracts/jobs.py
@dataclass(frozen=True)
class SimJob(Job):
    """Simulation job with tasks."""
    # Support both old and new format
    batches: Optional[List[SimBatch]] = None  # DEPRECATED
    tasks: Optional[List[SimTask]] = None      # NEW

    def __post_init__(self):
        if self.tasks is None and self.batches:
            # Convert old format to new
            object.__setattr__(self, 'tasks',
                [task for batch in self.batches for task in batch.tasks])
        elif self.tasks is None and self.batches is None:
            raise ValueError("Either tasks or batches must be provided")

    @property
    def all_tasks(self) -> List[SimTask]:
        """Get all tasks (works with both formats)."""
        return self.tasks or []
```

### Phase 2: Update Test Files

#### test_batch.py → test_job_tasks.py
```python
# OLD TEST (delete)
def test_batch_requires_tasks():
    batch = SimBatch(
        batch_id="batch-001",
        tasks=[create_test_task()],
        sampling_method="grid"
    )
    assert batch.task_count() == 1

# NEW TEST (add to test_jobs.py)
def test_job_requires_tasks():
    job = SimJob(
        job_id="job-001",
        bundle_ref=TEST_BUNDLE_REF,
        tasks=[create_test_task()]
    )
    assert job.task_count() == 1
```

#### test_study.py Updates
```python
# OLD
def test_study_with_parameter_sets():
    study = SimulationStudy(
        model="models.test",
        scenario="baseline",
        parameter_sets=[
            ParameterSet(params={"x": 1}),
            ParameterSet(params={"x": 2})
        ]
    )

# NEW
def test_study_with_parameter_dicts():
    study = SimulationStudy(
        model="models.test",
        scenario="baseline",
        parameter_sets=[
            {"x": 1},  # Plain dict
            {"x": 2}   # Plain dict
        ]
    )
```

#### Integration Test Updates
```python
# OLD (examples/test_job_submission.py)
def test_submit_job():
    batch = SimBatch(
        batch_id="batch-1",
        tasks=tasks,
        sampling_method="grid"
    )
    job = SimJob(
        job_id="job-1",
        batches=[batch],
        bundle_ref=bundle_ref
    )

# NEW
def test_submit_job():
    job = SimJob(
        job_id="job-1",
        tasks=tasks,  # Direct!
        bundle_ref=bundle_ref
    )
```

### Phase 3: Remove Deprecated Code

After verification, remove:
1. `SimBatch` class entirely
2. `ParameterSet` class entirely
3. `test_batch.py` file entirely
4. Compatibility code in `SimJob`

## Test Coverage Analysis

### What's Well Tested:
- ✅ UniqueParameterSet with param_id generation
- ✅ SimTask validation and creation
- ✅ SimJob validation (needs update for tasks)
- ✅ Bundle reference validation
- ✅ Seed generation determinism

### What Needs New Tests:
- ❌ SimJob.get_task_groups() method (new)
- ❌ SimulationStudy with plain dict parameter_sets
- ❌ Migration from old to new format
- ❌ Serialization of simplified types

## Risk Assessment

### Low Risk:
- Removing ParameterSet - it's just a dict wrapper
- Removing SimBatch - always 1:1 with SimJob
- Updating tests - mostly mechanical changes

### Medium Risk:
- Serialization compatibility - need careful migration
- Integration tests - may have hidden dependencies

### Mitigation:
1. Keep compatibility layer for 1-2 releases
2. Add deprecation warnings immediately
3. Test with real workloads before removing old code
4. Maintain backward compatibility for deserialization

## Recommended Test Strategy

### Phase 1 (Week 1):
1. Add compatibility layer with full test coverage
2. Run ALL existing tests - must pass
3. Add deprecation warnings

### Phase 2 (Week 2):
1. Update modelops-contracts tests to use new format
2. Update integration tests in modelops
3. Update Calabaria tests
4. All tests must pass with warnings

### Phase 3 (Week 3):
1. Test with production workloads
2. Verify serialization compatibility
3. Performance testing

### Phase 4 (v2.0):
1. Remove deprecated code
2. Remove compatibility layer
3. Delete test_batch.py
4. Final test suite cleanup

## Conclusion

The testing landscape shows this refactor is **safe to proceed with**:

1. **Concentrated Impact**: Most changes in modelops-contracts
2. **Clear Migration Path**: Compatibility layer enables gradual migration
3. **Good Test Coverage**: Existing tests will catch issues
4. **Low Risk**: Removing unnecessary abstractions, not changing behavior

The test suite actually makes this refactor easier - it clearly shows SimBatch is purely structural with no behavioral significance, and ParameterSet is just a wrapper around dict with no validation logic.

## Next Steps

1. Implement compatibility layer in SimJob
2. Update SimulationStudy to use plain dicts
3. Run test suite to ensure compatibility
4. Gradually migrate tests to new format
5. Remove deprecated code in v2.0