# ProvenanceStore Design

## Overview

The ProvenanceStore is ModelOps' unified storage system for simulation and aggregation results. It replaces the previous dual-system architecture (SimulationCache + CAS) with a single, provenance-based storage solution that supports flexible invalidation strategies through declarative schemas.

## Key Design Decisions

### Single Storage System
- **Before**: Separate SimulationCache for metadata and CAS (Content-Addressed Storage) for artifacts
- **After**: Unified ProvenanceStore handles both metadata and artifacts
- **Rationale**: Eliminates complexity of coordinating two systems, simplifies transactions

### Input-Addressed vs Content-Addressed
- **Input-addressed**: Storage paths derived from simulation inputs (bundle, params, seed)
- **Content-addressed**: Storage paths derived from output content hash
- **Decision**: Use input-addressed storage for better cache invalidation control
- **Trade-off**: Can't deduplicate identical outputs from different inputs

### MVP Simplification
- **Always inline in memory**: All artifacts loaded into memory as `TableArtifact.inline`
- **Always blobs on disk**: All artifacts stored as separate files on disk
- **No refs**: Removed complexity of inline vs reference decision based on size
- **Future**: Can add streaming/chunking for large artifacts post-MVP

## Architecture

```
ProvenanceStore
├── ProvenanceSchema (declarative path templates)
│   ├── DSL for path generation
│   ├── Invalidation strategy encoded in paths
│   └── Schema versioning for migrations
│
├── Storage Operations
│   ├── get_sim() / put_sim()
│   ├── get_agg() / put_agg()
│   └── list_results()
│
└── Storage Layout
    └── {storage_dir}/
        └── {schema_name}/
            └── v{version}/
                ├── sims/
                └── aggs/
```

## ProvenanceSchema DSL

The schema uses a simple Domain-Specific Language for path templates:

### DSL Functions

| Function | Description | Example |
|----------|-------------|---------|
| `{var}` | Direct variable interpolation | `{seed}` → `42` |
| `{var[:n]}` | Variable with slicing | `{param_id[:8]}` → `abcd1234` |
| `{hash(var)[:n]}` | Hash of variable (BLAKE2b) | `{hash(bundle_digest)[:12]}` → `a1b2c3d4e5f6` |
| `{shard(var,d,w)}` | Sharded path for filesystem efficiency | `{shard(param_id,2,2)}` → `ab/cd` |

### Path Template Examples

```python
# Bundle-based invalidation (coarse-grained)
sim_path_template = "sims/{hash(bundle_digest)[:12]}/{shard(param_id,2,2)}/params_{param_id[:8]}/seed_{seed}"

# Token-based invalidation (fine-grained)
sim_path_template = "sims/{hash(model_digest)[:12]}/{shard(param_id,2,2)}/params_{param_id[:8]}/seed_{seed}"

# Aggregation paths
agg_path_template = "aggs/{hash(bundle_digest)[:12]}/target_{target}/agg_{aggregation_id}"
```

## Invalidation Strategies

### Bundle Invalidation (Default)
- Cache key includes `bundle_digest`
- Any change to bundle invalidates all cached results
- Simple and conservative
- Good for development and testing

### Token Invalidation
- Cache key includes `model_digest` from manifest
- Only semantic code changes invalidate cache
- Ignores whitespace, comments, non-functional changes
- Requires integration with modelops-calabaria manifest system

### Schema Instances

```python
# Pre-defined schemas
BUNDLE_INVALIDATION_SCHEMA = ProvenanceSchema(
    name="bundle",
    sim_path_template="sims/{hash(bundle_digest)[:12]}/..."
)

TOKEN_INVALIDATION_SCHEMA = ProvenanceSchema(
    name="token",
    sim_path_template="sims/{hash(model_digest)[:12]}/..."
)
```

## Storage Layout

