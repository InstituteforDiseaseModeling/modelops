# Telemetry System Design

**Author**: Claude (for review by VSB + colleague)
**Date**: 2025-01-04
**Status**: Proposed

## Executive Summary

Add lightweight telemetry to ModelOps to capture execution timing and performance metrics. Currently **zero systematic metrics collection exists** - this design fills that gap with minimal overhead (<1%) and no breaking changes.

## Goals

### Primary Goals
1. **Capture simulation execution timing**: How long do simulations take? Bundle loading? Cache lookups?
2. **Track calibration performance**: Iteration timing, ask/tell durations, convergence metrics
3. **Enable performance debugging**: Identify bottlenecks in distributed execution
4. **Support reproducibility**: Record execution metadata alongside scientific results

### Non-Goals (Future Work)
- Real-time dashboards (can add later)
- Distributed tracing (OpenTelemetry integration later)
- Log aggregation (separate concern)
- Cost tracking (requires cloud provider APIs)

## Current State

**Zero systematic telemetry exists.** Found:
- Basic logging only (`logger.info()` statements)
- Placeholder comment in `executor.py:55`: `# Future: with metrics.timer("simulation.execute"):`
- `SimReturn.metrics: Optional[Mapping[str, float]]` field exists in contracts but is **always None**

## Proposed Architecture

### Module Location

**Recommendation**: `src/modelops/telemetry/`

**Rationale**:
- NOT a service (services like `ProvenanceStore` provide stateful functionality)
- NOT just a util (substantial enough to warrant its own module)
- Cross-cutting concern similar to `core/` but more specific
- Allows growth (query utilities, exporters, etc.)

**Structure**:
```
src/modelops/telemetry/
├── __init__.py          # Exports TelemetryCollector
├── collector.py         # Core collection logic
├── storage.py           # Persistence layer
└── query.py             # Query utilities (Phase 2)
```

### Core Design: Context Manager Pattern

**Why context managers?**
- Automatic timing (no manual start/stop)
- Exception-safe (guaranteed cleanup in `finally`)
- Pythonic and readable
- Zero overhead when disabled (via Noop pattern)

**Why Noop instead of None?**
- No `if span:` guards needed at call sites
- Cleaner instrumentation code: `span.metrics["key"] = value` always works
- Type safety: span is never Optional
- Singleton pattern = zero allocation overhead when disabled

**Code Sketch**:

