# ProvenanceStore Design Overview & Questions

## Current Implementation

### ProvenanceSchema
Defines how results are organized on disk based on inputs (provenance-based addressing).

```python
# From src/modelops/services/provenance_schema.py
class ProvenanceSchema:
    """Defines path structure for provenance-based storage"""

    def sim_path(self, bundle_digest, param_id, seed, **kwargs) -> str:
        """Generate path for simulation result

        Example output:
        'sha256/abc123.../v1/sims/params_xyz789/seed_42/'

        Structure:
        └── sha256/
            └── abc123.../           # Bundle digest (first 12 chars)
                └── v1/              # Schema version
                    └── sims/        # Simulation results
                        └── params_xyz789/  # Parameter set ID
                            └── seed_42/    # Seed value
                                ├── metadata.json  # Task inputs
                                ├── result.json    # Task outputs metadata
                                └── artifact_*.arrow  # Actual data files
        """

# Two schemas available:
DEFAULT_SCHEMA = "token"  # Bundle changes invalidate all results
PARAMS_ONLY_SCHEMA = "params_only"  # Only param changes invalidate
```

### ProvenanceStore
Manages storage and retrieval of simulation results.

```python
# From src/modelops/services/provenance_store.py
class ProvenanceStore:
    def __init__(self,
                 storage_dir: Path,          # Always local directory
                 schema: ProvenanceSchema,
                 azure_backend: Optional[Dict] = None):  # Optional Azure uploads

        # ALWAYS stores locally first
        self.storage_dir = storage_dir  # e.g., /tmp/modelops/provenance/

        # OPTIONALLY also uploads to Azure
        self._azure_backend = AzureBlobBackend(...) if azure_backend else None

    def put_sim(self, task: SimTask, result: SimReturn):
        """Store simulation result"""
        # 1. Always write to local disk
        result_dir = self.storage_dir / self.schema.sim_path(...)
        result_dir.mkdir(parents=True)

        # Write manifest files
        write_json(result_dir / "metadata.json", task_metadata)
        write_json(result_dir / "result.json", result_metadata)

        # Write data files
        for name, artifact in result.outputs.items():
            if artifact.inline:  # Data included in result
                write_file(result_dir / f"artifact_{name}.arrow", artifact.inline)

        # 2. Optionally upload to Azure (synchronous currently)
        if self._azure_backend:
            for file in result_dir.glob("*"):
                blob_path = f"{sim_path}/{file.name}"
                self._azure_backend.save(blob_path, file.read_bytes())

    def get_sim(self, task: SimTask) -> Optional[SimReturn]:
        """Retrieve simulation result"""
        result_dir = self.storage_dir / self.schema.sim_path(...)

        # 1. Check local first
        if not result_dir.exists():
            # 2. Try downloading from Azure
            if self._azure_backend:
                # Download all files for this sim
                blobs = self._azure_backend.list_keys(sim_path)
                for blob in blobs:
                    data = self._azure_backend.load(blob)
                    local_file = result_dir / Path(blob).name
                    local_file.write_bytes(data)

        # 3. Load from local files
        metadata = json.load(result_dir / "metadata.json")
        result = json.load(result_dir / "result.json")

        # Reconstruct artifacts
        for name in result["outputs"]:
            artifact_file = result_dir / f"artifact_{name}.arrow"
            result["outputs"][name]["inline"] = artifact_file.read_bytes()

        return SimReturn(**result)
```

## Current Architecture Flow

```
1. SIMULATION EXECUTION (on Dask worker)
   └── IsolatedWarmExecEnv.run(task)
       └── subprocess executes simulation
       └── returns SimReturn with inline artifacts
       └── ProvenanceStore.put_sim(task, result)
           ├── Writes to worker's local /tmp/modelops/provenance/
           └── [Optional] Uploads to Azure blob storage

2. AGGREGATION (on same or different worker)
   └── Needs results from multiple simulations
   └── ProvenanceStore.get_sim(task) for each
       ├── Checks local disk first (fast if same worker)
       └── Downloads from Azure if not local (if configured)

3. USER RETRIEVAL (from client machine)
   └── Currently: No direct support
   └── Must either:
       - kubectl cp from pods
       - Download from Azure
       - Run client on same machine as worker
```

## Storage Locations & States

```
┌─────────────────────────────────────────────────────────┐
│                     Storage Tiers                        │
├─────────────────────────────────────────────────────────┤
│ 1. Worker Pod Local Disk (/tmp/modelops/provenance/)    │
│    - Fastest access for aggregations                     │
│    - Ephemeral (lost on pod restart)                    │
│    - Scattered across workers                           │
│                                                          │
│ 2. Azure Blob Storage (optional)                        │
│    - Persistent                                         │
│    - Accessible from anywhere                          │
│    - Higher latency                                     │
│    - Costs money                                        │
│                                                          │
│ 3. User's Local Machine                                 │
│    - Currently no direct path                           │
│    - Must download from Azure or kubectl cp            │
└─────────────────────────────────────────────────────────┘
```

## Key Design Questions for Colleague

### 1. How Lazy Should Uploads Be?

