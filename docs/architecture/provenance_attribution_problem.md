# ProvenanceStore Attribution Problem - Request for Design Advice

## Context

We have a distributed simulation system (ModelOps) that runs thousands of simulations with different parameter combinations. After simulations complete, we need to index and aggregate results for analysis. The system uses a **ProvenanceStore** for caching and storing results.

## Current Architecture

### ProvenanceStore Design

The ProvenanceStore implements **input-addressed storage** (similar to
content-addressed storage, but based on input hash rather than output hash).
Results are stored at deterministic paths based on their inputs:

```
/provenance/
  /token/v1/
    /sims/{bundle_digest[:12]}/{shard(param_id,2,2)}/params_{param_id[:8]}/seed_{seed}/
      metadata.json    # Contains: bundle_ref, params, seed, param_id, timestamp
      result.json      # Contains: task_id, outputs metadata, error info
      artifact_*.arrow # Binary output files

    /aggs/{bundle_digest[:12]}/target_{target}/agg_{aggregation_id}/
      metadata.json    # Contains: bundle_ref, target_entrypoint, param_id, timestamp
      result.json      # Contains: aggregation_id, loss value, diagnostics, n_replicates
```

### Path Generation Logic
Paths are generated deterministically using a DSL-based schema:

```python
class ProvenanceSchema:
    sim_path_template = "sims/{bundle_digest[:12]}/{shard(param_id,2,2)}/params_{param_id[:8]}/seed_{seed}"

    def sim_path(self, bundle_digest, param_id, seed):
        # Returns deterministic path like:
        # "token/v1/sims/abc123/1f/2d/params_1f2d3e4f/seed_42/"
```

### Key Properties
1. **Deterministic paths**: Same inputs (bundle_digest, param_id, seed) always map to the same storage location
2. **Automatic caching**: If a simulation with identical inputs is requested again, we can return the cached result
3. **No duplication**: Each unique simulation is computed and stored exactly once
4. **Bundle-based invalidation**: When code changes (new bundle_digest), results are automatically stored in new locations

## The Problem

We're implementing a result indexing operation that needs to:
1. Collect all simulation/aggregation results
2. Combine losses for each parameter into a single DataFrame/Parquet file
3. **Track which job(s) generated these results**

Here's the attribution challenge:

### Scenario
1. **Monday**: Job_A runs 1000 simulations with param_sets [P1, P2, ..., P1000]
   - Results stored at `/sims/bundle_v1/*/params_*/seed_*/`
   - Currently, no job_id is stored in the results

2. **Tuesday**: Job_B runs 500 simulations, but 200 of them have identical inputs to Job_A
   - 200 simulations hit the cache (results already exist from Job_A)
   - 300 new simulations are computed and stored
   - Job_B benefits from Job_A's work

3. **Wednesday**: We want to index results
   - For auditing: "Which job(s) contributed to these results?"
   - For debugging: "What results did Job_B actually compute vs. reuse?"
   - For analysis: "Show me all results associated with Job_B's run"

### Current State
- The ProvenanceStore has no concept of job_id
- Results are shared across jobs through caching (this is good for efficiency!)
- Once a result is computed, we lose track of who requested it
- The `JobRegistry` tracks jobs separately but doesn't connect to ProvenanceStore results

### Constraints
1. **Must preserve caching**: We don't want to compute the same simulation twice
2. **Must maintain deterministic paths**: The input-addressed storage is core to cache invalidation
3. **Must track attribution**: Need to know which job(s) are associated with each result
4. **Must handle multiple jobs**: A single result might be used by many jobs over time
5. **Backward compatibility**: Existing results don't have job attribution

## The Question

How would you design a system to track job attribution for cached, input-addressed storage results?

Specifically:
- How do we record that multiple jobs might have "used" the same cached result?
- How do we query "all results for job_X" when some were computed by job_X and others were cache hits from earlier jobs?
- Should attribution be stored with the results, separately, or both?
- How do we handle the transition (existing results have no attribution)?

Looking for your thoughts on the cleanest architectural approach that preserves our caching benefits while enabling proper result attribution and indexing.

## Additional Context

Related systems for reference:
- Docker layers: Multiple images can share the same layers
- Git objects: Multiple branches can reference the same commits/trees/blobs
- Build systems: Multiple builds can share cached artifacts
- Nix store: Multiple derivations can depend on the same store paths

Our system is most similar to a build cache where we need to track "which build jobs used which cached artifacts" while still sharing the artifacts efficiently.
