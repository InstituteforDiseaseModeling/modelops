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
    """Create test AggregationReturn objects for multiple targets."""
    # Simulate multiple targets
    target_names = ["prevalence", "incidence", "mortality"]
    results_by_target = {}

    # Group tasks by parameter ID
    task_groups = job.get_task_groups()

    for target_idx, target_name in enumerate(target_names):
        results = []

        for i, (param_id, replicate_tasks) in enumerate(task_groups.items()):
            # Different loss values for different targets
            base_loss = 1000.0 + i * 500.0
            target_multiplier = 1.0 + target_idx * 0.5  # Each target has different scale
            loss_value = base_loss * target_multiplier

            result = AggregationReturn(
                aggregation_id=f"agg-{param_id[:8]}-{target_name}",
                loss=loss_value,
                diagnostics={"mean_squared_error": loss_value, "r_squared": 0.85 - target_idx * 0.1},
                outputs={},  # Empty for this test
                n_replicates=len(replicate_tasks)
            )
            results.append(result)
            logger.info(f"Created AggregationReturn for param {param_id[:8]}, target '{target_name}': loss={loss_value:.2f}")

        results_by_target[target_name] = results

    return results_by_target


def create_test_sim_returns(job):
    """Create test SimReturn objects with realistic DataFrame outputs."""
    import polars as pl
    import hashlib
    from io import BytesIO

    raw_sim_returns_by_param = {}

    # Group tasks by parameter ID
    task_groups = job.get_task_groups()

    for param_id, replicate_tasks in task_groups.items():
        sim_returns = []

        for replicate_idx, task in enumerate(replicate_tasks):
            # Create realistic time series data
            days = list(range(60))
            # Simulate SIR-like dynamics
            infected = [10 + replicate_idx * 2 + i * 0.5 for i in days]
            susceptible = [1000 - x for x in infected]

            # Create DataFrames for each output
            outputs = {}

            # Incidence output
            incidence_df = pl.DataFrame({
                "day": days,
                "infected": infected,
                "seed": [task.seed] * len(days)
            })

            # Prevalence output
            prevalence_df = pl.DataFrame({
                "day": days,
                "susceptible": susceptible,
                "infected": infected,
                "seed": [task.seed] * len(days)
            })

            # Serialize to Arrow IPC
            for output_name, df in [("incidence", incidence_df), ("prevalence", prevalence_df)]:
                buffer = BytesIO()
                df.write_ipc(buffer)
                arrow_bytes = buffer.getvalue()

                # Create TableArtifact with valid checksum
                checksum = hashlib.blake2b(arrow_bytes, digest_size=32).hexdigest()
                outputs[output_name] = TableArtifact(
                    size=len(arrow_bytes),
                    inline=arrow_bytes,
                    checksum=checksum
                )

            # Create SimReturn
            from modelops_contracts import SimReturn
            sim_return = SimReturn(
                task_id=f"task-{param_id[:8]}-{replicate_idx}",
                outputs=outputs
            )
            sim_returns.append(sim_return)

        raw_sim_returns_by_param[param_id] = sim_returns
        logger.info(
            f"Created {len(sim_returns)} SimReturns for param {param_id[:8]} "
            f"with outputs: {list(sim_returns[0].outputs.keys())}"
        )

    return raw_sim_returns_by_param


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

    results_by_target = create_test_results(job)
    logger.info(f"Created results for {len(results_by_target)} targets")

    # Create test SimReturns with model outputs
    raw_sim_returns = create_test_sim_returns(job)
    logger.info(f"Created SimReturns for {len(raw_sim_returns)} parameter sets")

    # Use a temporary directory for output
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "views" / "jobs"
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"\nWriting to: {output_dir}")

        try:
            # Call the write function with multiple targets AND model outputs
            view_path = write_job_view(job, results_by_target, output_dir, raw_sim_returns=raw_sim_returns)
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
                logger.info(f"  - Targets: {list(manifest.get('targets', {}).keys())}")
                for target_name, target_info in manifest.get('targets', {}).items():
                    logger.info(f"  - Target '{target_name}':")
                    logger.info(f"      Rows: {target_info['rows']}")
                    logger.info(f"      Mean loss: {target_info.get('mean_loss', 'N/A')}")
            else:
                logger.error("✗ No manifest.json found")

            # Check Parquet files for each target
            targets_dir = job_dir / "targets"
            if targets_dir.exists():
                for target_dir in targets_dir.iterdir():
                    if target_dir.is_dir():
                        parquet_path = target_dir / "data.parquet"
                        if parquet_path.exists():
                            logger.info(f"✓ Found Parquet for target '{target_dir.name}'")

                            # Try to read the Parquet data
                            try:
                                import pyarrow.parquet as pq
                                table = pq.read_table(str(parquet_path))
                                logger.info(f"  - Rows: {table.num_rows}")
                                logger.info(f"  - Columns: {len(table.column_names)}")

                                # Show column names
                                param_cols = [c for c in table.column_names if c.startswith('param_')]
                                logger.info(f"  - Parameter columns: {param_cols}")

                            except ImportError:
                                logger.warning("pyarrow not installed - cannot read Parquet files")
                            except Exception as e:
                                logger.error(f"Failed to read Parquet: {e}")
                        else:
                            logger.error(f"✗ No Parquet file for target '{target_dir.name}'")
            else:
                logger.error("✗ No targets directory found")

            # Check model_outputs directory
            logger.info("\nChecking model outputs:")
            model_outputs_dir = job_dir / "model_outputs"
            if model_outputs_dir.exists():
                output_files = list(model_outputs_dir.glob("*.parquet"))
                logger.info(f"✓ Found {len(output_files)} model output Parquet files")

                for output_file in output_files:
                    logger.info(f"  - {output_file.name}")
                    try:
                        import polars as pl
                        df = pl.read_parquet(output_file)
                        logger.info(f"    Rows: {len(df):,}, Columns: {len(df.columns)}")
                        logger.info(f"    Schema: {', '.join(df.columns)}")

                        # Check for metadata columns
                        if "param_id" in df.columns:
                            logger.info(f"    ✓ Has param_id column")
                        if "replicate_idx" in df.columns:
                            logger.info(f"    ✓ Has replicate_idx column")
                        if "seed" in df.columns:
                            logger.info(f"    ✓ Has seed column")

                    except ImportError:
                        logger.warning("    polars not installed - cannot read Parquet")
                    except Exception as e:
                        logger.error(f"    Failed to read: {e}")
            else:
                logger.warning("✗ No model_outputs directory found")

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

    # Mix of result types - simulating what happens with a single target
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
                # New format uses 'targets' instead of 'row_counts'
                default_target = manifest['targets'].get('default', {})
                available = default_target.get('available', 0)
                logger.info(f"  - Available rows: {available}")
                if available == 1:
                    logger.info("✓ Correctly processed only the valid AggregationReturn")
                else:
                    logger.error(f"✗ Expected 1 row, got {available}")
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