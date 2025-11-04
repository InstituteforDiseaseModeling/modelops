"""Telemetry storage for ModelOps.

Persists telemetry data following ProvenanceStore pattern:
local-first with optional Azure Blob Storage upload.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from modelops.services.provenance_store import ProvenanceStore
    from modelops.telemetry.collector import TelemetryCollector

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
        prov_store: Optional["ProvenanceStore"] = None,
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
        telemetry: "TelemetryCollector",
        job_type: str = "simulation",
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
        telemetry: "TelemetryCollector",
        job_type: str,
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
            "by_name": {},
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
                        "count": len(values),
                    }
                    for key, values in all_metrics.items()
                }

        return summary

    def _upload_to_azure(self, local_dir: Path, job_id: str):
        """Upload telemetry to Azure (best-effort)."""
        if not self.prov_store:
            return

        if not hasattr(self.prov_store, "_azure_backend") or not self.prov_store._azure_backend:
            return

        try:
            remote_prefix = f"telemetry/jobs/{job_id}"
            self.prov_store._upload_to_azure(local_dir, remote_prefix)
            logger.info(f"Uploaded telemetry to Azure: {remote_prefix}")
        except Exception as e:
            logger.warning(f"Failed to upload telemetry to Azure: {e}")
