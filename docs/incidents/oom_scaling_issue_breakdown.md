# Technical Breakdown: OOM Issues with Large-Scale Job Submission in ModelOps

## Executive Summary
We're experiencing cascading OOM (Out of Memory) failures when submitting large-scale simulation studies. A grid study with 400 parameter sets × 30 replicates (12,000 tasks) causes workers to repeatedly OOMKill, making the job stuck indefinitely. The system lacks memory-aware task scheduling and doesn't detect/handle OOM conditions, leading to endless retry loops.

## Current Incident Details

### Job Configuration
- **Job ID**: `job-951bcfcb` (running for 12+ minutes, stuck)
- **Study Type**: Grid sampling (20×20 grid)
- **Scale**: 400 parameter sets × 30 replicates = **12,000 simulation tasks**
- **Model**: StarsimSIR (agent-based epidemic model with contact networks)
- **Bundle**: Using Starsim framework which creates RandomNet contact networks

### Infrastructure State
```yaml
Workers:
  Deployment: dask-workers
  Replicas: 4 (configured)
  Resources:
    Limits: 2 CPU, 4Gi memory
    Requests: 2 CPU, 4Gi memory
  Processes per worker: 4 (dask-worker --nprocs 4 --nthreads 1)

HorizontalPodAutoscaler:
  Min replicas: 2
  Max replicas: 10
  Target CPU: 70%
  Current CPU: 82%
  Current state: 4 replicas (1 pending, 3 restarting)
```

### Failure Pattern
```
Worker Pod Status:
- dask-workers-dcd86b544-9rpkp: OOMKilled, restarted 1x
- dask-workers-dcd86b544-l42dq: OOMKilled, restarted 1x
- dask-workers-dcd86b544-rj4n9: OOMKilled, restarted 1x
- dask-workers-dcd86b544-q7wwh: Pending (can't schedule)

Memory usage before crash: ~4GB (hitting limit)
```

## Root Cause Analysis

### 1. Model Memory Profile
The StarsimSIR model originally created contact networks in `__init__`:
```python
class StarsimSIR(BaseModel):
    def __init__(self, space: Optional[ParameterSpace] = None):
        # This was creating network for EVERY instance
        self.network = ss.RandomNet(n_contacts=self.config.network_contacts)
```

We fixed this by moving to `build_sim`, but with 12,000 tasks, even the deferred creation is problematic.

### 2. Task Submission Pattern
Current job_runner.py submits ALL tasks immediately:
```python
# From src/modelops/services/job_runner.py
for param_set_dict in study.parameter_sets:
    for seed in seeds[:study.n_replicates]:
        future = client.submit(
            run_simulation_task,
            model_ref=model_ref,
            params=param_set_dict,
            seed=seed,
            # ...
        )
        futures.append(future)
```

This creates 12,000 futures at once, overwhelming Dask's scheduler and workers.

### 3. Missing Safeguards
- No memory-based task limiting
- No OOM detection in job runner
- No backpressure mechanism
- No batch processing for large studies
- Job keeps "running" even when all workers are dead

## Architecture Context

### Current Flow
```
SimulationStudy (12k tasks)
    ↓
JobSubmissionClient.submit_job()
    ↓
K8s Job (job_runner.py)
    ↓
DaskClient.submit() × 12,000  ← ALL AT ONCE!
    ↓
Dask Scheduler (overwhelmed)
    ↓
Workers (OOM → restart → OOM loop)
```

### Dask Configuration
Workers started with:
```bash
dask-worker tcp://dask-scheduler:8786 \
  --name worker-$(hostname) \
  --nprocs 4 \
  --nthreads 1 \
  --memory-limit 1.0GiB \  # Per process limit
  --resources aggregation=1
```

With 4 processes per pod and 4GB total, each process gets ~1GB.

## Available Solutions

### 1. Dask Native Solutions

#### A. Worker Saturation (Immediate Fix)
Dask has `distributed.scheduler.worker-saturation` config:
```python
from dask.distributed import Client
client = Client(
    scheduler_address,
    config={
        'distributed.scheduler.worker-saturation': 1.0,  # Don't oversaturate
        'distributed.scheduler.work-stealing': True,
    }
)
```

This prevents scheduler from assigning more tasks than workers can handle.

#### B. Task Batching with as_completed
```python
from dask.distributed import as_completed
import itertools

def submit_in_batches(client, study, batch_size=100):
    """Submit tasks in controlled batches."""
    all_params = list(itertools.product(
        study.parameter_sets,
        range(study.n_replicates)
    ))

    results = []
    for i in range(0, len(all_params), batch_size):
        batch = all_params[i:i+batch_size]
        futures = []

        for params, seed in batch:
            future = client.submit(run_simulation_task, ...)
            futures.append(future)

        # Wait for batch to complete before submitting more
        for future in as_completed(futures):
            result = future.result()
            results.append(result)

    return results
```

#### C. Memory-Aware Submission
```python
def submit_with_memory_limit(client, study, memory_per_task_mb=100):
    """Submit tasks based on available worker memory."""
    worker_info = client.scheduler_info()['workers']
    total_memory = sum(w['memory_limit'] for w in worker_info.values())

    # Calculate safe batch size
    max_concurrent = int(total_memory / (memory_per_task_mb * 1024 * 1024))

    # Use Semaphore to limit concurrent tasks
    from distributed import Semaphore
    sem = Semaphore(max_concurrent)

    futures = []
    for params in study.parameter_sets:
        for seed in range(study.n_replicates):
            with sem:
                future = client.submit(run_simulation_task, ...)
                futures.append(future)

    return futures
```

