"""Telemetry module for ModelOps.

Provides lightweight telemetry collection for capturing execution
timing and performance metrics.
"""

from modelops.telemetry.collector import (
    NOOP_SPAN,
    NoopSpan,
    TelemetryCollector,
    TelemetrySpan,
)
from modelops.telemetry.storage import TelemetryStorage

__all__ = [
    "NoopSpan",
    "NOOP_SPAN",
    "TelemetryCollector",
    "TelemetrySpan",
    "TelemetryStorage",
]
