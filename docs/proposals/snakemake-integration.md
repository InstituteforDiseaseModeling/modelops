# Proposal: Snakemake Integration for Pipeline Orchestration

## Summary

Integrate Snakemake as a high-level pipeline orchestrator that runs in K8s and communicates with ModelOps' simulation infrastructure via a lightweight client library. Snakemake handles workflow DAGs (calibration → analysis → visualization), while Dask handles compute-intensive simulation batches.

## Motivation

- **Snakemake** excels at: reproducible workflows, DAG dependencies, file-based I/O, scientific pipelines
- **ModelOps/Dask** excels at: parallel simulation execution, worker pools, warm processes, dynamic task graphs

Combining them gives users the best of both worlds - familiar Snakemake workflows with ModelOps' optimized simulation infrastructure.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  User's Snakemake Workflow (Snakefile)                          │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐     │
│  │ prepare  │ → │ simulate │ → │ analyze  │ → │ visualize│     │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘     │
└────────────────────────┬────────────────────────────────────────┘
                         │ imports
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  modelops-client (lightweight Python package)                    │
│  - SimulationClient class                                        │
│  - Async job submission + polling                                │
│  - Result download from Azure Blob                               │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP/gRPC
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Simulation Service (K8s Deployment + Service)                   │
│  - FastAPI/gRPC endpoint                                         │
│  - Wraps DaskSimulationService                                   │
│  - Job registry integration                                      │
└────────────────────────┬────────────────────────────────────────┘
                         │ Dask protocol
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Existing Dask Infrastructure                                    │
│  - Scheduler (persistent)                                        │
│  - Workers with subprocess pools                                 │
│  - Bundle execution                                              │
└─────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Simulation Service (`modelops-service`)

A persistent K8s Deployment that exposes the simulation infrastructure via HTTP API.

**Endpoints:**

```
POST /v1/simulate
  Request:  { bundle_ref, parameter_sets, n_replicates, targets? }
  Response: { job_id }

GET  /v1/jobs/{job_id}
  Response: { status, progress, results_path? }

GET  /v1/jobs/{job_id}/results
  Response: { parquet_url } or stream parquet directly

POST /v1/simulate/sync
  Request:  { bundle_ref, parameter_sets, ... }
  Response: { results } (blocks until complete - for small jobs)
```

**Implementation:**
- FastAPI app wrapping `DaskSimulationService`
- Connects to existing Dask scheduler
- Uses existing job registry for state tracking
- ~300-500 LOC

**K8s Resources:**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: simulation-service
spec:
  replicas: 1
  template:
    spec:
      containers:
      - name: service
        image: modelops-service:latest
        ports:
        - containerPort: 8080
        env:
        - name: DASK_SCHEDULER_ADDRESS
          value: "tcp://dask-scheduler:8786"
---
apiVersion: v1
kind: Service
metadata:
  name: simulation-service
spec:
  ports:
  - port: 8080
  selector:
    app: simulation-service
```

### 2. Client Library (`modelops-client`)

Lightweight package with minimal dependencies for use in Snakemake workflows.

**Dependencies:** `httpx`, `polars` (optional for result handling)

**API:**

```python
from modelops_client import SimulationClient

# Initialize client
client = SimulationClient(
    service_url="http://simulation-service:8080",  # In-cluster
    # or service_url="https://modelops.myorg.com",  # External
)

# Async submission
job = client.submit(
    bundle_ref="mymodel@sha256:abc123...",
    parameter_sets=[{"beta": 0.1}, {"beta": 0.2}],
    n_replicates=10,
    targets=["targets.loss:mse_target"],
)
print(f"Submitted: {job.job_id}")

# Poll for completion
result = job.wait(timeout=3600, poll_interval=5)

# Get results as DataFrame
df = result.to_polars()  # or .to_parquet("output.parquet")
```

**Snakemake Integration:**

```python
# Snakefile
from modelops_client import SimulationClient

