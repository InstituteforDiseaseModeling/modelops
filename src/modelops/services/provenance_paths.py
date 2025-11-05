"""Path resolution utilities for ProvenanceStore.

This module provides canonical path generation for all provenance store artifacts
including simulations, aggregations, and job-scoped views.
"""

import hashlib
from typing import TYPE_CHECKING

from modelops_contracts.utils import bundle12

if TYPE_CHECKING:
    from modelops_contracts.simulation import SimTask


def shard(param_id: str) -> str:
    """Get shard prefix from param_id for directory partitioning.

    Args:
        param_id: Parameter set ID

    Returns:
        First 2 chars of param_id for sharding
    """
    return param_id[:2]


def sim_path(task: "SimTask") -> str:
    """Generate canonical path for simulation result.

    Args:
        task: SimTask containing bundle_ref, params, seed

    Returns:
        Path to simulation result directory
    """
    b12 = bundle12(task.bundle_ref)
    pid = task.params.param_id
    return f"/provenance/token/v1/sims/{b12}/{shard(pid)}/params_{pid[:8]}/seed_{task.seed}"


def agg_path(bundle_ref: str, target_entrypoint: str, task_ids: list[str]) -> str:
    """Generate canonical path for aggregation result.

    Args:
        bundle_ref: Bundle reference string
        target_entrypoint: Target module:object string
        task_ids: List of task IDs being aggregated

    Returns:
        Path to aggregation result directory
    """
    # Generate stable aggregation ID
    key = f"{target_entrypoint}:{','.join(sorted(task_ids))}"
    aid = hashlib.blake2b(key.encode(), digest_size=16).hexdigest()[:16]

    b12 = bundle12(bundle_ref)
    # Clean target for filesystem (replace : with __)
    target_clean = target_entrypoint.replace(":", "__")

    return f"/provenance/token/v1/aggs/{b12}/target_{target_clean}/agg_{aid}"


def job_view_root(job_id: str) -> str:
    """Get root path for job-scoped views.

    Args:
        job_id: Job identifier

    Returns:
        Root path for job's view artifacts
    """
    return f"/provenance/token/v1/views/jobs/{job_id}"


def job_losses_path(job_id: str) -> str:
    """Get path to job's losses parquet dataset.

    Args:
        job_id: Job identifier

    Returns:
        Path to losses.parquet directory
    """
    return f"{job_view_root(job_id)}/losses.parquet"


def job_manifest_path(job_id: str) -> str:
    """Get path to job's index manifest.

    Args:
        job_id: Job identifier

    Returns:
        Path to manifest.json
    """
    return f"{job_view_root(job_id)}/manifest.json"


def job_summary_path(job_id: str) -> str:
    """Get path to job's summary statistics.

    Args:
        job_id: Job identifier

    Returns:
        Path to summary.json
    """
    return f"{job_view_root(job_id)}/summary.json"


def job_schema_path(job_id: str) -> str:
    """Get path to job's schema definition.

    Args:
        job_id: Job identifier

    Returns:
        Path to schema.json
    """
    return f"{job_view_root(job_id)}/schema.json"
