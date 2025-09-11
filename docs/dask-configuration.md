# Dask Worker Configuration Guide

## Overview

This document explains how Dask worker configuration flows through the ModelOps infrastructure stack and provides best practices for configuring workers for different workload types.

## Configuration Lineage: Azure → Kubernetes → Dask

### 1. User Configuration (`examples/workspace.yaml`)

The configuration starts with the workspace YAML specification:

```yaml
workers:
  replicas: 4        # Number of Kubernetes pods
  processes: 2       # Number of Dask worker processes per pod
  threads: 1         # Number of threads per worker process
  image: ghcr.io/institutefordiseasemodeling/modelops-dask-worker:latest
  resources:
    requests:
      memory: "4Gi"
      cpu: "2"
    limits:
      memory: "4Gi"
      cpu: "2"
```

### 2. Pulumi Infrastructure (`src/modelops/infra/components/workspace.py`)

Pulumi translates the configuration into Kubernetes resources:

```python
# Creates a Kubernetes Deployment
k8s.apps.v1.Deployment(
    spec=k8s.apps.v1.DeploymentSpecArgs(
        replicas=worker_count,  # Number of pods
        template=k8s.core.v1.PodTemplateSpecArgs(
            spec=k8s.core.v1.PodSpecArgs(
                containers=[
                    k8s.core.v1.ContainerArgs(
                        command=[
                            "dask-worker",
                            "tcp://dask-scheduler:8786",
                            "--nprocs", str(workers_config.get("processes", 1)),
                            "--nthreads", str(workers_config.get("threads", 2)),
                            "--memory-limit", memory_limit
                        ],
```

### 3. Kubernetes Deployment

The resulting Kubernetes deployment creates:
- **N pods** based on `replicas`
- Each pod runs **1 container**
- Each container executes the `dask-worker` command with specified flags

### 4. Dask Worker Process Architecture

```
Azure AKS Cluster
    ↓
K8s Node Pool (modelops.io/role: cpu)
    ↓
K8s Deployment (replicas: 4)
    ↓
4 Pods
    ↓
1 Container per Pod
    ↓
M Dask Worker Processes per Container (--nprocs)
    ↓
T Threads per Worker Process (--nthreads)
```

## Process vs Thread Tradeoffs

### When to Use Threads (`--nthreads`)

**Best for:** NumPy, Pandas, Scikit-Learn, and other libraries that release Python's GIL

**Advantages:**
- Shared memory between tasks
- Lower memory overhead
- Faster inter-task communication
- Efficient for vectorized operations

**Configuration:**
```yaml
workers:
  processes: 1
  threads: 4  # Multiple threads, single process
```

### When to Use Processes (`--nprocs`)

**Best for:** Pure Python code, string manipulation, simulation models

**Advantages:**
- Bypasses Python's GIL limitations
- True parallelism for Python code
- Process isolation (important for the new runner design)
- Better for CPU-bound Python operations

**Configuration:**
```yaml
workers:
  processes: 4
  threads: 1  # Multiple processes, single thread each
```

## Recommended Configurations

### For Simulation Workloads (ModelOps Default)

Since simulations typically involve pure Python model code that cannot release the GIL:

```yaml
workers:
  replicas: 2      # Fewer pods, more processes per pod
  processes: 4     # More processes for GIL bypass
  threads: 1       # Single thread per process
  resources:
    requests:
      memory: "4Gi"  # Total per pod
      cpu: "4"       # Matches process count
    limits:
      memory: "4Gi"
      cpu: "4"
```

**Per-process memory:** When using `--nprocs > 1`, Dask automatically divides the memory limit:
- Pod memory: 4Gi
- Processes: 4
- Per-process memory: 1Gi

### For Data Science Workloads

For NumPy/Pandas heavy workloads:

```yaml
workers:
  replicas: 4
  processes: 1     # Single process
  threads: 4       # Multiple threads for GIL-released operations
  resources:
    requests:
      memory: "8Gi"  # More memory for data operations
      cpu: "4"
```

### For Mixed Workloads

Start with balanced configuration:

```yaml
workers:
  replicas: 3
  processes: 2
  threads: 2
  resources:
    requests:
      memory: "6Gi"
      cpu: "4"
```

## Memory Management

### Important Considerations

1. **Total memory per pod** is divided among processes when `--nprocs > 1`
2. **Memory limit per process** = Pod memory / nprocs
3. **Dask spill thresholds** are per-process:
   - Target: 90% (start spilling to disk)
   - Spill: 95% (aggressive spilling)
   - Pause: 98% (pause execution)

### Example Memory Calculation

```yaml
# Pod configuration
memory: "8Gi"
processes: 4

# Results in:
# - Each process gets 2Gi
# - Spill starts at 1.8Gi per process
# - Pause at 1.96Gi per process
```

## Implications for New Runner Design

The proposed `PooledVenvExecutor` adds another layer of process isolation:

```
Dask Worker Process
    ↓
PooledVenvExecutor (manages subprocess pool)
    ↓
N Subprocess Workers (isolated venvs)
    ↓
Simulation execution
```

### Resource Considerations

With subprocess pools inside Dask workers:

1. **Memory allocation:** Reserve memory for subprocess overhead
2. **CPU allocation:** Consider subprocess CPU usage
3. **Process limits:** System limits on total process count

### Recommended Configuration for Runner

```yaml
workers:
  replicas: 4
  processes: 2       # Moderate process count
  threads: 1         # Single thread (GIL-bound)
  resources:
    requests:
      memory: "8Gi"  # Extra memory for subprocess overhead
      cpu: "4"
  env:
    - name: MODELOPS_SUBPROCESS_POOL_SIZE
      value: "2"     # Subprocesses per Dask worker
```

## Monitoring and Tuning

### Key Metrics to Watch

1. **CPU utilization** per worker process
2. **Memory usage** and spill rates
3. **Task execution time**
4. **GIL contention** (for threaded workers)

### Tuning Strategy

1. Start with recommended configuration
2. Monitor actual workload performance
3. Adjust based on:
   - Task characteristics (CPU vs I/O bound)
   - Memory requirements
   - GIL impact
4. Test changes in staging before production

## Common Issues and Solutions

### Issue: Low CPU utilization with threads

**Symptom:** Multiple threads but low CPU usage
**Cause:** GIL preventing parallel execution
**Solution:** Switch to processes (`--nprocs`)

### Issue: Out of memory errors

**Symptom:** Workers killed by OOM killer
**Cause:** Memory limit too low or too many processes
**Solution:** Increase pod memory or reduce process count

### Issue: High task latency

**Symptom:** Tasks queue despite available workers
**Cause:** Not enough worker processes/threads
**Solution:** Increase replicas or processes per pod

## Future Enhancements

1. **Auto-scaling:** Dynamic worker scaling based on queue depth
2. **Heterogeneous workers:** Different configurations for different task types
3. **GPU workers:** Separate pools for GPU-accelerated tasks
4. **Spot instances:** Cost optimization with preemptible nodes