**Current**: Uploads happen immediately during put_sim() if configured
```python
def put_sim(self, task, result):
    save_locally()
    if azure_configured:
        upload_to_azure()  # Synchronous, happens immediately
```

**Alternative A**: Batch uploads periodically
```python
def put_sim(self, task, result):
    save_locally()
    mark_for_upload()  # Just track what needs uploading

def sync_to_azure():  # Called periodically or on-demand
    for pending in get_pending_uploads():
        upload_to_azure(pending)
```

**Alternative B**: Upload only on explicit request
```python
# Never auto-upload, only when user runs:
mops results push --job-id abc123
mops results push --bundle sha256:...
mops results push --since 2024-01-01
```

### 2. Manifest-Data Connection for DataFrames

**Current Issues**:
- Metadata (manifest) and data (artifacts) are separate files
- No convenient API to get "all results from a run as DataFrames"
- Users must manually reconstruct from files

**Desired API**:
```python
# User wants this experience:
from modelops.client import ResultsClient

client = ResultsClient()

# Get all results from a specific job/study
study_results = client.get_study_results(job_id="abc123")
# Returns: Dict[str, pd.DataFrame] with all simulation outputs

# Or get specific simulation
sim_result = client.get_simulation(
    bundle="sha256:...",
    params={"alpha": 0.5},
    seed=42
)
# Returns: Dict[str, pd.DataFrame] for that one simulation

# Or bulk query
results = client.query_results(
    bundle="sha256:...",
    param_filter=lambda p: p["alpha"] > 0.3,
    output_names=["infections", "deaths"]
)
# Returns: pd.DataFrame with params as index, outputs as columns
```

### 3. Worker-to-Client Data Path

**Option A**: Direct from workers
```python
# Client could query workers directly via Dask
dask_client.run(collect_local_results, workers=...)
```

**Option B**: Via Azure (current)
```python
# Workers upload → Azure → Client downloads
# Pro: Persistent, works after cluster dies
# Con: Slower, costs money
```

**Option C**: Hybrid with local cache
```python
# Check local → Check workers → Check Azure
# Cache locally after fetch
```

### 4. Result Addressing & Indexing

**Current**: Content-addressed by inputs (provenance)
```
Path = f(bundle_digest, param_id, seed)
```

**Questions**:
- Should we maintain an index/catalog of what results exist?
- How to map from high-level concepts (job, study) to individual results?
- Should manifest include pointers to related results (e.g., other seeds)?

### 5. Lifecycle Management

**Key Questions**:
1. When should results move from pod → Azure?
   - Immediately after creation?
   - When pod is about to die?
   - Only on explicit request?
   - After aggregation completes?

2. When can results be deleted from pods?
   - After uploading to Azure?
   - After aggregation?
   - Never (let pod restart clean them)?

3. How to handle partial uploads?
   - Track upload status per result?
   - Checksums for verification?
   - Retry logic?

## Concrete Use Cases to Design For

### Use Case 1: Development/Debugging
- User runs small study locally
- Wants to inspect results immediately
- Doesn't want Azure costs
- Needs quick iteration

### Use Case 2: Large Production Run
- 10,000+ simulations
- Results scattered across 50 worker pods
- Need to persist before weekend pod maintenance
- Want to analyze subset without downloading everything

### Use Case 3: Post-Hoc Analysis
- Job completed days ago
- Pods have restarted
- User wants specific parameter combinations
- Needs to join with external data

### Use Case 4: Reproducibility Audit
- Given a job ID, reconstruct exact inputs/outputs
- Verify nothing was lost
- Generate report of what was run


## Questions for Colleague

1. **Laziness**: Should uploads be automatic (current), batched, or fully manual?

2. **Client Access**: Should we build a Python client library for result
   retrieval -- how would it look? what downstream operations would be
   supported?

3. **Manifest Structure**: Should we enhance metadata.json to include more
   queryable fields? Job ID? Study name? How do we combine/index manifests.

4. **Indexing**: Should we maintain a central index of all results, or keep it fully distributed?

5. **Caching**: Should the client cache downloaded results locally? How to handle cache invalidation?

6. **Streaming**: For large results, should we support streaming from Azure → client without full download?

7. **Compression**: Should we compress artifacts before storage? Trade CPU for storage/bandwidth?

8. **Retention**: Should we support different retention policies for different result types?

## Current Pain Points

1. **No direct way to get results to user's machine** - must use kubectl or Azure
2. **No convenient DataFrame API** - users work with raw files
3. **No job-level result management** - everything is per-simulation
4. **Uploads during execution might slow down simulations**
5. **No way to query "what results exist"** without scanning filesystem

## Implementation Status

- ✅ ProvenanceStore with local storage
- ✅ Optional Azure upload (synchronous during put)
- ✅ Download from Azure if local missing
- ❌ Batch upload capabilities
- ❌ Collection from worker pods
- ❌ Client library for result access
- ❌ DataFrame-friendly APIs
- ❌ Result indexing/catalog
- ❌ CLI commands for result management

Let me know what your colleague thinks about these design questions!