### Simulation Result Directory
```
{storage_dir}/bundle/v1/sims/a1b2c3d4e5f6/ab/cd/params_abcd1234/seed_42/
├── metadata.json       # Task metadata (bundle_ref, params, seed)
├── manifest.json       # Optional bundle manifest with model digests
├── result.json         # SimReturn metadata (task_id, sim_root)
├── artifact_infections.arrow  # Output artifact (Arrow IPC format)
└── artifact_deaths.arrow       # Output artifact
```

### Aggregation Result Directory
```
{storage_dir}/bundle/v1/aggs/a1b2c3d4e5f6/target_deaths/agg_1234abcd/
├── metadata.json       # Task metadata
├── result.json         # AggregationReturn (loss, diagnostics)
└── artifact_*.arrow    # Optional aggregated outputs
```

## Integration Points

### Execution Environments

```python
class IsolatedWarmExecEnv:
    def __init__(self, ..., storage_dir: Path):
        self.provenance = ProvenanceStore(storage_dir, schema)

    def run(self, task: SimTask) -> SimReturn:
        # Check cache first
        stored = self.provenance.get_sim(task)
        if stored:
            return stored

        # Execute and store
        result = self._execute(task)
        self.provenance.put_sim(task, result)
        return result
```

### CLI Integration

```bash
# List cached results
mops results list --type sim --limit 20

# Show result details
mops results show /path/to/result --format summary

# Clear cache for schema
mops results clear --schema bundle --force

# Storage statistics
mops results stats
```

## Manifest Integration

The system integrates with modelops-contracts manifest types:

```python
@dataclass
class BundleManifest:
    bundle_digest: str      # Content hash of entire bundle
    bundle_ref: str         # OCI reference
    models: Dict[str, ModelManifest]

@dataclass
class ModelManifest:
    model_digest: str       # Semantic hash of model code
    model_name: str
    parameters: List[str]
```

SimTask now includes optional `bundle_manifest` field and `model_digest` property for token-based invalidation.

## Migration Path

### From SimulationCache + CAS

1. **Remove dependencies**:
   - Delete `cache.py`, `cache_codec.py`, `provenance_scheme.py`
   - Remove CAS imports from execution environments

2. **Update execution environments**:
   - Replace `cas` parameter with `storage_dir`
   - Use ProvenanceStore instead of separate cache/CAS

3. **Update WorkerPlugin**:
   - Remove `_make_cas()` method
   - Pass `storage_dir` to execution environments

### Data Migration

Old cache data is not automatically migrated. Users can:
- Start fresh (recommended for development)
- Write migration script if needed (not provided in MVP)

## Performance Considerations

### Sharding
- Path sharding prevents filesystem bottlenecks
- `shard(param_id,2,2)` creates 256 directories at each level
- Balances between too many files per directory and too deep nesting

### Memory Usage
- MVP loads all artifacts into memory
- Suitable for typical simulation outputs (< 100MB)
- Future: Add streaming for large artifacts

### Disk Usage
- No deduplication of identical outputs
- Each result stored separately
- Use `mops results clear` to manage disk space

## Future Enhancements

1. **Streaming Large Artifacts**
   - Add chunked reading/writing
   - Support artifacts > memory

2. **Result Querying**
   - Add SQL/DuckDB index for complex queries
   - Search by parameter values, loss ranges, etc.

3. **Distributed Storage**
   - S3/Azure Blob backend option
   - Shared cache across workers

4. **Compression**
   - Automatic gzip for artifacts
   - Configurable compression levels

5. **Garbage Collection**
   - TTL-based expiration
   - LRU eviction policies

## Testing

The ProvenanceStore is tested through:
- Unit tests for ProvenanceSchema DSL
- Integration tests with execution environments
- CLI command tests for results management

Key test scenarios:
- Path template rendering with all DSL functions
- Cache hit/miss behavior
- Schema isolation (different schemas don't interfere)
- Concurrent access (thread-safe operations)

## Security Considerations

- No user input in path templates (prevents path traversal)
- Checksums validate artifact integrity
- File permissions follow system defaults
- No automatic code execution from cached results