```python
# src/modelops/telemetry/collector.py

from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Dict, List, Optional, Any
import time

@dataclass
class NoopSpan:
    """No-op span when telemetry is disabled.

    Implements same interface as TelemetrySpan but drops all data.
    Using a Noop object instead of None lets instrumentation code
    stay clean without guards: span.metrics["key"] = value just works.
    """
    metrics: Dict[str, float] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)

    def duration(self) -> None:
        """Always returns None for disabled telemetry."""
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Returns empty dict for disabled telemetry."""
        return {}

# Singleton - reuse same instance to avoid allocations
NOOP_SPAN = NoopSpan()


@dataclass
class TelemetrySpan:
    """A single timing/metrics span.

    Represents a timed operation with optional metrics and tags.
    """
    name: str
    start_time: float
    end_time: Optional[float] = None
    metrics: Dict[str, float] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)

    def duration(self) -> Optional[float]:
        """Duration in seconds, or None if still running."""
        if self.end_time is None:
            return None
        return self.end_time - self.start_time

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration(),
            "metrics": self.metrics,
            "tags": self.tags,
        }


class TelemetryCollector:
    """Lightweight context-manager based telemetry collector.

    Usage:
        collector = TelemetryCollector()

        with collector.span("simulation.execute", param_id="abc123") as span:
            result = run_simulation()
            span.metrics["cached"] = 1.0 if cached else 0.0

        # Export at end
        telemetry_data = collector.to_dict()
    """

    def __init__(self, enabled: bool = True):
        """Initialize collector.

        Args:
            enabled: If False, spans become no-ops (zero overhead)
        """
        self.enabled = enabled
        self.spans: List[TelemetrySpan] = []
        self._current_span: Optional[TelemetrySpan] = None

    @contextmanager
    def span(self, name: str, **tags):
        """Create a timing span.

        Args:
            name: Hierarchical name (e.g., "simulation.execute")
            **tags: Optional tags for filtering (param_id="abc", iteration="5")

        Yields:
            TelemetrySpan or NoopSpan: Active span for adding metrics
            Always returns a span object (never None), so no guards needed

        Example:
            with collector.span("cache.lookup", key="abc") as span:
                result = cache.get("abc")
                span.metrics["hit"] = 1.0 if result else 0.0  # Works even when disabled!
        """
        if not self.enabled:
            yield NOOP_SPAN
            return

        span = TelemetrySpan(
            name=name,
            start_time=time.perf_counter(),
            tags=tags,
        )

        prev_span = self._current_span
        self._current_span = span

        try:
            yield span
            span.end_time = time.perf_counter()
        except Exception as e:
            span.end_time = time.perf_counter()
            span.tags["error"] = type(e).__name__
            span.tags["error_msg"] = str(e)[:200]
            raise
        finally:
            self.spans.append(span)
            self._current_span = prev_span

    def add_metric(self, key: str, value: float):
        """Add metric to current span (convenience method).

        Args:
            key: Metric name
            value: Numeric value
        """
        if self.enabled and self._current_span:
            self._current_span.metrics[key] = value

    def to_dict(self) -> Dict[str, Any]:
        """Export all telemetry as JSON-compatible dict."""
        return {
            "spans": [s.to_dict() for s in self.spans],
            "total_spans": len(self.spans),
            "total_duration": sum(s.duration() or 0 for s in self.spans),
        }
```

### Storage Strategy

**Follow ProvenanceStore pattern**: Local-first, optional Azure upload

