# ModelOps Result Indexing and Job Attribution - Technical Design Problem

## Context for Colleague

You're somewhat familiar with ModelOps - our Kubernetes-native runtime for distributed simulation. This document describes our current result storage architecture and the missing pieces we need for post-job result analysis.

## Current Architecture

### 1. Job Submission Flow

When a user submits a simulation study with targets:

```python
# User generates study with Calabaria
cb sampling sobol "models/seir.py:StochasticSEIR" \
  --n-samples 256 \
  --n-replicates 500 \
  --targets "targets.prevalence:prevalence_target" \
  --output study.json

# Submit to cluster
mops jobs submit study.json --auto
```

The job contains:
- **SimulationStudy** with 256 parameter sets × 500 replicates = 128,000 simulations
- **Targets** for loss computation (e.g., comparing to observed prevalence data)
- **Job ID** like `job-5b718dc8`

### 2. Execution Architecture

```
K8s Job (job-5b718dc8)
    └── Dask Workers (pods)
         ├── Run simulations (128,000 tasks)
         ├── Aggregate replicates (256 aggregation tasks)
         └── Compute losses against targets
```

Each aggregation:
1. Takes 500 simulation results (same param_id, different seeds)
2. Aggregates outputs (e.g., mean prevalence over replicates)
3. Computes loss against target data
4. Returns `AggregationReturn` with loss value

### 3. Result Storage (ProvenanceStore)

All results go to content-addressed storage based on inputs:

```
/tmp/modelops/provenance/
  /token/v1/
    /sims/{bundle_digest[:12]}/{shard(param_id)}/params_{param_id[:8]}/seed_{seed}/
      metadata.json    # params, bundle_ref, timestamp - NO JOB_ID
      result.json      # outputs, task_id
      artifact_*.arrow # actual simulation data

    /aggs/{bundle_digest[:12]}/target_{target}/agg_{aggregation_id}/
      metadata.json    # param_id (extracted), bundle_ref - NO JOB_ID
      result.json      # Contains: loss value, diagnostics, n_replicates
```

**Critical Issue**: Results are stored by content hash (bundle + params + seed), not by job. Multiple jobs with same inputs share results (caching), but we lose job attribution.

### 4. Current State After Job Completion

When job completes, we have:
- ✅ All simulation results in ProvenanceStore
- ✅ All aggregation results with loss values
- ✅ JobRegistry knows job succeeded
- ❌ No connection between job and its results
- ❌ No aggregated loss DataFrame
- ❌ No way to query "give me all losses for job X"

## Problems to Solve

### Problem 1: Job Attribution

**Current Issue**:
- Job A runs 256 parameter sets, stores results
- Job B runs overlapping parameter sets, gets cache hits
- We can't tell which results belong to which job
- No way to create job-specific loss report

**Engineering Constraints**:
- Must preserve content-addressed caching (same inputs → same location)
- ProvenanceStore is append-only for results (idempotent writes)
- Multiple jobs may "own" the same result

**Potential Solutions**:
1. **Attribution log** alongside results:
```
/provenance/attribution/
  /by-job/{job_id}/manifest.json    # List of result paths
  /by-result/{path}/jobs.json       # List of job_ids
```

2. **Job manifest in JobRegistry**:
```python
# In JobState
expected_outputs: List[OutputSpec]  # What we expect
computed_outputs: List[str]         # Paths to actual results
cache_hits: List[str]               # Paths that were cache hits
```

### Problem 2: Result Indexing After Job Completion

**Current Issue**:
- Losses are scattered across 256 aggregation directories
- No automatic collection into analyzable format
- User can't easily see loss landscape

**Requirements**:
- Automatically triggered when job completes
- Collect all aggregation results for the job
- Create indexed DataFrame with columns:
  - param_id
  - parameter values (unpacked)
  - loss
  - n_replicates
  - target_name
  - timestamp
  - job_id

**Engineering Constraints**:
- Must handle partial results (some aggregations may fail)
- Should be idempotent (can re-run safely)
- Output format needs to support large result sets (100k+ parameter sets)

**Potential Solutions**:

1. **Post-job hook in worker**:
```python
# In adaptive worker after job completes
if job_state.status == JobStatus.SUCCEEDED:
    indexer = ResultIndexer(provenance_store, job_registry)
    parquet_path = indexer.index_job_results(job_id)
    # Store parquet in blob storage with job attribution
```

