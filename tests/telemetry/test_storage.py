"""Tests for telemetry storage."""

import json
import pytest
from pathlib import Path
from unittest import mock

from modelops.telemetry import TelemetryCollector, TelemetryStorage


class TestTelemetryStorage:
    """Tests for TelemetryStorage."""

    def test_storage_initialization(self, tmp_path):
        """Initialize storage with directory."""
        storage = TelemetryStorage(storage_dir=tmp_path)

        assert storage.storage_dir == tmp_path
        assert storage.prov_store is None

    def test_storage_with_provenance_store(self, tmp_path):
        """Initialize storage with ProvenanceStore."""
        mock_prov_store = mock.Mock()
        storage = TelemetryStorage(storage_dir=tmp_path, prov_store=mock_prov_store)

        assert storage.prov_store is mock_prov_store

    def test_save_job_telemetry_creates_files(self, tmp_path):
        """save_job_telemetry creates summary and spans files."""
        storage = TelemetryStorage(storage_dir=tmp_path)
        collector = TelemetryCollector()

        # Create some telemetry
        with collector.span("test1"):
            pass
        with collector.span("test2"):
            pass

        # Save
        storage.save_job_telemetry("job-123", collector, job_type="simulation")

        # Check files created
        job_dir = tmp_path / "telemetry" / "jobs" / "job-123"
        assert job_dir.exists()
        assert (job_dir / "summary.json").exists()
        assert (job_dir / "spans.jsonl").exists()

    def test_summary_json_structure(self, tmp_path):
        """Summary JSON has expected structure."""
        storage = TelemetryStorage(storage_dir=tmp_path)
        collector = TelemetryCollector()

        with collector.span("operation1") as span:
            span.metrics["count"] = 5.0

        with collector.span("operation2") as span:
            span.metrics["size"] = 100.0

        storage.save_job_telemetry("job-123", collector, job_type="calibration")

        # Read summary
        summary_path = tmp_path / "telemetry" / "jobs" / "job-123" / "summary.json"
        with open(summary_path) as f:
            summary = json.load(f)

        assert summary["job_type"] == "calibration"
        assert summary["total_spans"] == 2
        assert summary["total_duration"] > 0
        assert "by_name" in summary
        assert "operation1" in summary["by_name"]
        assert "operation2" in summary["by_name"]

    def test_summary_aggregates_metrics(self, tmp_path):
        """Summary aggregates metrics from multiple spans."""
        storage = TelemetryStorage(storage_dir=tmp_path)
        collector = TelemetryCollector()

        # Multiple spans with same name
        for i in range(5):
            with collector.span("loop") as span:
                span.metrics["iteration"] = float(i)

        storage.save_job_telemetry("job-123", collector)

        summary_path = tmp_path / "telemetry" / "jobs" / "job-123" / "summary.json"
        with open(summary_path) as f:
            summary = json.load(f)

        loop_stats = summary["by_name"]["loop"]
        assert loop_stats["count"] == 5
        assert loop_stats["total_duration"] > 0
        assert loop_stats["mean_duration"] > 0
        assert loop_stats["max_duration"] >= loop_stats["min_duration"]

        # Metrics aggregated
        assert "metrics" in loop_stats
        assert "iteration" in loop_stats["metrics"]
        assert loop_stats["metrics"]["iteration"]["count"] == 5
        assert loop_stats["metrics"]["iteration"]["sum"] == 10.0  # 0+1+2+3+4
        assert loop_stats["metrics"]["iteration"]["mean"] == 2.0

    def test_spans_jsonl_format(self, tmp_path):
        """Spans written as line-delimited JSON."""
        storage = TelemetryStorage(storage_dir=tmp_path)
        collector = TelemetryCollector()

        with collector.span("test1", tag1="value1") as span:
            span.metrics["m1"] = 1.0

        with collector.span("test2", tag2="value2") as span:
            span.metrics["m2"] = 2.0

        storage.save_job_telemetry("job-123", collector)

        # Read spans
        spans_path = tmp_path / "telemetry" / "jobs" / "job-123" / "spans.jsonl"
        with open(spans_path) as f:
            lines = f.readlines()

        assert len(lines) == 2

        # Parse as JSON
        span1 = json.loads(lines[0])
        span2 = json.loads(lines[1])

        assert span1["name"] == "test1"
        assert span1["tags"]["tag1"] == "value1"
        assert span1["metrics"]["m1"] == 1.0

        assert span2["name"] == "test2"
        assert span2["tags"]["tag2"] == "value2"
        assert span2["metrics"]["m2"] == 2.0

    def test_atomic_write_with_temp_file(self, tmp_path):
        """Summary written atomically using temp file."""
        storage = TelemetryStorage(storage_dir=tmp_path)
        collector = TelemetryCollector()

        with collector.span("test"):
            pass

        storage.save_job_telemetry("job-123", collector)

        # No temp file should remain
        job_dir = tmp_path / "telemetry" / "jobs" / "job-123"
        temp_files = list(job_dir.glob("*.tmp"))
        assert len(temp_files) == 0

    def test_upload_to_azure_called_when_configured(self, tmp_path):
        """Azure upload called when ProvenanceStore has Azure backend."""
        mock_prov_store = mock.Mock()
        mock_prov_store._azure_backend = {"container": "results"}
        mock_prov_store._upload_to_azure = mock.Mock()

        storage = TelemetryStorage(storage_dir=tmp_path, prov_store=mock_prov_store)
        collector = TelemetryCollector()

        with collector.span("test"):
            pass

        storage.save_job_telemetry("job-123", collector)

        # Verify upload was called
        mock_prov_store._upload_to_azure.assert_called_once()
        call_args = mock_prov_store._upload_to_azure.call_args

        # Check arguments
        local_dir = call_args[0][0]
        remote_prefix = call_args[0][1]

        assert "job-123" in str(local_dir)
        assert remote_prefix == "telemetry/jobs/job-123"

    def test_upload_not_called_without_azure_backend(self, tmp_path):
        """Azure upload not called when no Azure backend."""
        mock_prov_store = mock.Mock()
        mock_prov_store._azure_backend = None

        storage = TelemetryStorage(storage_dir=tmp_path, prov_store=mock_prov_store)
        collector = TelemetryCollector()

        with collector.span("test"):
            pass

        storage.save_job_telemetry("job-123", collector)

        # Upload should not be called
        assert (
            not hasattr(mock_prov_store, "_upload_to_azure")
            or not mock_prov_store._upload_to_azure.called
        )

    def test_upload_failure_does_not_fail_job(self, tmp_path, caplog):
        """Azure upload failure is logged but doesn't raise."""
        mock_prov_store = mock.Mock()
        mock_prov_store._azure_backend = {"container": "results"}
        mock_prov_store._upload_to_azure = mock.Mock(side_effect=Exception("Upload failed"))

        storage = TelemetryStorage(storage_dir=tmp_path, prov_store=mock_prov_store)
        collector = TelemetryCollector()

        with collector.span("test"):
            pass

        # Should not raise
        storage.save_job_telemetry("job-123", collector)

        # Files still saved locally
        assert (tmp_path / "telemetry" / "jobs" / "job-123" / "summary.json").exists()

        # Warning logged
        assert "Failed to upload telemetry" in caplog.text

    def test_storage_failure_does_not_raise(self, tmp_path, caplog):
        """Storage failure is logged but doesn't raise."""
        # Make directory read-only to cause write failure
        job_dir = tmp_path / "telemetry" / "jobs" / "job-123"
        job_dir.mkdir(parents=True)
        job_dir.chmod(0o444)

        try:
            storage = TelemetryStorage(storage_dir=tmp_path)
            collector = TelemetryCollector()

            with collector.span("test"):
                pass

            # Should not raise
            storage.save_job_telemetry("job-123", collector)

            # Warning logged
            assert "Failed to save telemetry" in caplog.text
        finally:
            # Restore permissions for cleanup
            job_dir.chmod(0o755)

    def test_empty_telemetry(self, tmp_path):
        """Handle empty telemetry gracefully."""
        storage = TelemetryStorage(storage_dir=tmp_path)
        collector = TelemetryCollector()

        # No spans
        storage.save_job_telemetry("job-123", collector)

        # Files still created
        summary_path = tmp_path / "telemetry" / "jobs" / "job-123" / "summary.json"
        assert summary_path.exists()

        with open(summary_path) as f:
            summary = json.load(f)

        assert summary["total_spans"] == 0
        assert summary["total_duration"] == 0
        assert summary["by_name"] == {}

    def test_multiple_jobs(self, tmp_path):
        """Save telemetry for multiple jobs."""
        storage = TelemetryStorage(storage_dir=tmp_path)

        for job_id in ["job-1", "job-2", "job-3"]:
            collector = TelemetryCollector()
            with collector.span("test"):
                pass
            storage.save_job_telemetry(job_id, collector)

        # All jobs saved
        telemetry_dir = tmp_path / "telemetry" / "jobs"
        assert (telemetry_dir / "job-1" / "summary.json").exists()
        assert (telemetry_dir / "job-2" / "summary.json").exists()
        assert (telemetry_dir / "job-3" / "summary.json").exists()