```python
# src/modelops/telemetry/storage.py

from pathlib import Path
import json
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class TelemetryStorage:
    """Persist telemetry data.

    Follows ProvenanceStore pattern:
    1. Write to local filesystem first (atomic)
    2. Optionally upload to Azure Blob Storage
    3. Never fail jobs if telemetry storage fails
    """

    def __init__(
        self,
        storage_dir: Path,
        prov_store: Optional['ProvenanceStore'] = None
    ):
        """Initialize storage.

        Args:
            storage_dir: Local directory (e.g., /tmp/modelops/provenance)
            prov_store: Optional ProvenanceStore for Azure uploads
        """
        self.storage_dir = Path(storage_dir)
        self.prov_store = prov_store

    def save_job_telemetry(
        self,
        job_id: str,
        telemetry: TelemetryCollector,
        job_type: str = "simulation"
    ):
        """Save job-level telemetry.

        Creates:
        - telemetry/jobs/{job_id}/summary.json (aggregate metrics)
        - telemetry/jobs/{job_id}/spans.jsonl (all spans, line-delimited)

        Args:
            job_id: Job identifier
            telemetry: Collected telemetry data
            job_type: "simulation" or "calibration"
        """
        try:
            job_dir = self.storage_dir / "telemetry" / "jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)

            # Write summary (aggregate metrics)
            summary = self._compute_summary(telemetry, job_type)
            summary_path = job_dir / "summary.json"

            # Atomic write
            tmp_path = summary_path.with_suffix(".json.tmp")
            with open(tmp_path, "w") as f:
                json.dump(summary, f, indent=2)
            tmp_path.rename(summary_path)

            # Write spans as JSONL (efficient for querying)
            spans_path = job_dir / "spans.jsonl"
            with open(spans_path, "w") as f:
                for span in telemetry.spans:
                    f.write(json.dumps(span.to_dict()) + "\n")

            logger.info(f"Saved telemetry: {summary_path}")

            # Upload to Azure if configured
            self._upload_to_azure(job_dir, job_id)

        except Exception as e:
            # Never fail jobs on telemetry errors
            logger.warning(f"Failed to save telemetry for {job_id}: {e}")

    def _compute_summary(
        self,
        telemetry: TelemetryCollector,
        job_type: str
    ) -> Dict[str, Any]:
        """Compute aggregate metrics from spans."""
        spans_by_name: Dict[str, list] = {}
        for span in telemetry.spans:
            if span.name not in spans_by_name:
                spans_by_name[span.name] = []
            spans_by_name[span.name].append(span)

        summary = {
            "job_type": job_type,
            "total_spans": len(telemetry.spans),
            "total_duration": sum(s.duration() or 0 for s in telemetry.spans),
            "by_name": {}
        }

        for name, spans in spans_by_name.items():
            durations = [s.duration() for s in spans if s.duration() is not None]

            summary["by_name"][name] = {
                "count": len(spans),
                "total_duration": sum(durations),
                "mean_duration": sum(durations) / len(durations) if durations else None,
                "max_duration": max(durations) if durations else None,
                "min_duration": min(durations) if durations else None,
            }

            # Aggregate metrics from all spans of this type
            all_metrics: Dict[str, List[float]] = {}
            for span in spans:
                for key, value in span.metrics.items():
                    if key not in all_metrics:
                        all_metrics[key] = []
                    all_metrics[key].append(value)

            if all_metrics:
                summary["by_name"][name]["metrics"] = {
                    key: {
                        "mean": sum(values) / len(values),
                        "sum": sum(values),
                        "count": len(values)
                    }
                    for key, values in all_metrics.items()
                }

        return summary

    def _upload_to_azure(self, local_dir: Path, job_id: str):
        """Upload telemetry to Azure (best-effort)."""
        if not self.prov_store:
            return

        if not hasattr(self.prov_store, '_azure_backend') or not self.prov_store._azure_backend:
            return

        try:
            remote_prefix = f"telemetry/jobs/{job_id}"
            self.prov_store._upload_to_azure(local_dir, remote_prefix)
            logger.info(f"Uploaded telemetry to Azure: {remote_prefix}")
        except Exception as e:
            logger.warning(f"Failed to upload telemetry to Azure: {e}")
```

## Integration Points

### 1. SimulationExecutor (High-Level)

**File**: `src/modelops/core/executor.py`

```python
from modelops.telemetry import TelemetryCollector

class SimulationExecutor:
    """Orchestrates simulation execution with telemetry."""

    def __init__(self, exec_env: ExecutionEnvironment):
        self.exec_env = exec_env
        self.telemetry = TelemetryCollector()

    def execute(self, task: SimTask) -> SimReturn:
        """Execute simulation with automatic telemetry.

        Telemetry is attached to SimReturn.metrics field.
        """
        with self.telemetry.span(
            "simulation.execute",
            param_id=task.params.param_id[:8],
            seed=str(task.seed)
        ) as span:

            # Execute via environment
            result = self.exec_env.run(task)

            # Collect metrics (no guards needed - span is never None!)
            span.metrics["cached"] = 1.0 if result.cached else 0.0

            # Attach telemetry to SimReturn
            # This uses existing SimReturn.metrics field!
            from dataclasses import replace
            result = replace(result, metrics={
                "execution_duration": span.duration(),
                "cached": 1.0 if result.cached else 0.0,
                **span.metrics
            })

            return result
```

### 2. IsolatedWarmExecEnv (Detailed Spans)

**File**: `src/modelops/adapters/exec_env/isolated_warm.py`