2. **Separate indexing service**:
```python
# Watches for job completion events
# Runs indexing as separate K8s Job
# Stores results in dedicated location:
/results/{job_id}/
  losses.parquet     # All losses indexed
  manifest.json      # Job metadata
  summary.json       # Statistics
```

### Problem 3: Querying Historical Results

**Current Issue**:
- No way to compare losses across multiple jobs
- Can't track optimization progress over time
- No unified view of all experiments

**Requirements**:
- Query losses by job_id, bundle_digest, or time range
- Compare loss landscapes between jobs
- Track best parameters found so far

**Engineering Approach Needed**:
- Where to store indexed results? (Blob storage, database, or filesystem?)
- How to handle large result sets? (Streaming, pagination, or chunking?)
- Should we build a result catalog/registry?

## Technical Details

### Current Data Structures

```python
# What we have in AggregationReturn (stored in result.json)
class AggregationReturn:
    aggregation_id: str
    loss: float  # THE KEY VALUE WE NEED
    diagnostics: Dict[str, Any]
    n_replicates: int
    outputs: Dict[str, TableArtifact]  # Usually empty

# What we need for analysis
@dataclass
class IndexedResult:
    job_id: str
    param_id: str
    parameters: Dict[str, float]  # Unpacked parameter values
    loss: float
    n_replicates: int
    target: str
    bundle_digest: str
    computed_at: datetime
    cache_hit: bool
```

### Storage Calculations

For a typical job:
- 256 parameter sets × ~1KB per aggregation result = 256KB of loss data
- Indexed parquet with all parameters: ~500KB-1MB
- Need to handle jobs with 10k+ parameter sets (10-50MB indexed)

### Existing Infrastructure We Can Leverage

1. **JobRegistry** - Already tracks job lifecycle, could store result manifest
2. **ProvenanceStore** - Has all the data, just needs attribution layer
3. **Blob Storage** - Already configured for large artifacts
4. **Dask Client** - Could run indexing as distributed task

## Questions for Design Feedback

1. **Attribution Architecture**: Should we track job↔result mappings in ProvenanceStore, JobRegistry, or separate service?

2. **Indexing Trigger**: Should indexing happen:
   - In the worker process after job completes?
   - As a separate K8s Job/CronJob?
   - As a Dask task on the cluster?

3. **Storage Strategy**: Where should indexed results live?
   - Alongside job in blob storage?
   - In ProvenanceStore with job-specific schema?
   - In a separate results database?

4. **API Design**: How should users access indexed results?
   - Direct parquet file access?
   - REST API with query capabilities?
   - Python client with DataFrame methods?

5. **Consistency Model**: How do we handle:
   - Jobs that share cached results?
   - Partial failures (some aggregations fail)?
   - Re-runs of the same job?

## Proposed Implementation Priority

1. **Phase 1**: Add job attribution to ProvenanceStore
   - Track which job computed/used each result
   - Enable "list all results for job X"

2. **Phase 2**: Automatic result indexing
   - Trigger on job completion
   - Create losses.parquet per job
   - Store in blob storage

3. **Phase 3**: Result query API
   - List all jobs and their loss summaries
   - Compare losses across jobs
   - Export for downstream analysis

## Code Examples We'd Like to Support

```python
# After job completes (automatic)
job_id = "job-5b718dc8"
# System automatically creates:
# /results/job-5b718dc8/losses.parquet
# /results/job-5b718dc8/manifest.json

# User retrieves results
from modelops.client import ResultClient
client = ResultClient()

# Get losses for specific job
df = client.get_losses(job_id)
# Returns DataFrame with: param_id, parameters, loss, n_replicates

# Query across jobs
results = client.query_losses(
    bundle_digest="sha256:abc123...",
    min_loss=0.0,
    max_loss=1.0,
    limit=100
)

# Get best parameters found
best = client.get_best_parameters(
    target="prevalence_target",
    n_best=10
)
```

## Technical Constraints

1. **Backward Compatibility**: Existing results lack job_id - need migration strategy
2. **Scale**: Some jobs have 100k+ parameter sets - need efficient indexing
3. **Caching**: Must preserve cache sharing between jobs
4. **Idempotency**: All operations must be retry-safe
5. **Kubernetes**: Solution must work in K8s environment with pod restarts

What's your recommendation for the architecture and implementation approach?