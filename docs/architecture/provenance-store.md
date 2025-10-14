# ProvenanceStore Architecture

## Overview

The ProvenanceStore is ModelOps' unified storage system for simulation and aggregation results. It provides intelligent caching, automatic invalidation, and incremental computation through a declarative schema system. This document describes the architecture, implementation, and practical usage of the provenance system.

## Key Concepts

### Input-Addressed Storage
- **Storage paths derived from inputs**: Bundle/model digest, parameters, seed
- **Automatic caching**: Results are cached based on inputs, not job IDs
- **Incremental computation**: Only missing results are computed
- **Content validation**: Results include checksums for integrity

### ProvenanceSchema System
- **Declarative path templates**: Define how results are organized on disk
- **Flexible invalidation**: Different schemas for different caching strategies
- **Schema versioning**: Support for migrations and upgrades
- **Isolation**: Different schemas create separate namespaces

## Architecture

```
ProvenanceStore
├── ProvenanceSchema (declarative path templates)
│   ├── DSL for path generation
│   ├── Invalidation strategy encoded in paths
│   └── Schema versioning for migrations
│
├── Storage Operations
│   ├── get_sim() / put_sim()     # Simulation results
│   ├── get_agg() / put_agg()     # Aggregation results
│   └── list_results()            # Query stored results
│
└── Storage Layout
    └── {storage_dir}/
        └── {schema_name}/        # e.g., "token" or "bundle"
            └── v{version}/        # Schema version
                ├── sims/          # Simulation results
                └── aggs/          # Aggregation results
```

## ProvenanceSchema DSL

The schema uses a Domain-Specific Language for generating storage paths:

### DSL Functions

| Function | Description | Example |
|----------|-------------|---------|
| `{var}` | Direct variable interpolation | `{seed}` → `42` |
| `{var[:n]}` | Variable with slicing | `{param_id[:8]}` → `abcd1234` |
| `{hash(var)[:n]}` | Hash of variable (BLAKE2b) | `{hash(bundle_digest)[:12]}` → `a1b2c3d4e5f6` |
| `{shard(var,d,w)}` | Sharded path for filesystem efficiency | `{shard(param_id,2,2)}` → `ab/cd` |

### Built-in Schemas

#### Token Invalidation Schema (Default)
```python
TOKEN_INVALIDATION_SCHEMA = ProvenanceSchema(
    name="token",
    sim_path_template="sims/{hash(model_digest)[:12]}/{shard(param_id,2,2)}/params_{param_id[:8]}/seed_{seed}",
    agg_path_template="aggs/{hash(model_digest)[:12]}/target_{target}/agg_{aggregation_id}"
)
```
- **Cache key**: Based on `model_digest` (semantic code hash)
- **Fine-grained invalidation**: Only affected models are invalidated
- **Use case**: Production runs where code stability matters

#### Bundle Invalidation Schema
```python
BUNDLE_INVALIDATION_SCHEMA = ProvenanceSchema(
    name="bundle",
    sim_path_template="sims/{hash(bundle_digest)[:12]}/{shard(param_id,2,2)}/params_{param_id[:8]}/seed_{seed}",
    agg_path_template="aggs/{hash(bundle_digest)[:12]}/target_{target}/agg_{aggregation_id}"
)
```
- **Cache key**: Based on `bundle_digest` (entire bundle hash)
- **Coarse-grained invalidation**: Any bundle change invalidates all
- **Use case**: Development and testing

## Storage Layout Examples

### Simulation Result Directory
```
/tmp/modelops/provenance/token/v1/sims/666d6a4f303d/ab/cd/params_abcd1234/seed_42/
├── metadata.json       # Task metadata (bundle_ref, params, seed)
├── manifest.json       # Bundle manifest with model digests
├── result.json         # SimReturn (task_id, outputs, diagnostics)
├── artifact_infections.arrow  # Output artifact (Arrow IPC format)
└── artifact_deaths.arrow      # Another output artifact
```

### Aggregation Result Directory
```
/tmp/modelops/provenance/token/v1/aggs/666d6a4f303d/target_targets.prevalence:prevalence_target/agg_a0c60492a3ed2d42/
├── metadata.json       # Aggregation metadata
├── result.json         # AggregationReturn (loss, diagnostics, n_replicates)
└── artifact_*.arrow    # Optional aggregated outputs
```

## Practical Examples

### Example 1: Changing Replicate Count

**Scenario**: You have a study.json with 3 replicates, run it, then update to 200 replicates.

**What happens**:
1. First run with 3 replicates:
   - Computes results for seeds 1-3
   - Stores in `token/v1/sims/.../seed_1/`, `seed_2/`, `seed_3/`
   - Creates aggregation with 3 results

2. Update study.json to 200 replicates and rerun:
   - ProvenanceStore checks existing results
   - Finds seeds 1-3 already computed (cache hits)
   - Only computes seeds 4-200 (cache misses)
   - Creates NEW aggregation with all 200 results

**Result paths**:
```bash
# After first run (3 replicates)
token/v1/sims/666d6a4f303d/.../seed_1/result.json  # Exists
token/v1/sims/666d6a4f303d/.../seed_2/result.json  # Exists
token/v1/sims/666d6a4f303d/.../seed_3/result.json  # Exists
token/v1/aggs/666d6a4f303d/.../agg_074ae7a2/result.json  # 3 replicates

# After second run (200 replicates)
token/v1/sims/666d6a4f303d/.../seed_1/result.json  # Reused
token/v1/sims/666d6a4f303d/.../seed_2/result.json  # Reused
token/v1/sims/666d6a4f303d/.../seed_3/result.json  # Reused
token/v1/sims/666d6a4f303d/.../seed_4/result.json  # New
...
token/v1/sims/666d6a4f303d/.../seed_200/result.json # New
token/v1/aggs/666d6a4f303d/.../agg_9f8e7d6c/result.json  # 200 replicates
```