```python
def run(self, task: SimTask) -> SimReturn:
    """Run simulation with detailed telemetry."""
    telemetry = TelemetryCollector()

    # Phase 1: Provenance lookup
    with telemetry.span("provenance.lookup"):
        if not self.disable_provenance_cache:
            stored = self.provenance.get_sim(task)
            if stored:
                telemetry.add_metric("cache_hit", 1.0)
                # Attach telemetry to cached result
                return replace(stored, metrics=telemetry.to_dict())
        telemetry.add_metric("cache_hit", 0.0)

    # Phase 2: Bundle resolution
    with telemetry.span("bundle.resolve") as span:
        digest, bundle_path = self._resolve_bundle(task.bundle_ref)
        span.tags["digest"] = digest[:12]

    # Phase 3: Process acquisition
    with telemetry.span("process.acquire"):
        process = self._process_manager.get_process(digest, bundle_path)

    # Phase 4: Remote execution (main bottleneck)
    with telemetry.span("simulation.run_remote"):
        result = process.execute(...)

    # Phase 5: Provenance storage
    with telemetry.span("provenance.store"):
        self.provenance.put_sim(task, result)

    # Attach detailed telemetry
    return replace(result, metrics=telemetry.to_dict())
```

### 3. Calibration Wire (Iteration Tracking)

**File**: `modelops-calabaria/src/modelops_calabaria/calibration/wire.py`

```python
def calibration_wire(
    job: CalibrationJob,
    sim_service: SimulationService,
    prov_store=None
) -> None:
    """Run calibration with iteration-level telemetry."""
    from modelops.telemetry import TelemetryCollector

    job_telemetry = TelemetryCollector()

    with job_telemetry.span("calibration.job", job_id=job.job_id):

        iteration = 0
        while not adapter.finished() and iteration < job.max_iterations:
            iteration += 1

            with job_telemetry.span(
                "calibration.iteration",
                iteration=str(iteration)
            ) as iter_span:

                # Ask phase
                with job_telemetry.span("algorithm.ask") as span:
                    param_sets = adapter.ask(n=batch_size)
                    span.metrics["n_params"] = len(param_sets)

                # Submit phase
                with job_telemetry.span("simulation.submit_batch") as span:
                    futures = [...]  # submission logic
                    span.metrics["n_submitted"] = len(futures)

                # Gather phase (main bottleneck)
                with job_telemetry.span("simulation.gather") as span:
                    results = sim_service.gather(futures)
                    span.metrics["n_results"] = len(results)

                    # Aggregate timing from individual SimReturns
                    if results and hasattr(results[0], 'metrics') and results[0].metrics:
                        durations = [
                            r.metrics.get("execution_duration", 0)
                            for r in results
                            if r.metrics
                        ]
                        if durations:
                            span.metrics["mean_sim_duration"] = sum(durations) / len(durations)
                            span.metrics["max_sim_duration"] = max(durations)
                            span.metrics["total_sim_duration"] = sum(durations)

                # Tell phase
                with job_telemetry.span("algorithm.tell") as span:
                    trial_results = convert_to_trial_result(...)
                    adapter.tell(trial_results)
                    span.metrics["n_trials"] = len(trial_results)

                    # Track best loss
                    completed = [t for t in trial_results if t.status == TrialStatus.COMPLETED]
                    if completed:
                        span.metrics["min_loss"] = min(t.loss for t in completed)

                iter_span.metrics["total_trials"] = len(trial_results)

    # Save telemetry with calibration results
    from modelops.telemetry import TelemetryStorage
    if prov_store:
        storage = TelemetryStorage(
            storage_dir=Path("/tmp/modelops/provenance"),
            prov_store=prov_store
        )
        storage.save_job_telemetry(job.job_id, job_telemetry, job_type="calibration")
```

### 4. Job Runner (Top-Level)

**File**: `src/modelops/runners/job_runner.py`

```python
def run_simulation_job(job: SimJob, client: Client) -> None:
    """Run simulation job with telemetry."""
    from modelops.telemetry import TelemetryCollector, TelemetryStorage

    job_telemetry = TelemetryCollector()

    with job_telemetry.span("job.simulation", job_id=job.job_id) as span:
        span.metrics["total_tasks"] = len(job.tasks)

        with job_telemetry.span("job.submit"):
            futures = [...]

        with job_telemetry.span("job.gather"):
            results = sim_service.gather(futures)

        with job_telemetry.span("job.write_views"):
            write_job_view(job, results, prov_store=prov_store)

        span.metrics["n_results"] = len(results)

    # Save job-level telemetry
    storage = TelemetryStorage(
        storage_dir=Path("/tmp/modelops/provenance"),
        prov_store=prov_store
    )
    storage.save_job_telemetry(job.job_id, job_telemetry, job_type="simulation")
```

