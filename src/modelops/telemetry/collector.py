"""Telemetry collector for ModelOps.

Provides lightweight context-manager based telemetry for capturing
execution timing and metrics without requiring external dependencies.
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NoopSpan:
    """No-op span when telemetry is disabled.

    Implements same interface as TelemetrySpan but drops all data.
    Using a Noop object instead of None lets instrumentation code
    stay clean without guards: span.metrics["key"] = value just works.
    """

    metrics: dict[str, float] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    def duration(self) -> None:
        """Always returns None for disabled telemetry."""
        return None

    def to_dict(self) -> dict[str, Any]:
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
    end_time: float | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    def duration(self) -> float | None:
        """Duration in seconds, or None if still running."""
        if self.end_time is None:
            return None
        return self.end_time - self.start_time

    def to_dict(self) -> dict[str, Any]:
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
        self.spans: list[TelemetrySpan] = []
        self._current_span: TelemetrySpan | None = None

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
                span.metrics["hit"] = 1.0 if result else 0.0
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

    def to_dict(self) -> dict[str, Any]:
        """Export all telemetry as JSON-compatible dict."""
        return {
            "spans": [s.to_dict() for s in self.spans],
            "total_spans": len(self.spans),
            "total_duration": sum(s.duration() or 0 for s in self.spans),
        }