### 2. Infrastructure Solutions

#### A. Increase Worker Memory
Update workspace configuration:
```yaml
workers:
  resources:
    memory: "8Gi"  # Double current
  replicas: 4
```

#### B. Adjust Process/Thread Balance
For memory-heavy tasks, fewer processes with more memory each:
```yaml
workers:
  processes: 2  # Instead of 4
  threads: 2    # Better memory sharing
```

### 3. Job Runner Improvements

#### A. Add OOM Detection
```python
def detect_worker_oom(client):
    """Check if workers are failing with OOM."""
    worker_info = client.scheduler_info()['workers']

    # Check Kubernetes for OOMKilled pods
    v1 = kubernetes.client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace="modelops-dask-dev")

    for pod in pods.items:
        if pod.status.container_statuses:
            for container in pod.status.container_statuses:
                if container.last_state.terminated:
                    if container.last_state.terminated.reason == "OOMKilled":
                        return True
    return False

# In job runner main loop
if detect_worker_oom(client):
    logger.error("Workers experiencing OOM - aborting job")
    sys.exit(1)  # Don't keep retrying
```

#### B. Implement Progressive Batching
```python
def run_study_with_adaptive_batching(client, study):
    """Start with small batch, increase if successful."""
    batch_size = 10
    max_batch = 1000

    all_tasks = generate_all_tasks(study)
    completed = 0

    while completed < len(all_tasks):
        batch = all_tasks[completed:completed+batch_size]

        try:
            futures = [client.submit(task) for task in batch]
            results = client.gather(futures, errors='skip')

            # Success - increase batch size
            batch_size = min(batch_size * 2, max_batch)
            completed += len(batch)

        except MemoryError:
            # Failure - decrease batch size
            batch_size = max(batch_size // 2, 1)
            if batch_size == 1:
                raise  # Can't reduce further
```

## Recommended Implementation Plan

### Phase 1: Immediate Fixes (Today)
1. **Enable worker-saturation in job_runner.py**:
```python
client = Client(
    scheduler_address,
    config={'distributed.scheduler.worker-saturation': 1.0}
)
```

2. **Add batch submission with `as_completed`**:
   - Batch size = 100 tasks initially
   - Wait for completion before next batch

3. **Add OOM detection to prevent infinite loops**:
   - Check pod status every minute
   - Exit gracefully if OOM detected

### Phase 2: Short-term (This Week)
1. **Implement memory-aware scheduling**:
   - Calculate memory per task from test runs
   - Use Semaphore to limit concurrent tasks

2. **Update default worker configuration**:
   - Increase memory to 8GB for simulation workloads
   - Adjust process/thread balance

3. **Add study size warnings**:
   - Warn users when submitting >1000 tasks
   - Suggest using adaptive algorithms instead

### Phase 3: Long-term (Next Sprint)
1. **Implement job chunking**:
   - Split large studies into multiple K8s Jobs
   - Coordinate via shared storage

2. **Add resource profiling**:
   - Profile memory usage per model type
   - Auto-configure batch sizes

3. **Implement spill-to-disk**:
   - Configure Dask to spill to disk when memory pressure
   - Add persistent volumes to workers

## Questions for Discussion

1. **Acceptable latency vs throughput tradeoff?**
   - Batching adds latency but prevents OOM
   - What's acceptable for users?

2. **Should we auto-detect model memory requirements?**
   - Run small test batch first?
   - Store profiles per model type?

3. **Resource limits philosophy?**
   - Hard fail on large studies?
   - Auto-chunk into smaller jobs?
   - Require explicit "large-scale" flag?

4. **Dask vs Ray consideration?**
   - Ray has better memory management
   - Worth evaluating for large-scale work?

## Appendix: Current Code References

### Job Runner (src/modelops/services/job_runner.py:151-200)
```python
# Current problematic pattern
logger.info(f"Processing {len(study.parameter_sets)} parameter sets with replicates")

for param_idx, param_set_dict in enumerate(study.parameter_sets):
    seeds = seed_generator.generate_seeds(param_set_dict, study.n_replicates)

    for seed in seeds[:study.n_replicates]:
        future = client.submit(
            run_simulation_task,
            model_ref=model_ref,
            scenario=study.scenario,
            params=param_set_dict,
            seed=seed,
            bundle_digest=bundle_digest,
            outputs=study.outputs,
            pure=False,
            key=f"{job_id}-p{param_idx}-s{seed}"
        )
        futures.append(future)
```

### Worker Deployment (via Pulumi ComponentResource)
```python
# From DaskWorkspace ComponentResource
container = k8s.core.v1.ContainerArgs(
    name="worker",
    image=worker_image,
    command=["dask-worker"],
    args=[
        f"tcp://{scheduler_service.metadata.name}:8786",
        "--nprocs", str(config.get("processes", 4)),
        "--nthreads", str(config.get("threads", 1)),
        "--memory-limit", "1.0GiB",
    ],
    resources=k8s.core.v1.ResourceRequirementsArgs(
        limits={"memory": "4Gi", "cpu": "2"},
        requests={"memory": "4Gi", "cpu": "2"}
    )
)
```

## Next Steps

1. **Immediate**: Should I implement worker-saturation config now?
2. **Today**: Create PR with batching implementation?
3. **Review**: Schedule architecture review for large-scale handling?

Let me know which approach you'd prefer or if you need more specific details on any solution!