## Modern Library Considerations

### Should we use OpenTelemetry?

**Pros**:
- Industry standard (CNCF project)
- Rich ecosystem (exporters for Prometheus, Jaeger, etc.)
- Distributed tracing support
- Auto-instrumentation for many libraries

**Cons**:
- Heavy dependency (many packages)
- Requires infrastructure (collector, backend)
- Overkill for MVP
- More complex to configure

**Recommendation**: Start with pure Python, export to OTLP later if needed.

### Should we use structlog?

**Pros**:
- Structured logging with context
- Can attach metrics to log entries
- Good for debugging

**Cons**:
- Not a metrics system (logs ≠ metrics)
- Still need separate aggregation
- Another dependency

**Recommendation**: Keep logging and telemetry separate. Logs for debugging, telemetry for performance.

### Should we use Prometheus client?

**Pros**:
- De-facto standard for metrics
- Built-in aggregation
- Good for live monitoring

**Cons**:
- Requires Prometheus server
- Push vs pull model complexity
- Not great for historical analysis

**Recommendation**: Can export to Prometheus format later, but start simple.

### Recommendation: Pure Python + Future OTLP Export

**Phase 1** (MVP):
- Pure Python with `time.perf_counter()`
- Zero external dependencies
- JSON storage for easy inspection

**Phase 2** (Future):
- Add OTLP exporter: `pip install opentelemetry-api opentelemetry-sdk`
- Export spans to Jaeger/Tempo for distributed tracing
- Add Prometheus metrics endpoint for live monitoring

This gives us flexibility without premature commitment.

## Example Usage

### Querying Telemetry

```python
# Read job summary
import json
from pathlib import Path

job_id = "job-abc123"
summary_path = Path(f"/tmp/modelops/provenance/telemetry/jobs/{job_id}/summary.json")

with open(summary_path) as f:
    summary = json.load(f)

print(f"Total job duration: {summary['total_duration']:.2f}s")
print(f"Total simulations: {summary['by_name']['simulation.run_remote']['count']}")
print(f"Mean simulation time: {summary['by_name']['simulation.run_remote']['mean_duration']:.2f}s")
print(f"Cache hit rate: {summary['by_name']['provenance.lookup']['metrics']['cache_hit']['mean']:.1%}")
```

```python
# Read detailed spans
import pandas as pd

spans_path = Path(f"/tmp/modelops/provenance/telemetry/jobs/{job_id}/spans.jsonl")
df = pd.read_json(spans_path, lines=True)

# Analyze by operation type
print(df.groupby('name')['duration'].describe())

# Find slow simulations
slow_sims = df[df['name'] == 'simulation.run_remote'].nlargest(10, 'duration')
print(slow_sims[['duration', 'tags']])

# Plot timeline
import matplotlib.pyplot as plt
df['end_time'] = df['start_time'] + df['duration']
plt.barh(df['name'], df['duration'], left=df['start_time'])
plt.xlabel('Time (s)')
plt.title(f'Job {job_id} Timeline')
plt.tight_layout()
plt.savefig(f'{job_id}_timeline.png')
```

## Implementation Plan

### Phase 1: Foundation (Week 1)
**Goal**: Basic telemetry working end-to-end

1. **Create module structure** (2 hours)
   - `src/modelops/telemetry/__init__.py`
   - `src/modelops/telemetry/collector.py`
   - `src/modelops/telemetry/storage.py`
   - Unit tests: `tests/telemetry/test_collector.py`

2. **Integrate with executor** (3 hours)
   - Modify `SimulationExecutor.execute()`
   - Modify `IsolatedWarmExecEnv.run()`
   - Populate `SimReturn.metrics` field
   - Integration test

