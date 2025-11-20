# SimBatch Removal Refactor Plan

## Current State

The current type hierarchy has an unnecessary intermediate layer:

```python
SimulationStudy → SimJob → SimBatch → SimTask
```

Where SimBatch is ALWAYS 1:1 with SimJob, adding no value.

## Proposed Simplification

Remove SimBatch entirely and have SimJob directly contain tasks:

```python
SimulationStudy → SimJob → SimTask
```

## Code Sketches

### 1. Current modelops_contracts/study.py

```python
# CURRENT (problematic)
@dataclass(frozen=True)
class ParameterSet:
    """Pure parameter values without execution context."""
    params: Dict[str, Any]  # Just a dict wrapper!

@dataclass(frozen=True)
class SimBatch:
    """Batch of simulation tasks."""
    batch_id: str
    tasks: List[SimTask]

@dataclass(frozen=True)
class SimJob:
    """Job containing batches of simulation tasks."""
    job_id: str
    batches: List[SimBatch]  # Always exactly 1!
    metadata: Dict[str, Any]

@dataclass(frozen=True)
class SimulationStudy:
    """Bundle-agnostic experiment design."""
    model: str
    scenario: str
    parameter_sets: List[ParameterSet]  # Weak wrapper type
    sampling_method: str
    n_replicates: int
    outputs: Optional[List[str]]
    metadata: Optional[Dict[str, Any]]

    def to_simjob(self, bundle_ref: str, job_id: str) -> SimJob:
        """Convert study to executable job."""
        tasks = []
        for param_set in self.parameter_sets:
            for replicate_idx in range(self.n_replicates):
                # Build entrypoint
                entrypoint_str = f"{self.model}/{self.scenario}"

                # Create task
                task = SimTask.from_components(
                    import_path=self.model,
                    scenario=self.scenario,
                    bundle_ref=bundle_ref,
                    params=param_set.params,  # Extract dict
                    seed=self._compute_seed(param_set, replicate_idx),
                    outputs=self.outputs
                )
                tasks.append(task)

        # Unnecessary batch wrapper!
        batch = SimBatch(
            batch_id=f"{job_id}-batch-0",
            tasks=tasks
        )

        return SimJob(
            job_id=job_id,
            batches=[batch],  # Always exactly 1
            metadata=self.metadata or {}
        )
```

### 2. Proposed Refactored modelops_contracts/study.py

```python
# PROPOSED (simplified)
# Remove ParameterSet class entirely - not needed!

@dataclass(frozen=True)
class SimJob:
    """Job containing simulation tasks - no batch wrapper needed."""
    job_id: str
    tasks: List[SimTask]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def task_count(self) -> int:
        """Get total number of tasks."""
        return len(self.tasks)

    def get_task_groups(self) -> Dict[str, List[SimTask]]:
        """Group tasks by parameter set for aggregation."""
        groups = {}
        for task in self.tasks:
            param_id = task.params.param_id
            if param_id not in groups:
                groups[param_id] = []
            groups[param_id].append(task)
        return groups

@dataclass(frozen=True)
class SimulationStudy:
    """Bundle-agnostic experiment design."""
    model: str
    scenario: str
    parameter_sets: List[Dict[str, Any]]  # Just plain dicts, no wrapper!
    sampling_method: str
    n_replicates: int = 1
    outputs: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_simjob(self, bundle_ref: str, job_id: str) -> SimJob:
        """Convert study to executable job - DIRECT, no batch!"""
        tasks = []

        for param_dict in self.parameter_sets:
            # Convert dict to UniqueParameterSet HERE
            unique_params = UniqueParameterSet.from_dict(param_dict)

            for replicate_idx in range(self.n_replicates):
                # Create task directly
                task = SimTask.from_components(
                    import_path=self.model,
                    scenario=self.scenario,
                    bundle_ref=bundle_ref,
                    params=param_dict,  # from_components will convert
                    seed=self._compute_seed(unique_params.param_id, replicate_idx),
                    outputs=self.outputs
                )
                tasks.append(task)

        # Direct SimJob, no batch wrapper!
        return SimJob(
            job_id=job_id,
            tasks=tasks,
            metadata=self.metadata or {}
        )

    def _compute_seed(self, param_id: str, replicate_idx: int) -> int:
        """Compute deterministic seed from param_id and replicate index."""
        import hashlib
        content = f"{param_id}:{replicate_idx}"
        hash_bytes = hashlib.blake2b(content.encode(), digest_size=8).digest()
        return int.from_bytes(hash_bytes, byteorder='little') % (2**32)
```