### Example 2: Model Code Change

**Scenario**: You modify your simulation model code.

**With Token Schema**:
- Model change → new `model_digest`
- New path: `sims/{new_hash}/.../`
- All results recomputed for that model
- Other unchanged models keep cached results

**With Bundle Schema**:
- Any change → new `bundle_digest`
- New path: `sims/{new_hash}/.../`
- ALL results recomputed (more conservative)

### Example 3: Finding Your Results

After running `make submit`, results are stored on worker pods:

```bash
# Check results on Kubernetes pods
kubectl exec deployment/dask-workers -n modelops-dask-dev -- \
  find /tmp/modelops/provenance -name "result.json" -type f | head -5

# Sync results to local machine
make sync-results  # Copies from pods to local results/ directory

# Navigate result structure
cd results/dask-workers-*/token/v1/
ls sims/  # Simulation results by model digest
ls aggs/  # Aggregation results

# View a specific result
cat aggs/*/target_*/agg_*/result.json | jq .
```

## Configuration

### Default Configuration
- **Schema**: `TOKEN_INVALIDATION_SCHEMA` (default)
- **Storage Directory**: `/tmp/modelops/provenance` on workers
- **Environment Variable**: `MODELOPS_STORAGE_DIR`

### Switching Schemas

Currently, the schema is hardcoded in `src/modelops/services/provenance_schema.py`:
```python
DEFAULT_SCHEMA = TOKEN_INVALIDATION_SCHEMA
```

To use bundle-based invalidation, modify:
```python
DEFAULT_SCHEMA = BUNDLE_INVALIDATION_SCHEMA
```

### Custom Schemas

Create custom invalidation strategies:
```python
CUSTOM_SCHEMA = ProvenanceSchema(
    name="custom",
    version=1,
    sim_path_template="sims/{job_id}/{param_id}/seed_{seed}",
    agg_path_template="aggs/{job_id}/target_{target}"
)
```

## Performance Considerations

### Sharding Strategy
- Path sharding prevents filesystem bottlenecks
- `shard(param_id,2,2)` creates 256 directories (16×16)
- Balances file distribution vs directory depth

### Cache Efficiency
- **Token schema**: Fine-grained caching, minimal recomputation
- **Bundle schema**: Simple but conservative, more recomputation
- **Trade-off**: Choose based on development vs production needs

### Disk Usage
- No deduplication of identical outputs
- Each result stored separately
- Monitor with: `du -sh /tmp/modelops/provenance/`

## Integration Points

### Worker Plugin
```python
class ModelOpsWorkerPlugin:
    def setup(self, worker):
        # Storage directory for provenance
        storage_dir = Path(config.storage_dir)

        # Create execution environment with provenance
        exec_env = IsolatedWarmExecEnv(
            storage_dir=storage_dir,
            provenance_schema=DEFAULT_SCHEMA
        )
```

### Execution Environment
```python
class IsolatedWarmExecEnv:
    def __init__(self, storage_dir: Path, provenance_schema=None):
        # Create provenance store with schema
        self.provenance = ProvenanceStore(
            storage_dir=storage_dir,
            schema=provenance_schema or DEFAULT_SCHEMA
        )

    def run(self, task: SimTask) -> SimReturn:
        # Check cache first
        stored = self.provenance.get_sim(task)
        if stored:
            return stored  # Cache hit!

        # Execute and store
        result = self._execute(task)
        self.provenance.put_sim(task, result)
        return result
```

## Troubleshooting

### Results Not Where Expected
1. Check which schema is active (look for `token/` vs `bundle/` in paths)
2. Verify model_digest hasn't changed unexpectedly
3. Check pod logs for storage errors

### Cache Not Being Used
1. Ensure same parameters generate same `param_id`
2. Check if model code changed (new `model_digest`)
3. Verify storage directory is persistent on workers

### Disk Space Issues
1. Results accumulate over time
2. Each schema version creates separate storage
3. Clean periodically or increase worker disk allocation

## Future Enhancements

### Planned Features
1. **CLI Commands**: `mops results list/show/clear`
2. **S3/Azure Blob Backend**: Distributed storage option
3. **Compression**: Automatic gzip for large artifacts
4. **Garbage Collection**: TTL-based expiration policies

### Under Consideration
1. **Content deduplication**: Store identical outputs once
2. **Streaming**: Support artifacts larger than memory
3. **SQL Index**: Complex queries over cached results
4. **Web UI**: Browse and visualize cached results

## Security Considerations

- **Path injection**: Templates prevent user input in paths
- **Checksums**: Validate artifact integrity
- **Permissions**: Follow system defaults
- **Isolation**: Schemas provide namespace isolation

## Summary

The ProvenanceStore provides:
- **Automatic caching** based on inputs, not job IDs
- **Incremental computation** for changed parameters
- **Flexible invalidation** through declarative schemas
- **Transparent operation** with no user intervention needed

This system enables efficient re-runs, parameter sweeps, and iterative development while maintaining full reproducibility and provenance tracking.