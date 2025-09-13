# Fan-out vs Grouped Replicates for ABM Simulations on Dask
_A technical note on scheduling strategy, cost models, memory requirements, and practical guidance_

**Status:** MVP decision guide • **Scope:** large-timestep agent-based epidemiological simulations (ABMs) executed via ModelOps warm process pool + Dask

---

## 1) Executive summary

- **Two viable patterns**  
  - **Fan-out:** scatter replicates across many workers for parallel wall-time speedup.  
  - **Grouped:** run all replicates for a parameter set **on one worker** (same warm process), then **aggregate on that worker**.  
- **Rule of thumb (ABMs):** when **replicate runtime `t_exec` is large** (minutes→hours) **and outputs are non-trivial**, prefer **Grouped** for MVP reliability and lower network pressure. Use **Fan-out** when outputs are small and cluster parallelism can be fully utilized.
- **Current implementation:** Fan-out for simulations, single-worker aggregation (hybrid approach)

---

## 2) Memory Requirements by Scale

### Critical Memory Thresholds

| Replicates | Output/Sim | Total Memory | Strategy | Risk Level | Notes |
|------------|------------|--------------|----------|------------|-------|
| 100 | 1MB | 100MB | Either | ✅ Safe | Trivial for any worker |
| 100 | 10MB | 1GB | Either | ✅ Safe | Comfortable margin |
| 1000 | 1MB | 1GB | Either | ✅ Safe | Still manageable |
| 1000 | 10MB | **10GB** | Fanout recommended | ⚠️ Warning | Single worker aggregation stressed |
| 100 | 100MB | **10GB** | Fanout recommended | ⚠️ Warning | Large output concern |
| 10000 | 1MB | **10GB** | Fanout required | ⚠️ Warning | Scale requires distribution |
| 10000 | 10MB | **100GB** | Tree aggregation | ❌ Critical | Exceeds single worker capacity |
| 1000 | 100MB | **100GB** | Tree aggregation | ❌ Critical | Requires multi-level aggregation |

### Memory Formula
```
Memory_aggregation = N_replicates × Size_per_SimReturn + Overhead
```
Where overhead includes:
- JSON-RPC serialization buffers (~2x data size during peak)
- Dask task metadata (~KB per task)
- Python object overhead (~20-30% for complex structures)

### Practical Limits
- **8GB worker**: Safe up to ~500 replicates @ 10MB each
- **16GB worker**: Safe up to ~1000 replicates @ 10MB each  
- **32GB worker**: Safe up to ~2000 replicates @ 10MB each

---

## 3) Symbols & assumptions

- `R` – number of replicates per parameter set  
- `W` – available workers (effective parallel slots `W_eff ≤ W`)  
- `t_exec` – time to run one replicate (ABM core)  
- `t_spin` – one-time warm-process cost (venv, imports) per worker/bundle  
- `t_rpc` – JSON-RPC + (de)serialization per call overhead  
- `t_io` – artifact materialization/hydration (CAS ↔ inline) + local I/O  
- `t_sched` – Dask scheduling & bookkeeping overhead per group  
- `t_agg` – target alignment/evaluation time on the worker

Assume warm cache hits (`t_spin ≈ 0`) after first run for a bundle; cold start includes `t_spin` once per participating worker.

---

## 4) Cost models

### 4.1 Fan-out (scatter replicates across workers)

Replicates run concurrently in batches of size `W_eff`:

$$
T_{\text{fanout}} \approx \left\lceil \frac{R}{W_{\text{eff}}}\right\rceil \cdot t_{\text{exec}} \, + \, t_{\text{sched}} \, + \, t_{\text{io,gather}} \, + \, t_{\text{agg}} \, + \, \mathbf{1}_{\text{cold}}\cdot N_{\text{workers}}\cdot t_{\text{spin}}
$$

- **Pros:** parallel speedup up to `min(R, W_eff)`.
- **Cons:** higher **network traffic** (gathering results), more warm-process cold starts if many workers participate, potential variability in replicate ordering.

### 4.2 Grouped (pin all replicates to one worker)

$$
T_{\text{group}} \approx t_{\text{spin}} \, + \, R\cdot(t_{\text{exec}} + t_{\text{rpc}} + t_{\text{io}}) \, + \, t_{\text{agg}}
$$

- **Pros:** **no cross-worker transfers**, maximizes **warm cache locality**, simple failure semantics, deterministic replicate ordering.
- **Cons:** no within-set parallelism (but many sets can still run in parallel across workers).

### 4.3 Intuition / crossover

Normalize by `t_exec`. Let $\rho = t_{\text{spin}}/t_{\text{exec}}$, $\theta = (t_{\text{sched}} + t_{\text{io,gather}})/t_{\text{exec}}$.  
Fan-out becomes favorable when

$$
\left\lceil \frac{R}{W_{\text{eff}}}\right\rceil + \theta \;<\; R\cdot\left(1 + \frac{t_{\text{rpc}} + t_{\text{io}}}{t_{\text{exec}}}\right) + \rho
$$

In ABMs, usually $t_{\text{exec}}$ is **dominant**, $t_{\text{rpc}}$ and $t_{\text{io}}$ are smaller but the **gather I/O** can be noticeable for large outputs. If `W_eff` is modest and artifacts are heavy, **Grouped** often wins for predictability.

---

## 5) Current Implementation (Hybrid)

ModelOps currently implements a **hybrid approach**:

```python
# Simulations fan out across workers
replicate_futures = self.client.map(
    _worker_run_task,
    tasks,
    pure=False,
    key=keys  # Distributed across available workers
)

# Aggregation runs on single worker
agg_future = self.client.submit(
    gather_and_aggregate,
    replicate_futures,
    target_entrypoint,
    bundle_ref,
    key=f"agg-{param_id[:8]}",
    pure=False
)
```