client = SimulationClient(os.environ["SIMULATION_SERVICE_URL"])

rule calibrate:
    input:
        params="config/calibration_params.json"
    output:
        results="results/calibration.parquet"
    run:
        import json
        params = json.load(open(input.params))

        job = client.submit(
            bundle_ref=config["bundle_ref"],
            parameter_sets=params["parameter_sets"],
            n_replicates=params["n_replicates"],
            targets=params["targets"],
        )

        result = job.wait()
        result.to_parquet(output.results)

rule analyze:
    input:
        calibration="results/calibration.parquet"
    output:
        analysis="results/analysis.parquet"
    script:
        "scripts/analyze.py"

rule visualize:
    input:
        analysis="results/analysis.parquet"
    output:
        report="reports/calibration_report.html"
    script:
        "scripts/visualize.py"
```

### 3. Snakemake K8s Execution

Users can run their Snakemake workflow either:

**A. Locally (talking to in-cluster service via port-forward or ingress):**
```bash
kubectl port-forward svc/simulation-service 8080:8080 &
SIMULATION_SERVICE_URL=http://localhost:8080 snakemake --cores 4
```

**B. In-cluster (Snakemake K8s executor):**
```bash
snakemake --kubernetes \
  --default-remote-provider AzBlob \
  --default-remote-prefix mycontainer/workflows \
  --envvars SIMULATION_SERVICE_URL
```

## Implementation Plan

### Phase 1: Simulation Service (1 week)

- [ ] Create `modelops-service` package
- [ ] Implement FastAPI endpoints (submit, status, results)
- [ ] Add K8s Deployment/Service to workspace component
- [ ] Integration tests with existing Dask infrastructure

### Phase 2: Client Library (3-4 days)

- [ ] Create `modelops-client` package (separate repo or monorepo)
- [ ] Implement `SimulationClient` with async support
- [ ] Add result handling (Polars/Parquet)
- [ ] Publish to PyPI or internal registry

### Phase 3: Snakemake Examples (2-3 days)

- [ ] Create example Snakefile for calibration workflow
- [ ] Document local execution with port-forward
- [ ] Document K8s execution with Snakemake executor
- [ ] Add to examples/ directory

### Phase 4: Production Hardening (1 week)

- [ ] Add authentication (API keys or service-to-service)
- [ ] Add rate limiting
- [ ] Add OpenTelemetry tracing
- [ ] Add health checks and readiness probes

## Alternatives Considered

### A. Snakemake calling `mops` CLI directly
- Simpler but requires mops installed in Snakemake environment
- Shell overhead for each rule
- No async/parallel job submission

### B. Replace Dask with Snakemake K8s executor
- Would lose warm process pools and dynamic task graphs
- Each simulation = one K8s job (high overhead)
- Not suitable for parameter sweeps with 1000s of combinations

### C. Dask-only with custom DAG
- Dask can do DAGs but less ergonomic than Snakemake
- No file-based workflow tracking
- Harder for scientists to adopt

## Success Criteria

1. User can write a Snakefile that runs calibration jobs via ModelOps
2. Workflow runs locally (port-forward) or in K8s (Snakemake executor)
3. Results are stored in Azure Blob and accessible as Parquet
4. Job status visible in `mops jobs list`
5. < 5 second overhead per simulation batch submission

## Open Questions

1. **Authentication**: API keys? Service accounts? Azure AD?
2. **Result storage**: Return URLs to blob storage or stream data through service?
3. **Scope**: Should client support calibration jobs too, or just simulations?
4. **Naming**: `modelops-client` vs `mops-client` vs `modelops-sdk`?

## References

- [Snakemake K8s Executor](https://snakemake.readthedocs.io/en/master/executing/cloud.html)
- [FastAPI](https://fastapi.tiangolo.com/)
- [Current ModelOps Architecture](../ARCHITECTURE.md)
