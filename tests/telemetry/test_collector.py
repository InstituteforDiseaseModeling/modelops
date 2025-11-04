"""Tests for telemetry collector."""

import pytest
import time
from unittest import mock

from modelops.telemetry import (
    NoopSpan,
    NOOP_SPAN,
    TelemetryCollector,
    TelemetrySpan,
)


class TestNoopSpan:
    """Tests for NoopSpan."""

    def test_noop_span_interface(self):
        """NoopSpan implements same interface as TelemetrySpan."""
        span = NoopSpan()

        # Has metrics and tags
        assert hasattr(span, "metrics")
        assert hasattr(span, "tags")

        # Can write to them (drops data)
        span.metrics["test"] = 1.0
        span.tags["key"] = "value"

        # Duration is None
        assert span.duration() is None

        # to_dict is empty
        assert span.to_dict() == {}

    def test_noop_span_singleton(self):
        """NOOP_SPAN is a singleton."""
        assert NOOP_SPAN is not None
        assert isinstance(NOOP_SPAN, NoopSpan)


class TestTelemetrySpan:
    """Tests for TelemetrySpan."""

    def test_span_creation(self):
        """Create a span with start time."""
        start = time.perf_counter()
        span = TelemetrySpan(name="test", start_time=start)

        assert span.name == "test"
        assert span.start_time == start
        assert span.end_time is None
        assert span.duration() is None

    def test_span_duration(self):
        """Compute duration after span ends."""
        start = time.perf_counter()
        span = TelemetrySpan(name="test", start_time=start)

        time.sleep(0.01)  # Sleep 10ms
        span.end_time = time.perf_counter()

        duration = span.duration()
        assert duration is not None
        assert duration >= 0.01  # At least 10ms
        assert duration < 0.1  # But not too long

    def test_span_metrics_and_tags(self):
        """Add metrics and tags to span."""
        span = TelemetrySpan(name="test", start_time=time.perf_counter())

        span.metrics["count"] = 42.0
        span.metrics["ratio"] = 0.75

        span.tags["param_id"] = "abc123"
        span.tags["iteration"] = "5"

        assert span.metrics == {"count": 42.0, "ratio": 0.75}
        assert span.tags == {"param_id": "abc123", "iteration": "5"}

    def test_span_to_dict(self):
        """Serialize span to dict."""
        start = time.perf_counter()
        span = TelemetrySpan(name="test", start_time=start)
        span.end_time = start + 1.5
        span.metrics["count"] = 10.0
        span.tags["key"] = "value"

        data = span.to_dict()

        assert data["name"] == "test"
        assert data["start_time"] == start
        assert data["end_time"] == start + 1.5
        assert data["duration"] == 1.5
        assert data["metrics"] == {"count": 10.0}
        assert data["tags"] == {"key": "value"}


class TestTelemetryCollector:
    """Tests for TelemetryCollector."""

    def test_collector_enabled_by_default(self):
        """Collector is enabled by default."""
        collector = TelemetryCollector()
        assert collector.enabled is True
        assert len(collector.spans) == 0

    def test_collector_disabled_mode(self):
        """Collector can be disabled."""
        collector = TelemetryCollector(enabled=False)
        assert collector.enabled is False

    def test_span_context_manager_enabled(self):
        """Span context manager records timing when enabled."""
        collector = TelemetryCollector(enabled=True)

        with collector.span("test.operation", key="value") as span:
            # Span is real
            assert isinstance(span, TelemetrySpan)
            assert span.name == "test.operation"
            assert span.tags["key"] == "value"

            # Can add metrics
            span.metrics["count"] = 5.0

            time.sleep(0.01)  # Do some work

        # Span was recorded
        assert len(collector.spans) == 1
        recorded = collector.spans[0]

        assert recorded.name == "test.operation"
        assert recorded.duration() is not None
        assert recorded.duration() >= 0.01
        assert recorded.metrics["count"] == 5.0
        assert recorded.tags["key"] == "value"

    def test_span_context_manager_disabled(self):
        """Span context manager returns NOOP_SPAN when disabled."""
        collector = TelemetryCollector(enabled=False)

        with collector.span("test.operation") as span:
            # Span is noop
            assert span is NOOP_SPAN

            # Can still write to it (no-op)
            span.metrics["count"] = 5.0

        # No spans recorded
        assert len(collector.spans) == 0

    def test_nested_spans(self):
        """Can create nested spans."""
        collector = TelemetryCollector()

        with collector.span("outer") as outer:
            outer.metrics["outer_metric"] = 1.0

            with collector.span("inner") as inner:
                inner.metrics["inner_metric"] = 2.0

        # Both spans recorded
        assert len(collector.spans) == 2
        assert collector.spans[0].name == "inner"  # Inner completes first
        assert collector.spans[1].name == "outer"  # Outer completes second

    def test_span_exception_handling(self):
        """Span records exception and re-raises."""
        collector = TelemetryCollector()

        with pytest.raises(ValueError):
            with collector.span("test") as span:
                raise ValueError("test error")

        # Span was still recorded
        assert len(collector.spans) == 1
        recorded = collector.spans[0]

        assert recorded.tags["error"] == "ValueError"
        assert "test error" in recorded.tags["error_msg"]
        assert recorded.duration() is not None

    def test_add_metric_convenience(self):
        """add_metric adds to current span."""
        collector = TelemetryCollector()

        with collector.span("test") as span:
            # Can use convenience method
            collector.add_metric("count", 42.0)

            # Or direct assignment
            span.metrics["direct"] = 10.0

        recorded = collector.spans[0]
        assert recorded.metrics["count"] == 42.0
        assert recorded.metrics["direct"] == 10.0

    def test_add_metric_no_current_span(self):
        """add_metric does nothing outside span context."""
        collector = TelemetryCollector()

        # No error, just no-op
        collector.add_metric("count", 42.0)

        assert len(collector.spans) == 0

    def test_add_metric_when_disabled(self):
        """add_metric does nothing when disabled."""
        collector = TelemetryCollector(enabled=False)

        with collector.span("test"):
            collector.add_metric("count", 42.0)

        # No spans recorded
        assert len(collector.spans) == 0

    def test_to_dict_export(self):
        """Export all telemetry as dict."""
        collector = TelemetryCollector()

        with collector.span("first"):
            time.sleep(0.01)

        with collector.span("second"):
            time.sleep(0.01)

        data = collector.to_dict()

        assert data["total_spans"] == 2
        assert data["total_duration"] >= 0.02
        assert len(data["spans"]) == 2
        assert data["spans"][0]["name"] == "first"
        assert data["spans"][1]["name"] == "second"

    def test_to_dict_empty(self):
        """Export empty telemetry."""
        collector = TelemetryCollector()

        data = collector.to_dict()

        assert data["total_spans"] == 0
        assert data["total_duration"] == 0
        assert data["spans"] == []

    def test_multiple_spans_same_name(self):
        """Can create multiple spans with same name."""
        collector = TelemetryCollector()

        for i in range(5):
            with collector.span("loop", iteration=str(i)) as span:
                span.metrics["index"] = float(i)

        assert len(collector.spans) == 5
        assert all(s.name == "loop" for s in collector.spans)
        assert [s.metrics["index"] for s in collector.spans] == [0.0, 1.0, 2.0, 3.0, 4.0]