### 3. Impact on JobSubmissionClient (modelops/client/job_submission.py)

```python
# CURRENT
class JobSubmissionClient:
    def submit_simjob(self, sim_job: SimJob) -> str:
        """Submit SimJob to cluster."""
        # Has to iterate through batches unnecessarily
        all_tasks = []
        for batch in sim_job.batches:
            all_tasks.extend(batch.tasks)

        # Submit tasks...

# PROPOSED
class JobSubmissionClient:
    def submit_simjob(self, sim_job: SimJob) -> str:
        """Submit SimJob to cluster."""
        # Direct access to tasks!
        futures = self.sim_service.submit_batch(sim_job.tasks)

        # Track job
        self._active_jobs[sim_job.job_id] = {
            'futures': futures,
            'metadata': sim_job.metadata
        }

        return sim_job.job_id
```

### 4. Impact on Serialization

```python
# CURRENT (manual conversion needed)
def _study_to_dict(study: SimulationStudy) -> dict:
    """Manual serialization because of nested ParameterSet."""
    return {
        "parameter_sets": [
            {"params": ps.params}  # Extract dict from wrapper
            for ps in study.parameter_sets
        ],
        # ...
    }

# PROPOSED (cleaner)
def _study_to_dict(study: SimulationStudy) -> dict:
    """Simple serialization - parameter_sets are already dicts."""
    return {
        "parameter_sets": [
            {"params": ps}  # Already a dict!
            for ps in study.parameter_sets
        ],
        # ...
    }

# Or even better with Pydantic:
class SimulationStudy(BaseModel):
    """With Pydantic, serialization is automatic."""
    model: str
    scenario: str
    parameter_sets: List[Dict[str, Any]]
    sampling_method: str
    n_replicates: int = 1
    outputs: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None

    # model_dump() and model_dump_json() come for free!
```

## Migration Path

### Phase 1: Add Deprecation Warnings
```python
@dataclass(frozen=True)
class SimBatch:
    """DEPRECATED: Will be removed in v2.0. Use SimJob.tasks directly."""
    # ...

class SimJob:
    batches: List[SimBatch]  # DEPRECATED

    @property
    def tasks(self) -> List[SimTask]:
        """Direct access to tasks (future interface)."""
        if self.batches:
            # Support old format
            return [task for batch in self.batches for task in batch.tasks]
        return self._tasks  # Future: direct storage
```

### Phase 2: Dual Support
```python
class SimJob:
    """Support both old (batches) and new (tasks) format."""

    def __init__(self, job_id: str,
                 tasks: Optional[List[SimTask]] = None,
                 batches: Optional[List[SimBatch]] = None,
                 metadata: Optional[Dict] = None):
        self.job_id = job_id
        self.metadata = metadata or {}

        if tasks is not None:
            # New format
            self._tasks = tasks
            self.batches = []  # Empty for compatibility
        elif batches is not None:
            # Old format
            self.batches = batches
            self._tasks = None
        else:
            raise ValueError("Either tasks or batches must be provided")

    @property
    def tasks(self) -> List[SimTask]:
        """Get tasks regardless of storage format."""
        if self._tasks is not None:
            return self._tasks
        return [task for batch in self.batches for task in batch.tasks]
```

### Phase 3: Remove Old Code
```python
# Final state - clean and simple
@dataclass(frozen=True)
class SimJob:
    """Job containing simulation tasks."""
    job_id: str
    tasks: List[SimTask]
    metadata: Dict[str, Any] = field(default_factory=dict)

# SimBatch removed entirely
# ParameterSet removed entirely
```

## Benefits

1. **Simpler Mental Model**: Study → Job → Task (no confusing batch layer)
2. **Less Code**: Remove entire SimBatch class and conversion logic
3. **Cleaner Serialization**: No wrapper types to unwrap
4. **Better Performance**: One less layer of iteration
5. **Clearer Intent**: SimJob directly contains what it executes

## Testing Strategy

1. **Unit Tests**: Test new SimJob.tasks property with both formats
2. **Integration Tests**: Ensure job submission works with new format
3. **Backward Compatibility Tests**: Old serialized jobs still load
4. **Migration Tests**: Verify deprecation warnings appear

## Timeline

- **Week 1**: Add deprecation warnings, implement dual support
- **Week 2**: Update all code to use new format
- **Week 3**: Test migration with real workloads
- **v2.0 Release**: Remove deprecated code entirely