3. **Add storage** (2 hours)
   - Implement `TelemetryStorage`
   - Add to `job_runner.py`
   - Test local save + Azure upload

**Deliverable**: Simulation jobs capture timing, saved to JSON

### Phase 2: Calibration Integration (Week 2)
**Goal**: Calibration jobs tracked

4. **Instrument calibration wire** (3 hours)
   - Add telemetry to `calibration_wire()`
   - Track iteration metrics
   - Aggregate from SimReturn.metrics
   - Test with example job

**Deliverable**: Calibration jobs capture iteration timing

### Phase 3: Observability (Week 3)
**Goal**: Easy access to telemetry

5. **CLI commands** (4 hours)
   - `mops telemetry show <job-id>` - Pretty-print summary
   - `mops telemetry export <job-id>` - Export to CSV/JSON
   - Basic visualization (timeline plot)

6. **Documentation** (2 hours)
   - Add to docs/observability/
   - Example Jupyter notebook
   - Update CLAUDE.md

**Deliverable**: Users can easily inspect job performance

### Phase 4: Polish (Week 4)
**Goal**: Production-ready

7. **Configuration** (2 hours)
   - Environment variable: `MODELOPS_TELEMETRY_ENABLED`
   - Config option: `telemetry.upload_to_azure`
   - Disable for tests

8. **Performance validation** (2 hours)
   - Benchmark overhead
   - Ensure < 1% impact
   - Memory profiling

9. **Error handling** (1 hour)
   - Ensure graceful degradation
   - Never fail jobs
   - Comprehensive logging

**Deliverable**: Production-ready telemetry system

## Trade-offs & Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Library** | Pure Python | Zero dependencies, easy to understand, fast enough |
| **Storage** | JSON + JSONL | Human-readable, easy to query with pandas, compresses well |
| **Integration** | Context managers | Pythonic, automatic timing, exception-safe |
| **Disabled mode** | Noop object | Cleaner instrumentation, no guards needed, singleton = zero overhead |
| **Location** | `src/modelops/telemetry/` | Cross-cutting concern, substantial enough for module |
| **SimReturn.metrics** | Use existing field | No contract changes, already designed for this |
| **Failure mode** | Never fail jobs | Telemetry is observability, not correctness |
| **Azure upload** | Optional, async | Same pattern as ProvenanceStore |

## Success Metrics

**MVP Success** (Phase 1-2):
- ✅ Every simulation has execution_duration in SimReturn.metrics
- ✅ Job telemetry saved to `/tmp/modelops/provenance/telemetry/`
- ✅ Overhead < 1% of total job time
- ✅ Zero job failures due to telemetry

**Full Success** (Phase 3-4):
- ✅ CLI command shows job timeline
- ✅ Documentation with examples
- ✅ Used to debug actual performance issues
- ✅ Optional Azure upload working

## Open Questions for Review

1. **Module location**: `src/modelops/telemetry/` vs `src/modelops/observability/`?
2. **Export format**: JSONL good enough, or also Parquet?
3. **Span nesting**: Track parent-child relationships?
4. **Sampling**: Always collect, or sample % of jobs?
5. **Retention**: Auto-delete old telemetry after N days?
6. **Type safety**: Add `SpanLike` Protocol or keep duck-typed?
   - Protocol adds type safety for mypy/pyright
   - Not needed for MVP (just TelemetrySpan + NoopSpan)
   - Can add later if we add more span types

## References

- [OpenTelemetry Specification](https://opentelemetry.io/docs/specs/otel/)
- [Prometheus Best Practices](https://prometheus.io/docs/practices/naming/)
- Python `time.perf_counter()` docs
- Existing ProvenanceStore implementation: `src/modelops/services/provenance_store.py`

---

**Ready for colleague review?** This design provides:
- ✅ Clear goals and non-goals
- ✅ Code sketches for all integration points
- ✅ Library evaluation with rationale
- ✅ Phased implementation plan
- ✅ Trade-off analysis
