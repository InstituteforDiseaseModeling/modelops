"""Output manifest generation for job validation.

This module generates the expected output paths for a job based on
its specification. These paths are used to validate that all expected
outputs were actually created.
"""

from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
import hashlib
import json

from modelops_contracts import SimJob, SimTask, CalibrationJob
from modelops_contracts.simulation import UniqueParameterSet, AggregationTask

from .provenance_schema import ProvenanceSchema


@dataclass
class OutputSpec:
    """Specification for an expected output.

    Contains all information needed to verify an output exists
    and reconstruct missing tasks for resumption.
    """
    param_id: str                # Parameter set ID
    seed: int                    # Random seed for simulation
    output_type: str             # "simulation" or "aggregation"
    bundle_digest: str           # Bundle hash for invalidation
    replicate_count: int         # Number of replicates
    provenance_path: str         # Expected path in ProvenanceStore

    # Additional fields for task reconstruction
    param_values: Optional[Dict[str, Any]] = None   # Original parameter values
    target_id: Optional[str] = None                 # Target ID for aggregation tasks
    aggregation_id: Optional[str] = None            # Aggregation ID


def generate_output_manifest(
    job: SimJob,
    provenance_schema: ProvenanceSchema
) -> List[OutputSpec]:
    """Generate expected outputs from job specification.

    Creates a complete list of all expected outputs based on the job's
    parameter sets and replicate counts. This manifest is used to:
    1. Validate job completion
    2. Identify missing outputs for partial completion
    3. Reconstruct tasks for resumption

    Args:
        job: Simulation job specification
        provenance_schema: Schema for generating storage paths

    Returns:
        List of OutputSpec objects describing expected outputs
    """
    outputs = []

    # Extract bundle digest from job metadata
    bundle_digest = job.metadata.get("bundle_digest", "unknown")

    # Generate output specs for each parameter set and replicate
    for param_set in job.parameter_sets:
        param_id = param_set.param_id

        # For each replicate (seed)
        for seed in range(param_set.replicate_count):
            # Generate the expected path using provenance schema
            path_context = {
                "schema_name": provenance_schema.name,
                "version": provenance_schema.version,
                "bundle_digest": bundle_digest,
                "param_id": param_id,
                "seed": seed,
            }

            # Render the simulation path
            sim_path = provenance_schema.render_path(
                provenance_schema.sim_path_template,
                path_context
            )

            # Create output spec for simulation result
            output = OutputSpec(
                param_id=param_id,
                seed=seed,
                output_type="simulation",
                bundle_digest=bundle_digest,
                replicate_count=param_set.replicate_count,
                provenance_path=sim_path,
                param_values=dict(param_set.params),  # Store for task reconstruction
            )
            outputs.append(output)

    # If job has aggregation tasks, add those too
    # (This would be extended for CalibrationJob with target evaluation)

    return outputs


def generate_calibration_manifest(
    job: CalibrationJob,
    provenance_schema: ProvenanceSchema
) -> List[OutputSpec]:
    """Generate expected outputs for calibration job.

    Calibration jobs include both simulation outputs and aggregation/target
    evaluation outputs.

    Args:
        job: Calibration job specification
        provenance_schema: Schema for generating storage paths

    Returns:
        List of OutputSpec objects for all expected outputs
    """
    outputs = []

    # Extract bundle digest
    bundle_digest = job.metadata.get("bundle_digest", "unknown")

    # First, add all simulation outputs
    for param_set in job.parameter_sets:
        param_id = param_set.param_id

        for seed in range(param_set.replicate_count):
            path_context = {
                "schema_name": provenance_schema.name,
                "version": provenance_schema.version,
                "bundle_digest": bundle_digest,
                "param_id": param_id,
                "seed": seed,
            }

            sim_path = provenance_schema.render_path(
                provenance_schema.sim_path_template,
                path_context
            )

            output = OutputSpec(
                param_id=param_id,
                seed=seed,
                output_type="simulation",
                bundle_digest=bundle_digest,
                replicate_count=param_set.replicate_count,
                provenance_path=sim_path,
                param_values=dict(param_set.params),
            )
            outputs.append(output)

    # Add aggregation outputs for each target
    if hasattr(job, 'targets') and job.targets:
        for target in job.targets:
            target_id = target.target_id if hasattr(target, 'target_id') else f"target_{hash(str(target))[:8]}"

            # For each parameter set that needs aggregation
            for param_set in job.parameter_sets:
                # Generate aggregation ID (deterministic from inputs)
                agg_data = {
                    "param_id": param_set.param_id,
                    "target_id": target_id,
                    "replicate_count": param_set.replicate_count
                }
                agg_id = hashlib.sha256(
                    json.dumps(agg_data, sort_keys=True).encode()
                ).hexdigest()[:16]

                # Generate aggregation path
                agg_context = {
                    "schema_name": provenance_schema.name,
                    "version": provenance_schema.version,
                    "bundle_digest": bundle_digest,
                    "target": target_id,
                    "aggregation_id": agg_id,
                }

                agg_path = provenance_schema.render_path(
                    provenance_schema.agg_path_template,
                    agg_context
                )

                # Create aggregation output spec
                agg_output = OutputSpec(
                    param_id=param_set.param_id,
                    seed=-1,  # Aggregations don't have a single seed
                    output_type="aggregation",
                    bundle_digest=bundle_digest,
                    replicate_count=param_set.replicate_count,
                    provenance_path=agg_path,
                    param_values=dict(param_set.params),
                    target_id=target_id,
                    aggregation_id=agg_id,
                )
                outputs.append(agg_output)

    return outputs


def reconstruct_task_from_spec(output_spec: OutputSpec) -> Optional[SimTask]:
    """Reconstruct a SimTask from an OutputSpec.

    Used when resuming partial jobs to recreate tasks for missing outputs.

    Args:
        output_spec: Specification of missing output

    Returns:
        SimTask that would produce the missing output, or None if not a sim task
    """
    if output_spec.output_type != "simulation":
        return None

    # Reconstruct the SimTask with proper structure
    from modelops_contracts import UniqueParameterSet

    task = SimTask(
        entrypoint="simulations.model/run",  # Valid entrypoint format
        bundle_ref=output_spec.bundle_digest,
        params=UniqueParameterSet(
            param_id=output_spec.param_id,
            params=output_spec.param_values or {}
        ),
        seed=output_spec.seed,
        outputs=["output.csv"]  # Default output
    )

    return task


def reconstruct_aggregation_from_spec(output_spec: OutputSpec) -> Optional[AggregationTask]:
    """Reconstruct an AggregationTask from an OutputSpec.

    Used when resuming partial jobs to recreate aggregation tasks.

    Args:
        output_spec: Specification of missing aggregation output

    Returns:
        AggregationTask for the missing output, or None if not aggregation
    """
    if output_spec.output_type != "aggregation":
        return None

    # Would need additional context to fully reconstruct
    # This is a placeholder for the pattern
    task = AggregationTask(
        aggregation_id=output_spec.aggregation_id,
        target_id=output_spec.target_id,
        param_id=output_spec.param_id,
        # Additional fields from job context
    )

    return task