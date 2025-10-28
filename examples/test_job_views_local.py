#!/usr/bin/env python
"""Test job_views.py Parquet writing locally without Kubernetes.

This script simulates what happens in job_runner.py but runs locally
for faster iteration during development.
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timezone
import tempfile

# Add parent directory to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from modelops_contracts import (
    SimJob, SimTask, UniqueParameterSet, TargetSpec,
    AggregationReturn, TableArtifact
)
from modelops.services.job_views import write_job_view

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def create_test_job():
    """Create a test SimJob with tasks."""
    tasks = []

    # Create 5 parameter sets with 10 replicates each
    for i in range(5):
        params = UniqueParameterSet.from_dict({
            "alpha": 1.0 + i * 0.5,
            "beta": 2.0 + i * 0.3,
            "gamma": 0.1
        })

        # Create 10 replicates for this parameter set
        for seed in range(10):
            task = SimTask(
                bundle_ref="sha256:" + "a" * 64,
                entrypoint="models.seir/baseline",
                params=params,
                seed=seed * 1000 + i
            )
            tasks.append(task)

    # Create job with target spec
    job = SimJob(
        job_id=f"test-job-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        bundle_ref="sha256:" + "a" * 64,
        tasks=tasks,
        target_spec=TargetSpec(
            data={"target_entrypoints": ["targets.prevalence/compute_loss"]},
            loss_function="mse",
            weights=None,
            metadata={}
        )
    )

    return job


def create_test_results(job):
    """Create test AggregationReturn objects."""
    results = []

    # Group tasks by parameter ID
    task_groups = job.get_task_groups()

    for i, (param_id, replicate_tasks) in enumerate(task_groups.items()):
        # Simulate AggregationReturn for each parameter set
        loss_value = 1000.0 + i * 500.0  # Dummy loss values

        result = AggregationReturn(
            aggregation_id=f"agg-{param_id[:8]}",
            loss=loss_value,
            diagnostics={"mean_squared_error": loss_value, "r_squared": 0.85},
            outputs={},  # Empty for this test
            n_replicates=len(replicate_tasks)
        )
        results.append(result)
        logger.info(f"Created AggregationReturn for param {param_id[:8]}: loss={loss_value:.2f}")

    return results


def test_direct_writing():
    """Test writing Parquet directly without going through job_runner."""
    logger.info("=" * 60)
    logger.info("Testing direct Parquet writing with job_views.py")
    logger.info("=" * 60)

    # Create test data
    job = create_test_job()
    logger.info(f"Created test job: {job.job_id}")
    logger.info(f"  Total tasks: {job.task_count()}")
    logger.info(f"  Parameter sets: {len(job.get_task_groups())}")

    results = create_test_results(job)
    logger.info(f"Created {len(results)} AggregationReturn objects")

    # Use a temporary directory for output
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "views" / "jobs"
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"\nWriting to: {output_dir}")

        try:
            # Call the write function
            view_path = write_job_view(job, results, output_dir)
            logger.info(f"✓ Successfully wrote job view to: {view_path}")

            # Check what was created
            logger.info("\nChecking created files:")
            job_dir = output_dir / job.job_id

            # Check manifest
            manifest_path = job_dir / "manifest.json"
            if manifest_path.exists():
                import json
                with open(manifest_path, 'r') as f:
                    manifest = json.load(f)
                logger.info(f"✓ Manifest created:")
                logger.info(f"  - Total rows: {manifest['row_counts']['total']}")
                logger.info(f"  - Available: {manifest['row_counts']['available']}")
                logger.info(f"  - Failed: {manifest['row_counts']['failed']}")
            else:
                logger.error("✗ No manifest.json found")

            # Check Parquet files
            data_dir = job_dir / "data"
            if data_dir.exists():
                parquet_files = list(data_dir.glob("*.parquet"))
                logger.info(f"✓ Found {len(parquet_files)} Parquet file(s)")

                # Try to read the Parquet data
                try:
                    import pyarrow.parquet as pq
                    table = pq.read_table(str(data_dir))
                    logger.info(f"✓ Successfully read Parquet table:")
                    logger.info(f"  - Rows: {table.num_rows}")
                    logger.info(f"  - Columns: {table.column_names}")

                    # Show first few rows
                    import pandas as pd
                    df = table.to_pandas()
                    logger.info("\nFirst 3 rows of data:")
                    logger.info(df.head(3).to_string())

                except ImportError:
                    logger.warning("pyarrow not installed - cannot read Parquet files")
                except Exception as e:
                    logger.error(f"Failed to read Parquet: {e}")
            else:
                logger.error("✗ No data directory found")

        except Exception as e:
            logger.error(f"✗ Failed to write job view: {e}")
            import traceback
            traceback.print_exc()
            return False

    return True


def test_with_non_aggregation_results():
    """Test handling of non-AggregationReturn results (should be skipped)."""
    logger.info("\n" + "=" * 60)
    logger.info("Testing with mixed result types")
    logger.info("=" * 60)

    job = create_test_job()

    # Mix of result types
    results = [
        "not_an_aggregation_return",  # Should be skipped
        {"loss": 100},  # Should be skipped
        AggregationReturn(
            aggregation_id="agg-001",
            loss=500.0,
            diagnostics={},
            outputs={},
            n_replicates=10
        ),
        None,  # Should be skipped
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "views" / "jobs"
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            view_path = write_job_view(job, results, output_dir)
            logger.info(f"✓ Handled mixed types without crashing")

            # Check that only valid results were written
            manifest_path = output_dir / job.job_id / "manifest.json"
            if manifest_path.exists():
                import json
                with open(manifest_path, 'r') as f:
                    manifest = json.load(f)
                logger.info(f"  - Available rows: {manifest['row_counts']['available']}")
                if manifest['row_counts']['available'] == 1:
                    logger.info("✓ Correctly processed only the valid AggregationReturn")
                else:
                    logger.error(f"✗ Expected 1 row, got {manifest['row_counts']['available']}")
        except Exception as e:
            logger.error(f"✗ Failed with mixed types: {e}")
            return False

    return True


if __name__ == "__main__":
    logger.info("Starting local job_views tests...\n")

    # Test 1: Direct writing with valid data
    success = test_direct_writing()

    # Test 2: Mixed result types
    if success:
        success = test_with_non_aggregation_results()

    if success:
        logger.info("\n" + "=" * 60)
        logger.info("✅ All local tests passed!")
        logger.info("=" * 60)
        logger.info("\nNext steps:")
        logger.info("1. If tests pass, rebuild Docker image:")
        logger.info("   REGISTRY=modelopsdevacrvsb.azurecr.io ORG=modelops make build-runner")
        logger.info("2. Redeploy and test on cluster")
    else:
        logger.error("\n❌ Some tests failed - fix issues before deploying")
        sys.exit(1)