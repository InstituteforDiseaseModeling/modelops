"""Job submission CLI commands.

This module provides a thin CLI wrapper around the JobSubmissionClient
for submitting simulation and calibration jobs to the cluster.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional, Tuple

import typer
from modelops_contracts import CalibrationSpec, SimulationStudy
from rich.progress import Progress, SpinnerColumn, TextColumn

from ..client import JobSubmissionClient
from ..services.job_registry import JobRegistry
from ..services.job_state import JobStatus
from ..services.storage.azure_versioned import AzureVersionedStore
from .common_options import env_option
from .display import console, error, info, section, success, warning
from .formatting import format_duration, format_timestamp, get_timezone_info

logger = logging.getLogger(__name__)

app = typer.Typer(help="Submit and manage simulation jobs")


def _get_registry(env: str) -> JobRegistry | None:
    """Get JobRegistry instance, or None if unavailable.

    Args:
        env: Environment name

    Returns:
        JobRegistry instance or None
    """
    try:
        # Get storage connection from environment or Pulumi
        import os

        from ..core import automation

        # Try environment variable first
        connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")

        if not connection_string:
            # Try to get from Pulumi storage stack (same as JobSubmissionClient)
            try:
                outputs = automation.outputs("storage", env, refresh=False)
                if outputs and "connection_string" in outputs:
                    connection_string = automation.get_output_value(outputs, "connection_string")
            except Exception:
                pass

            # Also try infra stack as fallback
            if not connection_string:
                try:
                    outputs = automation.outputs("infra", env, refresh=False)
                    if outputs and "storage_connection_string" in outputs:
                        connection_string = automation.get_output_value(
                            outputs, "storage_connection_string"
                        )
                except Exception:
                    pass

        if not connection_string:
            return None

        # Create versioned store and registry
        store = AzureVersionedStore(connection_string=connection_string, container="job-registry")
        return JobRegistry(store)

    except Exception as e:
        warning(f"Job registry unavailable: {e}")
        return None


def _load_local_registry(project_root: Path):
    """Load the local bundle registry."""
    from modelops_contracts import BundleRegistry

    root = project_root.resolve()
    registry_path = root / ".modelops-bundle" / "registry.yaml"
    if not registry_path.exists():
        raise FileNotFoundError(f"No registry at {registry_path}")
    registry = BundleRegistry.load(registry_path)
    return registry, registry_path


def _resolve_registry_targets(
    target_ids: Optional[List[str]],
    target_set: Optional[str],
    project_root: Path,
) -> Tuple[List[Tuple[str, str]], Optional[str]] | None:
    """Resolve targets from registry returning (id, entrypoint)."""
    if not target_set and not target_ids:
        return None

    registry, registry_path = _load_local_registry(project_root)
    if target_set:
        target_set_obj = registry.target_sets.get(target_set)
        if not target_set_obj:
            available = ", ".join(sorted(registry.target_sets.keys()))
            suffix = f" Available sets: {available}" if available else ""
            raise ValueError(f"Target set '{target_set}' not found in {registry_path}.{suffix}")
        selected = list(target_set_obj.targets)
    else:
        selected = list(dict.fromkeys(target_ids or []))

    if not selected:
        raise ValueError("No targets specified for override.")

    missing = [tid for tid in selected if tid not in registry.targets]
    if missing:
        raise ValueError(f"Unknown target id(s): {', '.join(missing)}")

    resolved = [(tid, registry.targets[tid].entrypoint) for tid in selected]
    return resolved, target_set


def _get_default_targets(project_root: Path) -> List[str] | None:
    """Get all registered targets as default when none specified.

    Returns:
        List of target entrypoints, or None if no registry/targets found
    """
    try:
        registry, _ = _load_local_registry(project_root)
        if registry and registry.targets:
            targets = [t.entrypoint for t in registry.targets.values()]
            logger.info(f"Using all {len(targets)} registered targets as default")
            return targets
        else:
            logger.debug("No targets found in registry")
            return None
    except Exception as e:
        logger.debug(f"Could not load registry defaults: {e}")
        return None


@dataclass
class ResolvedTargets:
    """Result of target resolution."""
    target_entrypoints: List[str]
    target_ids: List[str] | None = None
    target_set: str | None = None
    source: str = "unknown"  # "cli", "registry_default", "spec_file"


def _resolve_targets_for_job(
    target_ids_arg: List[str] | None,
    target_set_arg: str | None,
    spec_file_targets: List[str] | None,
    project_root: Path,
    use_registry_default: bool = True,
) -> ResolvedTargets | None:
    """Resolve targets from CLI args, spec file, or registry defaults.

    Priority order:
    1. CLI arguments (--target-set or --target)
    2. Spec file targets (if provided)
    3. Registry defaults (if use_registry_default=True)
    4. None

    Args:
        target_ids_arg: --target CLI arguments
        target_set_arg: --target-set CLI argument
        spec_file_targets: Targets from spec JSON file
        project_root: Project root for registry lookup
        use_registry_default: Whether to default to all registry targets

    Returns:
        ResolvedTargets or None if no targets found anywhere
    """
    # Priority 1: CLI arguments
    if target_set_arg or target_ids_arg:
        resolved = _resolve_registry_targets(target_ids_arg, target_set_arg, project_root)
        if resolved:
            entries, resolved_set = resolved
            return ResolvedTargets(
                target_entrypoints=[entry for _, entry in entries],
                target_ids=[tid for tid, _ in entries],
                target_set=resolved_set,
                source="cli"
            )

    # Priority 2: Spec file targets
    if spec_file_targets:
        logger.debug(f"Using {len(spec_file_targets)} targets from spec file")
        return ResolvedTargets(
            target_entrypoints=spec_file_targets,
            source="spec_file"
        )

    # Priority 3: Registry defaults
    if use_registry_default:
        default_targets = _get_default_targets(project_root)
        if default_targets:
            return ResolvedTargets(
                target_entrypoints=default_targets,
                source="registry_default"
            )

    # No targets found
    return None


def _trigger_result_indexing(job_id: str, batch_v1, namespace: str, verbose: bool = True):
    """Trigger result indexing as a K8s Job.

    Creates a single-shot K8s Job that runs the result indexer for the given job.

    Args:
        job_id: Job ID to index results for
        batch_v1: Kubernetes batch API client
        namespace: Kubernetes namespace for the indexer job
        verbose: Whether to print progress messages
    """
    from kubernetes import client as k8s_client

    from ..images import get_image_config

    try:
        # Get worker image for indexer
        image_config = get_image_config("worker")
        worker_image = image_config["full_tag"]

        # Create K8s Job for indexing
        indexer_name = f"mops-results-indexer-{job_id[:8]}"

        # Check if indexer job already exists
        try:
            existing = batch_v1.read_namespaced_job(name=indexer_name, namespace=namespace)
            if verbose:
                info(f"    Result indexer already exists for {job_id}")
            return
        except k8s_client.ApiException as e:
            if e.status != 404:
                raise

        # Create indexer job spec
        job_spec = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=indexer_name,
                namespace=namespace,
                labels={
                    "app": "modelops-indexer",
                    "job-id": job_id[:8],
                    "component": "result-indexer",
                },
            ),
            spec=k8s_client.V1JobSpec(
                ttl_seconds_after_finished=3600,  # Clean up after 1 hour
                backoff_limit=2,
                template=k8s_client.V1PodTemplateSpec(
                    metadata=k8s_client.V1ObjectMeta(
                        labels={
                            "app": "modelops-indexer",
                            "job-id": job_id[:8],
                        }
                    ),
                    spec=k8s_client.V1PodSpec(
                        restart_policy="OnFailure",
                        containers=[
                            k8s_client.V1Container(
                                name="indexer",
                                image=worker_image,
                                command=["python", "-m", "modelops.cli.main"],
                                args=["results", "index", job_id],
                                env=[
                                    k8s_client.V1EnvVar(name="MODELOPS_JOB_ID", value=job_id),
                                    # Pass through storage configuration
                                    k8s_client.V1EnvVar(
                                        name="AZURE_STORAGE_CONNECTION_STRING",
                                        value_from=k8s_client.V1EnvVarSource(
                                            secret_key_ref=k8s_client.V1SecretKeySelector(
                                                name="modelops-storage",
                                                key="connection-string",
                                                optional=True,
                                            )
                                        ),
                                    ),
                                ],
                                resources=k8s_client.V1ResourceRequirements(
                                    requests={"cpu": "0.5", "memory": "1Gi"},
                                    limits={"cpu": "1", "memory": "2Gi"},
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )

        # Create the indexer job
        batch_v1.create_namespaced_job(namespace=namespace, body=job_spec)

        if verbose:
            info(f"    Triggered result indexer: {indexer_name}")

    except Exception as e:
        if verbose:
            warning(f"    Failed to trigger result indexing: {e}")


def detect_spec_type(spec_data: dict) -> str:
    """Detect whether spec is SimulationStudy or CalibrationSpec.

    Detection logic:
    1. If has 'kind' field, use that
    2. If has 'algorithm' field -> CalibrationSpec
    3. If has 'parameter_sets' field -> SimulationStudy
    4. If has 'target_data' field -> CalibrationSpec
    5. If has 'sampling_method' field -> SimulationStudy
    """
    # Check for explicit kind field
    if "kind" in spec_data:
        return spec_data["kind"]

    # Check for distinguishing fields
    if "algorithm" in spec_data:
        return "CalibrationSpec"

    if "parameter_sets" in spec_data:
        return "SimulationStudy"

    if "target_data" in spec_data and "observed_file" in spec_data.get("target_data", {}):
        return "CalibrationSpec"

    if "sampling_method" in spec_data:
        return "SimulationStudy"

    # Default based on other heuristics
    if "max_iterations" in spec_data and "algorithm_config" in spec_data:
        return "CalibrationSpec"

    raise ValueError(
        "Cannot determine spec type. Add 'kind' field or ensure spec has "
        "distinguishing fields (algorithm for CalibrationSpec, parameter_sets for SimulationStudy)"
    )


@app.command()
def submit(
    spec_file: Path = typer.Argument(
        ...,
        help="Job specification file (SimulationStudy or CalibrationSpec)",
        exists=True,
        file_okay=True,
        readable=True,
    ),
    bundle: str | None = typer.Option(
        None, "--bundle", "-b", help="Explicit bundle reference (sha256:...)"
    ),
    auto: bool = typer.Option(
        True,
        "--auto/--no-auto",
        help="Auto-push bundle from current directory (default: True)",
    ),
    target_set: Optional[str] = typer.Option(
        None,
        "--target-set",
        help="Override targets using a named set from .modelops-bundle/registry.yaml (applies to both simulation studies and calibration specs)",
    ),
    target_ids: Optional[List[str]] = typer.Option(
        None,
        "--target",
        "-t",
        help="Override targets by id (repeatable). Applies to both simulation studies and calibration specs.",
    ),
    project_root: Path = typer.Option(
        Path("."),
        "--project-root",
        help="Project root containing .modelops-bundle/registry.yaml (default: cwd)",
    ),
    env: str | None = env_option(),
):
    """Submit a job specification (SimulationStudy or CalibrationSpec) as a K8s Job.

    Automatically detects the type of specification and routes to appropriate
    submission logic. Specs can be generated by:
    - SimulationStudy: 'cb sampling sobol' or 'cb sampling grid'
    - CalibrationSpec: 'cb calibration optuna' or 'cb calibration abc'

    By default, auto-pushes the bundle from the current directory.

    Examples:
        # Submit simulation study (auto-detects type)
        mops jobs submit study.json

        # Submit calibration spec (auto-detects type)
        mops jobs submit calibration_spec.json

        # Use explicit bundle
        mops jobs submit spec.json --bundle sha256:abc123...
    """
    # Use config default if env not specified
    from .utils import resolve_env

    env = resolve_env(env)
    project_root = project_root.resolve()

    # Load spec from JSON
    section("Loading job specification")
    try:
        with open(spec_file) as f:
            spec_data = json.load(f)
    except Exception as e:
        error(f"Failed to load spec file: {e}")
        raise typer.Exit(1)

    # Detect spec type
    try:
        spec_type = detect_spec_type(spec_data)
        info(f"  Type: {spec_type}")
    except ValueError as e:
        error(f"Failed to detect spec type: {e}")
        error("Hint: Add 'kind' field to your spec for explicit typing")
        raise typer.Exit(1)

    # Route based on type
    target_override = None

    if spec_type == "CalibrationSpec":
        # Parse CalibrationSpec
        try:
            spec = CalibrationSpec(
                model=spec_data["model"],
                scenario=spec_data["scenario"],
                algorithm=spec_data["algorithm"],
                target_data=spec_data["target_data"],
                max_iterations=spec_data["max_iterations"],
                convergence_criteria=spec_data.get("convergence_criteria", {}),
                algorithm_config=spec_data.get("algorithm_config", {}),
                outputs=spec_data.get("outputs"),
                metadata=spec_data.get("metadata", {}),
            )

            info(f"  Model: {spec.model}/{spec.scenario}")
            info(f"  Algorithm: {spec.algorithm}")
            info(f"  Max iterations: {spec.max_iterations}")
            if "parameter_specs" in spec.algorithm_config:
                info(f"  Parameters: {', '.join(spec.algorithm_config['parameter_specs'].keys())}")

        except Exception as e:
            error(f"Failed to parse CalibrationSpec: {e}")
            raise typer.Exit(1)

        # For CalibrationSpec, we'll submit as a calibration job
        is_calibration = True
        job_obj = spec

        # Resolve targets using unified function (CalibrationSpec requires explicit targets)
        if target_set or target_ids:
            try:
                resolved = _resolve_targets_for_job(
                    target_ids_arg=target_ids,
                    target_set_arg=target_set,
                    spec_file_targets=None,  # CalibrationSpec doesn't have spec file targets
                    project_root=project_root,
                    use_registry_default=False,  # CalibrationSpec requires explicit targets
                )
            except (FileNotFoundError, ValueError) as exc:
                error(f"Failed to resolve targets: {exc}")
                raise typer.Exit(1)

            if resolved and resolved.source == "cli":
                # Apply to CalibrationSpec
                job_obj.target_data["target_entrypoints"] = resolved.target_entrypoints
                job_obj.target_data["target_ids"] = resolved.target_ids
                job_obj.metadata.setdefault("target_ids", resolved.target_ids)
                if resolved.target_set:
                    job_obj.metadata["target_set"] = resolved.target_set
                info(f"  Using targets: {', '.join(resolved.target_ids)}")
                if resolved.target_set:
                    info(f"  Target set: {resolved.target_set}")

    else:
        # Parse SimulationStudy
        # Extract just the parameter dictionaries (not UniqueParameterSet objects)
        parameter_sets = [
            ps["params"] if isinstance(ps, dict) and "params" in ps else ps
            for ps in spec_data.get("parameter_sets", [])
        ]

        study = SimulationStudy(
            model=spec_data["model"],
            scenario=spec_data["scenario"],
            parameter_sets=parameter_sets,
            sampling_method=spec_data["sampling_method"],
            n_replicates=spec_data.get("n_replicates", 1),
            outputs=spec_data.get("outputs"),
            targets=spec_data.get("targets"),  # From JSON spec
            metadata=spec_data.get("metadata", {}),
        )

        # Resolve targets using unified function
        resolved = _resolve_targets_for_job(
            target_ids_arg=target_ids,
            target_set_arg=target_set,
            spec_file_targets=study.targets,  # From JSON spec
            project_root=project_root,
            use_registry_default=True,  # SimulationStudy defaults to all targets
        )

        # Apply resolved targets to study
        if resolved:
            info(f"  Targets resolved from {resolved.source}: {len(resolved.target_entrypoints)} target(s)")

            # Create updated study with resolved targets
            study = study.with_targets(
                targets=resolved.target_entrypoints,
                target_ids=resolved.target_ids,
                target_set=resolved.target_set,
            )
        else:
            info("  No targets specified - job will run without target evaluation")

        info(f"  Model: {study.model}/{study.scenario}")
        info(f"  Sampling: {study.sampling_method}")
        info(f"  Parameters: {study.parameter_count()} unique sets")
        info(f"  Replicates: {study.n_replicates} per parameter set")
        info(f"  Total simulations: {study.total_simulation_count()}")
        if study.targets:
            info(f"  Targets: {', '.join(study.targets)}")

        is_calibration = False
        job_obj = study

    # Determine bundle reference
    if bundle:
        # Explicit bundle overrides auto
        bundle_ref = bundle
        info(f"\n Using explicit bundle: {bundle_ref[:20]}...")
    elif auto:
        # Auto-push bundle from current directory (default)
        section("Auto-pushing bundle")
        try:
            from modelops_bundle.api import push_dir
            from modelops_bundle.ops import load_config

            info("  Building and pushing bundle from current directory...")
            digest = push_dir(".")

            # Get repository name from bundle config
            try:
                config = load_config()
                registry_ref = config.registry_ref  # e.g. "acr.io/my-project"

                # Extract repository name from registry_ref
                if "/" in registry_ref:
                    repository_name = registry_ref.split("/", 1)[1]
                    bundle_ref = f"{repository_name}@{digest}"
                else:
                    # Fallback to digest-only if parsing fails
                    warning(f"  Could not parse repository from registry_ref: {registry_ref}")
                    bundle_ref = digest
            except Exception as e:
                warning(f"  Could not load bundle config: {e}")
                warning("  Using digest-only reference")
                bundle_ref = digest

            success(f"  ✓ Pushed bundle: {bundle_ref[:50]}...")

        except ImportError:
            error("\nAuto-push requires modelops-bundle. Install with:")
            error("  uv pip install 'modelops[full]'")
            raise typer.Exit(1)
        except FileNotFoundError:
            error("\nCurrent directory is not a bundle project.")
            error("Initialize with: modelops-bundle init .")
            raise typer.Exit(1)
        except Exception as e:
            error(f"\nBundle push failed: {e}")
            raise typer.Exit(1)
    else:
        error("Must specify either --bundle or use --auto (default)")
        raise typer.Exit(1)

    # Submit using client
    section(f"Submitting {'calibration' if is_calibration else 'simulation'} job")
    client = JobSubmissionClient(env=env)

    try:
        if is_calibration:
            # Submit calibration job
            import uuid

            from modelops_contracts import CalibrationJob, TargetSpec

            # Create target spec from calibration spec
            target_spec = TargetSpec(
                data=job_obj.target_data,
                loss_function="default",
                metadata={"targets": job_obj.target_data.get("target_entrypoints", [])},
            )

            # Create calibration job
            # Add model and scenario to algorithm_config for the runner
            algorithm_config = job_obj.algorithm_config.copy()
            algorithm_config["model"] = job_obj.model
            algorithm_config["scenario"] = job_obj.scenario

            calib_job = CalibrationJob(
                job_id=f"calib-{uuid.uuid4().hex[:8]}",
                algorithm=job_obj.algorithm,
                bundle_ref=bundle_ref,
                target_spec=target_spec,
                max_iterations=job_obj.max_iterations,
                convergence_criteria=job_obj.convergence_criteria,
                algorithm_config=algorithm_config,
            )

            job_id = client.submit_job(calib_job)
        else:
            # Submit simulation job
            job_id = client.submit_sim_job(
                study=job_obj, bundle_strategy="explicit", bundle_ref=bundle_ref
            )

        success("\n✓ Job submitted successfully!")
        info(f"  Job ID: {job_id}")
        info(f"  Environment: {env}")
        info("  Status: Running")

        # Show how to monitor the job
        info("\n To monitor job execution:")
        info("  # Port-forward to access Dask dashboard (run in separate terminals or use &)")
        info("  kubectl port-forward -n modelops-dask-dev svc/dask-scheduler 8787:8787 &")
        info("  kubectl port-forward -n modelops-dask-dev svc/dask-scheduler 8786:8786 &")
        info("  # Then open http://localhost:8787 in your browser")

        info("\n To check job status:")
        info(f"  kubectl -n modelops-dask-dev get job {job_id}")
        info("\n To see logs:")
        info(f"  kubectl -n modelops-dask-dev logs job/{job_id}")
        info("  kubectl -n modelops-dask-dev logs deployment/dask-workers")

    except Exception as e:
        error(f"Job submission failed: {e}")
        raise typer.Exit(1)


@app.command(deprecated=True, hidden=True)
def submit_calibration(
    spec_file: Path = typer.Argument(
        ...,
        help="CalibrationSpec JSON file",
        exists=True,
        file_okay=True,
        readable=True,
    ),
    bundle: str | None = typer.Option(None, "--bundle", "-b", help="Explicit bundle reference"),
    build: bool = typer.Option(False, "--build", help="Build and push bundle"),
    latest: bool = typer.Option(False, "--latest", help="Use latest bundle"),
    env: str | None = env_option(),
):
    """Submit a CalibrationSpec as a K8s Job.

    Calibration jobs run an adaptive algorithm (e.g., Optuna) that
    iteratively generates parameters and evaluates them.
    """
    env = env or "dev"

    # Load calibration spec
    section("Loading calibration specification")
    try:
        with open(spec_file) as f:
            spec_data = json.load(f)

        spec = CalibrationSpec(
            model=spec_data["model"],
            scenario=spec_data["scenario"],
            algorithm=spec_data["algorithm"],
            target_data=spec_data["target_data"],
            max_iterations=spec_data["max_iterations"],
            convergence_criteria=spec_data.get("convergence_criteria", {}),
            algorithm_config=spec_data.get("algorithm_config", {}),
            outputs=spec_data.get("outputs"),
            metadata=spec_data.get("metadata", {}),
        )

        info(f"  Model: {spec.model}/{spec.scenario}")
        info(f"  Algorithm: {spec.algorithm}")
        info(f"  Max iterations: {spec.max_iterations}")

    except Exception as e:
        error(f"Failed to load calibration spec: {e}")
        raise typer.Exit(1)

    # Determine bundle strategy
    if build:
        strategy = "build"
        bundle_ref = None
    elif latest:
        strategy = "latest"
        bundle_ref = None
    elif bundle:
        strategy = "explicit"
        bundle_ref = bundle
    else:
        error("Must specify --bundle, --latest, or --build")
        raise typer.Exit(1)

    # Submit using client
    section("Submitting calibration job")
    client = JobSubmissionClient(env=env)

    try:
        job_id = client.submit_calibration_job(
            spec=spec, bundle_strategy=strategy, bundle_ref=bundle_ref
        )

        success("\n✓ Calibration job submitted successfully!")
        info(f"  Job ID: {job_id}")
        info(f"  Environment: {env}")

    except Exception as e:
        error(f"Calibration submission failed: {e}")
        raise typer.Exit(1)


@app.command()
def status(
    job_id: str = typer.Argument(..., help="Job ID to check"),
    env: str | None = env_option(),
):
    """Check the status of a submitted job.

    Queries the job registry for current status and progress information.
    Falls back to kubectl if registry is unavailable.
    """
    from .utils import resolve_env

    env = resolve_env(env)

    # Try to get status from registry
    registry = _get_registry(env)
    if not registry:
        warning("Job registry unavailable, use kubectl:")
        info(f"  kubectl -n modelops-dask-{env} get job job-{job_id}")
        raise typer.Exit(1)

    # Get job state
    job_state = registry.get_job(job_id)
    if not job_state:
        error(f"Job {job_id} not found in registry")
        info("\n Try kubectl to check if it exists:")
        info(f"  kubectl -n modelops-dask-{env} get job job-{job_id}")
        raise typer.Exit(1)

    # Display status
    section(f"Job Status: {job_id}")

    # Status with color coding
    status_color = {
        JobStatus.PENDING: "yellow",
        JobStatus.SUBMITTING: "yellow",
        JobStatus.SCHEDULED: "cyan",
        JobStatus.RUNNING: "blue",
        JobStatus.SUCCEEDED: "green",
        JobStatus.FAILED: "red",
        JobStatus.CANCELLED: "magenta",
    }
    color = status_color.get(job_state.status, "white")
    info(f"  Status: [{color}]{job_state.status.value}[/]")

    # Basic info with local timezone
    info(f"  Created: {format_timestamp(job_state.created_at)}")
    info(f"  Updated: {format_timestamp(job_state.updated_at)}")

    # Show duration if job is running or completed
    if job_state.status in [JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.FAILED]:
        duration = format_duration(job_state.created_at, job_state.updated_at)
        info(f"  Duration: {duration}")

    # Kubernetes info if available
    if job_state.k8s_name:
        info(f"  K8s Job: {job_state.k8s_name}")
    if job_state.k8s_namespace:
        info(f"  Namespace: {job_state.k8s_namespace}")

    # Progress if available
    if job_state.tasks_total > 0:
        progress = job_state.progress_percent or 0
        info(f"  Progress: {job_state.tasks_completed}/{job_state.tasks_total} ({progress:.1f}%)")

    # Results or errors
    if job_state.results_path:
        success(f"  Results: {job_state.results_path}")
    if job_state.error_message:
        error(f"  Error: {job_state.error_message}")
    if job_state.error_code:
        error(f"  Error Code: {job_state.error_code}")

    # Metadata if present
    if job_state.metadata:
        info("\n📋 Metadata:")
        for key, value in job_state.metadata.items():
            info(f"    {key}: {value}")


@app.command()
def logs(
    job_id: str = typer.Argument(..., help="Job ID to get logs for"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    env: str | None = env_option(),
):
    """Get logs for a running or completed job.

    This is a placeholder for future implementation that would
    stream logs from the K8s Job pod.
    """
    env = env or "dev"

    warning("Logs command not yet implemented")
    info("\n For now, use kubectl directly:")
    if follow:
        info(f"  kubectl -n modelops-dask-dev logs -f job/job-{job_id}")
    else:
        info(f"  kubectl -n modelops-dask-dev logs job/job-{job_id}")


@app.command()
def list(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of jobs to show"),
    status: str | None = typer.Option(None, "--status", "-s", help="Filter by status"),
    hours: int = typer.Option(24, "--hours", "-h", help="Show jobs from last N hours"),
    env: str | None = env_option(),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip automatic status sync"),
):
    """List recent jobs.

    Shows jobs from the registry with their current status and progress.
    Automatically syncs status with Kubernetes before displaying.
    """
    from datetime import datetime, timedelta

    from .utils import resolve_env

    env = resolve_env(env)

    # Get registry
    registry = _get_registry(env)
    if not registry:
        warning("Job registry unavailable, use kubectl:")
        info(f"  kubectl -n modelops-dask-{env} get jobs")
        raise typer.Exit(1)

    # Auto-sync status with spinner (unless disabled)
    if not no_sync:
        # Get active jobs that need syncing
        active_jobs = registry.list_jobs(
            status_filter=[
                JobStatus.PENDING,
                JobStatus.SUBMITTING,
                JobStatus.SCHEDULED,
                JobStatus.RUNNING,
            ]
        )

        if active_jobs:
            # Setup Kubernetes client
            temp_path = None
            try:
                from kubernetes import client, config
                from rich.progress import Progress, SpinnerColumn, TextColumn

                # Configure kubectl
                infra_state = load_infrastructure_state(env)
                if infra_state and infra_state.get("kubeconfig"):
                    temp_path = write_temp_kubeconfig(infra_state["kubeconfig"])
                    config.load_kube_config(config_file=temp_path)
                else:
                    config.load_kube_config()

                batch_v1 = client.BatchV1Api()

                # Sync with progress spinner
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                    transient=True,
                ) as progress:
                    task = progress.add_task("Syncing job status...", total=None)

                    _perform_sync(
                        registry=registry,
                        batch_v1=batch_v1,
                        active_jobs=active_jobs,
                        dry_run=False,
                        validate=True,
                        progress=progress,
                        verbose=False,  # Quiet during spinner
                    )

                    progress.update(task, completed=True)

            except Exception:
                # Silently skip sync on error, still show list
                pass

            finally:
                if temp_path:
                    cleanup_temp_kubeconfig(temp_path)

    # Parse status filter if provided
    status_filter = None
    if status:
        try:
            status_filter = [JobStatus(status.lower())]
        except ValueError:
            error(f"Invalid status: {status}")
            info(
                "Valid statuses: pending, submitting, scheduled, running, succeeded, failed, cancelled"
            )
            raise typer.Exit(1)

    # Get recent jobs
    since = datetime.now(UTC) - timedelta(hours=hours)
    jobs = registry.list_jobs(limit=limit, status_filter=status_filter, since=since)

    if not jobs:
        info(f"No jobs found in the last {hours} hours")
        return

    # Display jobs in a table
    from rich.table import Table

    # Get timezone info for table title
    tz_info = get_timezone_info()
    table = Table(title=f"Recent Jobs (last {hours} hours, {tz_info})")
    table.add_column("Job ID", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Progress")
    table.add_column("Created", style="dim")
    table.add_column("Updated", style="dim")

    for job in jobs:
        # Color code status
        status_style = {
            JobStatus.PENDING: "yellow",
            JobStatus.SUBMITTING: "yellow",
            JobStatus.SCHEDULED: "cyan",
            JobStatus.RUNNING: "blue",
            JobStatus.SUCCEEDED: "green",
            JobStatus.FAILED: "red",
            JobStatus.CANCELLED: "magenta",
        }
        style = status_style.get(job.status, "white")
        status_str = f"[{style}]{job.status.value}[/]"

        # Format progress
        if job.tasks_total > 0:
            progress = f"{job.tasks_completed}/{job.tasks_total}"
        else:
            progress = "-"

        # Format dates using the new formatter
        created_str = format_timestamp(job.created_at)
        updated_str = format_timestamp(job.updated_at)

        table.add_row(job.job_id, status_str, progress, created_str, updated_str)

    console.print(table)

    # Show summary
    info(f"\n Showing {len(jobs)} of {limit} most recent jobs")

    # Count by status
    counts = registry.count_jobs_by_status()
    active_count = sum(
        counts[s]
        for s in [
            JobStatus.PENDING,
            JobStatus.SUBMITTING,
            JobStatus.SCHEDULED,
            JobStatus.RUNNING,
        ]
    )
    if active_count > 0:
        info(f"  Active jobs: {active_count}")

    info("\n To see job details, use:")
    info("  mops jobs status <job-id>")


def _perform_sync(
    registry,
    batch_v1,
    active_jobs: list,
    dry_run: bool = False,
    validate: bool = True,
    progress_task=None,
    progress=None,
    verbose: bool = True,
) -> int:
    """Perform the actual sync operation.

    Returns number of jobs updated.
    """
    updated_count = 0
    total_jobs = len(active_jobs)

    for idx, job in enumerate(active_jobs):
        # Update progress if provided
        if progress and progress_task is not None:
            progress.update(
                progress_task,
                description=f"Syncing job status from Kubernetes... [{idx + 1}/{total_jobs}]",
            )

        # Get Kubernetes job status
        try:
            k8s_job = batch_v1.read_namespaced_job(
                name=job.k8s_name, namespace=job.k8s_namespace or "modelops-dask-dev"
            )

            # Determine status based on Kubernetes job
            k8s_status = None
            if k8s_job.status.succeeded and k8s_job.status.succeeded > 0:
                k8s_status = JobStatus.SUCCEEDED
            elif k8s_job.status.failed and k8s_job.status.failed > 0:
                k8s_status = JobStatus.FAILED
            elif k8s_job.status.active and k8s_job.status.active > 0:
                k8s_status = JobStatus.RUNNING
            else:
                # Still scheduled/pending
                continue

            # Update if status changed
            if k8s_status and k8s_status != job.status:
                if dry_run:
                    if verbose:
                        info(f"  {job.job_id}: {job.status.value} → {k8s_status.value}")
                else:
                    try:
                        # Handle K8s job success with validation
                        if k8s_status == JobStatus.SUCCEEDED and validate:
                            # Transition to VALIDATING if currently RUNNING
                            if job.status == JobStatus.RUNNING:
                                registry.update_job_status(job.job_id, JobStatus.VALIDATING)
                                # Note: A separate validation process will handle the actual validation
                                if verbose:
                                    info(f"  {job.job_id}: {job.status.value} → validating")
                        else:
                            registry.update_job_status(job.job_id, k8s_status)
                            if verbose:
                                info(f"  {job.job_id}: {job.status.value} → {k8s_status.value}")

                            # Trigger result indexing for succeeded jobs
                            if k8s_status == JobStatus.SUCCEEDED:
                                _trigger_result_indexing(
                                    job.job_id, batch_v1, job.k8s_namespace, verbose
                                )
                        updated_count += 1
                    except Exception as e:
                        if verbose:
                            warning(f"  Failed to update {job.job_id}: {e}")

        except Exception as e:
            # Check if it's a 404 (job doesn't exist)
            if "404" in str(e) or "not found" in str(e).lower():
                if verbose:
                    info(f"  {job.job_id}: K8s job not found (may have been deleted)")
                # Mark as failed if it was running but now missing
                if job.status == JobStatus.RUNNING and not dry_run:
                    try:
                        registry.update_job_status(job.job_id, JobStatus.FAILED)
                        if verbose:
                            info("    → marked as failed")
                        updated_count += 1
                    except Exception as update_e:
                        if verbose:
                            warning(f"  Failed to update {job.job_id}: {update_e}")
            else:
                if verbose:
                    warning(f"  Error checking {job.job_id}: {e}")

    return updated_count


@app.command()
def sync(
    env: str | None = env_option(),
    validate: bool = typer.Option(
        True, "--validate/--no-validate", help="Validate outputs after sync"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be updated without making changes"
    ),
):
    """Sync job status from Kubernetes to the registry.

    Updates the registry to reflect actual Kubernetes job status.
    Optionally validates outputs when jobs complete to ensure all expected files exist.
    """
    from ..cli.k8s_client import cleanup_temp_kubeconfig, get_k8s_client
    from .utils import resolve_env

    env = resolve_env(env)

    # Get registry
    registry = _get_registry(env)
    if not registry:
        error("Job registry unavailable")
        raise typer.Exit(1)

    # Get Kubernetes client
    try:
        v1, apps_v1, temp_path = get_k8s_client(env)
        from kubernetes import client as k8s_client

        batch_v1 = k8s_client.BatchV1Api()
    except Exception as e:
        error(f"Failed to connect to Kubernetes: {e}")
        raise typer.Exit(1)

    try:
        # Get active jobs from registry
        active_jobs = registry.get_active_jobs()

        if not active_jobs:
            info("No active jobs to sync")
            return

        # Use progress spinner for sync operation
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,  # Disappears when done
        ) as progress:
            task = progress.add_task(
                f"Syncing {len(active_jobs)} active {'job' if len(active_jobs) == 1 else 'jobs'}...",
                total=None,
            )

            updated_count = _perform_sync(
                registry=registry,
                batch_v1=batch_v1,
                active_jobs=active_jobs,
                dry_run=dry_run,
                validate=validate,
                progress_task=task,
                progress=progress,
                verbose=False,  # Quiet during spinner
            )

            progress.update(task, completed=True)

    finally:
        if temp_path:
            cleanup_temp_kubeconfig(temp_path)


@app.command()
def resume(
    job_id: str = typer.Argument(..., help="Job ID to resume"),
    env: str | None = env_option(),
    bundle: str | None = typer.Option(None, help="Override bundle reference"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be submitted without actually doing it",
    ),
):
    """Resume a partially completed job.

    Resubmits only the missing tasks from a job that completed with PARTIAL_SUCCESS status.
    This allows recovery from transient failures without re-running successful tasks.
    """
    from modelops_contracts import SimJob, UniqueParameterSet

    from ..client.job_submission import JobSubmissionClient
    from .utils import resolve_env

    env = resolve_env(env)

    # Get registry
    registry = _get_registry(env)
    if not registry:
        error("Job registry unavailable")
        raise typer.Exit(1)

    # Get job state
    job_state = registry.get_job(job_id)
    if not job_state:
        error(f"Job {job_id} not found")
        raise typer.Exit(1)

    # Check if job is resumable
    if job_state.status != JobStatus.PARTIAL_SUCCESS:
        error(f"Job {job_id} is not in PARTIAL_SUCCESS state (current: {job_state.status.value})")
        info("\n Only jobs with partial success can be resumed")
        raise typer.Exit(1)

    # Get resumable tasks
    resumable_tasks = registry.get_resumable_tasks(job_id)
    if not resumable_tasks:
        warning(f"No resumable tasks found for job {job_id}")
        raise typer.Exit(0)

    section(f"Resuming job {job_id}")
    info(f"Found {len(resumable_tasks)} tasks to resume")

    if dry_run:
        info("\n[Dry run mode - no actual submission]")
        info(f"\nWould submit {len(resumable_tasks)} tasks:")
        for i, task in enumerate(resumable_tasks[:5]):
            info(f"  • param_id={task.param_id[:8]}... seed={task.seed}")
        if len(resumable_tasks) > 5:
            info(f"  ... and {len(resumable_tasks) - 5} more")
        return

    # Create a new job spec with only the missing tasks
    # Group tasks by param_id to reconstruct parameter sets
    tasks_by_param = {}
    for task in resumable_tasks:
        if task.param_id not in tasks_by_param:
            tasks_by_param[task.param_id] = {"params": task.params, "seeds": []}
        tasks_by_param[task.param_id]["seeds"].append(task.seed)

    # Create parameter sets for the resume job
    parameter_sets = []
    for param_id, data in tasks_by_param.items():
        param_set = UniqueParameterSet(
            param_id=param_id, params=data["params"], replicate_count=len(data["seeds"])
        )
        parameter_sets.append(param_set)

    # Create resume job specification
    resume_job = SimJob(
        job_id=f"{job_id}-resume-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        parameter_sets=parameter_sets,
        metadata={
            "original_job_id": job_id,
            "bundle_digest": bundle or job_state.metadata.get("bundle_digest", "unknown"),
            "resume_attempt": job_state.metadata.get("resume_attempt", 0) + 1,
        },
    )

    # Submit the resume job
    info("\n Submitting resume job...")
    try:
        client = JobSubmissionClient(env=env)

        # Use bundle from command line or original job
        bundle_ref = bundle or job_state.metadata.get("bundle_ref")
        if not bundle_ref:
            error("Bundle reference not found. Please specify with --bundle")
            raise typer.Exit(1)

        new_job_id = client.submit_job(resume_job, bundle_ref=bundle_ref)

        success(f"\n✓ Resume job submitted: {new_job_id}")
        info(f"\nResuming {len(resumable_tasks)} tasks from job {job_id}")
        info("\n Track progress with:")
        info(f"  mops jobs status {new_job_id}")

    except Exception as e:
        error(f"Failed to submit resume job: {e}")
        raise typer.Exit(1)


@app.command()
def validate(
    job_id: str = typer.Argument(..., help="Job ID to validate"),
    env: str | None = env_option(),
    force: bool = typer.Option(
        False, "--force", help="Force re-validation even if already validated"
    ),
):
    """Manually validate job outputs.

    Checks ProvenanceStore to verify all expected outputs exist.
    Useful for re-validating jobs or checking jobs that completed without validation.
    """
    from .utils import resolve_env

    env = resolve_env(env)

    # Get registry
    registry = _get_registry(env)
    if not registry:
        error("Job registry unavailable")
        raise typer.Exit(1)

    # Get job state
    job_state = registry.get_job(job_id)
    if not job_state:
        error(f"Job {job_id} not found")
        raise typer.Exit(1)

    # Check if already validated (unless forced)
    if not force and job_state.validation_completed_at:
        info(f"Job {job_id} was already validated at {job_state.validation_completed_at}")
        info(f"Status: {job_state.status.value}")
        info(f"Verified: {job_state.tasks_verified} outputs")
        if job_state.missing_outputs:
            info(f"Missing: {len(job_state.missing_outputs)} outputs")
        info("\n Use --force to re-validate")
        return

    section(f"Validating job {job_id}")

    # Perform validation
    info("Checking outputs in ProvenanceStore...")
    validation_result = registry.validate_outputs(job_id)

    # Display results
    if validation_result.status == "unavailable":
        warning(f"Validation unavailable: {validation_result.error}")
        raise typer.Exit(1)

    info("\n Validation Results:")
    info(f"  Status: {validation_result.status.upper()}")
    info(f"  Verified: {validation_result.verified_count} outputs")
    info(f"  Missing: {validation_result.missing_count} outputs")

    # Show sample of missing outputs if any
    if validation_result.missing_outputs and len(validation_result.missing_outputs) > 0:
        info("\n✗ Missing outputs (first 5):")
        for path in validation_result.missing_outputs[:5]:
            info(f"  • {path}")
        if len(validation_result.missing_outputs) > 5:
            info(f"  ... and {len(validation_result.missing_outputs) - 5} more")

    # Update job state if needed
    if job_state.status in [JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.FAILED]:
        info("\n Updating job state based on validation...")

        # Transition to validating first if not already
        if job_state.status != JobStatus.VALIDATING:
            registry.transition_to_validating(job_id)

        # Finalize with validation results
        updated_state = registry.finalize_with_validation(job_id, validation_result)
        success(f"\n✓ Job updated to {updated_state.status.value}")

        if updated_state.status == JobStatus.PARTIAL_SUCCESS:
            info("\n This job can be resumed with:")
            info(f"  mops jobs resume {job_id}")


if __name__ == "__main__":
    app()