This provides:
- **Parallelism** for simulation execution
- **Data locality** for aggregation (gather happens on worker, not client)
- **Memory bottleneck** at aggregation worker for large replicate sets

---

## 6) Dask execution patterns

### 6.1 Grouped (future option)

1. Submit `R` replicate tasks **to the same worker** (via actor or worker restriction).  
2. Worker executes all replicates sequentially in same warm process.  
3. **Aggregation runs on that worker** without any data transfer.  
4. Only the small **AggregationReturn** (loss + diagnostics) returns to the client.

**Best for:**
- Very large outputs (100MB+ per replicate)
- Network-constrained environments
- Deterministic execution requirements

### 6.2 Fan-out with Tree Aggregation (future enhancement)

1. Scatter replicates across the cluster.  
2. Submit **multiple partial aggregation tasks** in a tree structure.  
3. Each aggregates a subset (e.g., 8 replicates).
4. Final aggregation combines partial results.

**Best for:**
- Very large replicate counts (1000+)
- Medium-sized outputs (1-10MB)
- Maximum parallelism requirements

---

## 7) Engineering Trade-offs

| Aspect | Current (Fanout Sim + Single Agg) | Full Grouped | Tree Aggregation |
|--------|-----------------------------------|--------------|------------------|
| **Parallelism** | High for sims, none for agg | Low (sequential) | High throughout |
| **Memory Peak** | At aggregation worker | Distributed | Distributed |
| **Network Traffic** | Medium (worker-to-worker) | Minimal | Medium |
| **Complexity** | Simple | Simpler | Complex |
| **Failure Handling** | Partial retry possible | All-or-nothing | Complex retry |
| **Warm Cache** | Multiple workers | Single worker | Multiple workers |
| **Best Scale** | 100s of replicates | 10s of replicates | 1000s of replicates |

---

## 8) Mapping to real workloads (order-of-magnitude)

Practitioner reports (paraphrased) suggest a broad range:
- **Compact models (non-spatial, lighter agents):** often **single-digit minutes** per replicate.
- **Heavier agent models or large geographies:** commonly **tens of minutes** per replicate.
- **Full-scale or continent-level scenarios:** **~hour-ish** per replicate is a reasonable planning number without additional shortcuts.

Interpretation for strategy:
- For **hour-scale** runs with **<100 replicates**, **Grouped** keeps data on one worker and reduces orchestration noise.
- For **5–15 minute** runs with **100-1000 replicates** and modest outputs, current **hybrid** approach works well.
- For **any scale** with **1000+ replicates**, **tree aggregation** becomes necessary.

---

## 9) API & wire compatibility (MVP → future)

- **ReplicateSet:** groups `R` `SimTask`s for the same params.  
- **AggregationTask:** `{ bundle_ref, target_entrypoint, sim_returns[, target_data, outputs] }`  
- **AggregationReturn:** `{ aggregation_id, loss, diagnostics, outputs?, n_replicates }`

Both patterns use the **same wire**:
- Simulation and aggregation both execute inside the **same warm subprocess** via entrypoints.  
- Calabaria provides the **target evaluator** (alignment + loss) as a standard entrypoint or entry-point group.

This makes **Grouped**, **Fan-out**, and **Tree** patterns interchangeable at the service layer.

---

## 10) Practical tuning checklist

- **Warm cache:** Run a tiny dry-run to populate venvs (`t_spin → 0` thereafter).
- **Artifacts:** Limit aggregation inputs via `AggregationTask.outputs` to the minimal set (avoid big tables in JSON-RPC).
- **Concurrency:** For current hybrid, increase **the number of parameter sets** in flight.
- **Memory monitoring:** Watch aggregation worker memory usage via Dask dashboard.
- **Failure policy:** Define behavior for partial replicate failures (drop vs `+∞` loss with diagnostics).
- **Chunking:** For very large sets, consider breaking into multiple smaller ReplicateSets.

---

## 11) Recommendations

### Immediate (MVP)
- **Keep current hybrid approach** for typical workloads (<1000 replicates, <10MB outputs)
- **Monitor aggregation worker memory** in production
- **Document memory limits** clearly for users

### Near-term
- **Add grouped mode option** for very large outputs (>100MB per replicate)
- **Implement memory checks** before aggregation to fail fast

### Future
- **Tree aggregation** for 1000+ replicate scenarios
- **Adaptive strategy selection** based on replicate count and estimated output size
- **Streaming aggregation** for unlimited scale

---

## Appendix A – Symbols

| Symbol | Meaning |
|---|---|
| `R` | Replicates per parameter set |
| `W`, `W_eff` | Physical workers, effective parallel slots |
| `t_exec` | Replicate runtime (ABM core) |
| `t_spin` | Warm process spin-up per worker/bundle |
| `t_rpc` | JSON-RPC + serialization overhead |
| `t_io` | Artifact materialization/hydration + local I/O |
| `t_sched` | Dask scheduling overhead per group |
| `t_agg` | Aggregation time (alignment + loss) |
| `T_fanout`, `T_group` | Total wall time estimates |

---

## Appendix B – Dask Task Naming Convention

To ensure proper grouping in the Dask dashboard, all task keys must use **hyphens** as the primary delimiter:

```python
# Good - groups properly
"sim-8d1aa963-1"    # Groups as "sim"
"agg-173c2cf0"      # Groups as "agg"

# Bad - each task becomes its own group
"sim_8d1aa963_1"    # Groups as "sim_8d1aa963_1" (entire key)
"agg_173c2cf0"      # Groups as "agg_173c2cf0" (entire key)
```

Dask uses the substring before the first hyphen as the task group name for visualization and progress tracking.

---

_Questions welcome; this document is meant to be living as we gather more profiling data._