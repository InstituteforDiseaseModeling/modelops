"""Workspace management CLI commands for Dask deployment."""

from pathlib import Path

import typer

from ..client import WorkspaceService
from ..components import WorkspaceConfig
from ..core import StackNaming, automation
from ..core.automation import get_output_value
from ..core.paths import UNIFIED_CONFIG_FILE
from .common_options import env_option, yes_option
from .display import (
    console,
    error,
    info,
    info_dict,
    section,
    success,
    warning,
    workspace_commands,
    workspace_info,
)
from .utils import handle_pulumi_error, resolve_env

app = typer.Typer(help="Manage Dask workspaces")


@app.command()
def up(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Workspace configuration file (YAML)"
    ),
    infra_stack: str = typer.Option(
        StackNaming.get_project_name("infra"),
        "--infra-stack",
        help=f"Infrastructure stack name (default: {StackNaming.get_project_name('infra')}, auto-appends env for default)",
    ),
    env: str | None = env_option(),
):
    """Deploy Dask workspace on existing infrastructure.

    This creates Stack 2 which references Stack 1 (infrastructure) to get
    the kubeconfig and deploy Dask scheduler and workers.

    Example:
        mops workspace up                  # Uses ~/.modelops/modelops.yaml
        mops workspace up --env dev
        mops workspace up --config workspace.yaml --infra-stack modelops-infra-prod
    """
    env = resolve_env(env)

    # Smart default: look for unified modelops.yaml when no config provided
    validated_config = None

    if config:
        # Explicit config provided
        if not config.exists():
            error(f"Configuration file not found: {config}")
            info("Please provide a valid workspace configuration file")
            raise typer.Exit(1)
        validated_config = WorkspaceConfig.from_yaml(config)
    else:
        # No config provided, check for unified config
        if UNIFIED_CONFIG_FILE.exists():
            info(f"Using workspace config from: {UNIFIED_CONFIG_FILE}")
            try:
                # Load unified config and extract workspace section
                from ..core.unified_config import UnifiedModelOpsConfig

                unified_config = UnifiedModelOpsConfig.from_yaml(UNIFIED_CONFIG_FILE)
                # Convert WorkspaceSpec to WorkspaceConfig format
                validated_config = WorkspaceConfig(
                    apiVersion="modelops/v1",
                    kind="Workspace",
                    metadata={"name": "default-workspace"},
                    spec={
                        "scheduler": {
                            "image": unified_config.workspace.scheduler_image,
                            "resources": {
                                "requests": {
                                    "memory": unified_config.workspace.scheduler_memory,
                                    "cpu": unified_config.workspace.scheduler_cpu,
                                },
                                "limits": {
                                    "memory": unified_config.workspace.scheduler_memory,
                                    "cpu": unified_config.workspace.scheduler_cpu,
                                },
                            },
                        },
                        "workers": {
                            "replicas": unified_config.workspace.worker_replicas,
                            "image": unified_config.workspace.worker_image,
                            "resources": {
                                "requests": {
                                    "memory": unified_config.workspace.worker_memory,
                                    "cpu": unified_config.workspace.worker_cpu,
                                },
                                "limits": {
                                    "memory": unified_config.workspace.worker_memory,
                                    "cpu": unified_config.workspace.worker_cpu,
                                },
                            },
                            "processes": unified_config.workspace.worker_processes,
                            "threads": unified_config.workspace.worker_threads,
                        },
                        "autoscaling": {
                            "enabled": unified_config.workspace.autoscaling_enabled,
                            "min_workers": unified_config.workspace.autoscaling_min_workers,
                            "max_workers": unified_config.workspace.autoscaling_max_workers,
                            "target_cpu": unified_config.workspace.autoscaling_target_cpu,
                        },
                    },
                )
            except Exception as e:
                error(f"Failed to load workspace config from {UNIFIED_CONFIG_FILE}: {e}")
                raise typer.Exit(1)
        else:
            error("No configuration specified and no modelops.yaml found")
            error("Run 'mops init' to generate modelops.yaml")
            error("Or specify config: mops workspace up --config <workspace.yaml>")
            raise typer.Exit(1)

    # Validate dependencies before attempting to provision
    try:
        from ..client.utils import validate_component_dependencies

        info("Checking dependencies...")
        validate_component_dependencies("workspace", env)
        success("✓ All dependencies satisfied")
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1)

    # Use WorkspaceService - it handles all ref resolution internally
    service = WorkspaceService(env)

    # Only pass infra_stack_ref if it's not the default
    infra_stack_ref = None
    if infra_stack != StackNaming.get_project_name("infra"):
        # Custom stack name provided
        infra_stack_ref = StackNaming.ref_from_stack(infra_stack)

    try:
        info(f"\n[bold]Deploying Dask workspace to environment: {env}[/bold]")
        info(f"Workspace stack: {StackNaming.get_stack_name('workspace', env)}\n")

        info("[yellow]Creating Dask resources...[/yellow]")
        # Let the service handle all ref resolution - consistent with infra up
        outputs = service.provision(
            config=validated_config, infra_stack_ref=infra_stack_ref, verbose=False
        )

        success("\nWorkspace deployed successfully!")
        workspace_info(outputs, env, StackNaming.get_stack_name("workspace", env))

    except Exception as e:
        error(f"\nError deploying workspace: {e}")
        handle_pulumi_error(
            e,
            "~/.modelops/pulumi/workspace",
            StackNaming.get_stack_name("workspace", env),
        )
        raise typer.Exit(1)


@app.command()
def down(env: str | None = env_option(), yes: bool = yes_option()):
    """Destroy Dask workspace.

    This removes all Dask resources but leaves the underlying
    Kubernetes cluster intact.
    """
    env = resolve_env(env)

    if not yes:
        warning("\nWarning")
        info(f"This will destroy the Dask workspace in environment: {env}")
        info("All running Dask jobs will be terminated.")

        confirm = typer.confirm("\nAre you sure you want to destroy the workspace?")
        if not confirm:
            success("Destruction cancelled")
            raise typer.Exit(0)

    # Use WorkspaceService
    service = WorkspaceService(env)

    try:
        info(
            f"\n[yellow]Destroying workspace: {StackNaming.get_stack_name('workspace', env)}...[/yellow]"
        )
        service.destroy(verbose=False)
        success("\nWorkspace destroyed successfully")

    except Exception as e:
        error(f"\nError destroying workspace: {e}")
        handle_pulumi_error(
            e,
            "~/.modelops/pulumi/workspace",
            StackNaming.get_stack_name("workspace", env),
        )
        raise typer.Exit(1)


@app.command()
def restart(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Workspace configuration file (YAML)"
    ),
    infra_stack: str = typer.Option(
        StackNaming.get_project_name("infra"),
        "--infra-stack",
        help=f"Infrastructure stack name (default: {StackNaming.get_project_name('infra')}, auto-appends env for default)",
    ),
    env: str | None = env_option(),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts"),
):
    """Restart the Dask workspace (down then up).

    This is equivalent to running:
      mops workspace down --yes
      mops workspace up [--config CONFIG] [--infra-stack STACK]
    """
    from modelops.cli.display import info, success

    info(f"Restarting workspace in environment: {resolve_env(env)}")

    # First, bring down the workspace
    down(env=env, yes=yes)

    # Then bring it back up with all the same parameters
    up(config=config, env=env, infra_stack=infra_stack)

    success("✓ Workspace restarted successfully")


@app.command()
def status(
    env: str | None = env_option(),
    smoke_test: bool = typer.Option(
        False, "--smoke-test", help="Run smoke tests to validate connectivity"
    ),
):
    """Show workspace status and connection details."""
    env = resolve_env(env)

    # Use WorkspaceService
    service = WorkspaceService(env)

    try:
        status = service.status()

        if not status.deployed:
            warning("Workspace not deployed")
            info("Run 'mops workspace up' to deploy a workspace")
            raise typer.Exit(0)

        outputs = service.get_outputs()

        if not outputs:
            warning("Workspace stack exists but has no outputs")
            info("The workspace may not be fully deployed.")
            raise typer.Exit(0)

        section("Workspace Status")
        workspace_info(outputs, env, StackNaming.get_stack_name("workspace", env))

        namespace = get_output_value(outputs, "namespace", StackNaming.get_namespace("dask", env))

        # Show smoke test status if available
        if outputs.get("smoke_test_job"):
            job_name = get_output_value(outputs, "smoke_test_job", "")
            info(f"\nSmoke test: Job '{job_name}' deployed")

        # Run smoke tests if requested
        if smoke_test:
            warning(
                "\n⚠ Smoke test flag is deprecated. Use 'mops status --smoke-test' for connectivity tests."
            )

        workspace_commands(namespace)

    except Exception as e:
        error(f"Error querying workspace status: {e}")
        handle_pulumi_error(
            e,
            "~/.modelops/pulumi/workspace",
            StackNaming.get_stack_name("workspace", env),
        )
        raise typer.Exit(1)


@app.command()
def autoscaling(
    env: str | None = env_option(),
    watch: bool = typer.Option(
        False, "--watch", "-w", help="Watch autoscaling status (updates every 5s)"
    ),
):
    """Show autoscaling status and current metrics."""
    import json
    import subprocess
    import time

    env = resolve_env(env)
    namespace = StackNaming.get_namespace("dask", env)

    # Check if workspace is deployed
    service = WorkspaceService(env)
    if not service.status().deployed:
        warning("Workspace not deployed")
        raise typer.Exit(1)

    def get_hpa_status():
        """Get HPA status from kubectl."""
        try:
            # Get HPA in JSON format
            result = subprocess.run(
                ["kubectl", "get", "hpa", "-n", namespace, "-o", "json"],
                capture_output=True,
                text=True,
                check=True,
            )
            hpa_list = json.loads(result.stdout)

            if not hpa_list.get("items"):
                return None

            # Find the dask-workers HPA
            for hpa in hpa_list["items"]:
                if "dask-workers" in hpa["metadata"]["name"]:
                    return hpa
            return None

        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return None

    def display_status():
        """Display HPA status."""
        console.clear()
        section("Autoscaling Status")

        hpa = get_hpa_status()
        if not hpa:
            warning("No HorizontalPodAutoscaler found")
            info("\nAutoscaling may be disabled in workspace configuration")
            return False

        spec = hpa.get("spec", {})
        status = hpa.get("status", {})

        # Basic info
        info_dict(
            {
                "Namespace": namespace,
                "Min replicas": spec.get("minReplicas", "N/A"),
                "Max replicas": spec.get("maxReplicas", "N/A"),
                "Current replicas": status.get("currentReplicas", "N/A"),
                "Desired replicas": status.get("desiredReplicas", "N/A"),
            }
        )

        # Metrics
        section("Current Metrics")
        current_metrics = status.get("currentMetrics", [])
        if current_metrics:
            for metric in current_metrics:
                if metric["type"] == "Resource" and "resource" in metric:
                    resource = metric["resource"]
                    name = resource.get("name", "unknown")
                    current = resource.get("current", {})

                    if "averageUtilization" in current:
                        info(f"  {name.upper()}: {current['averageUtilization']}%")
                    elif "averageValue" in current:
                        info(f"  {name.upper()}: {current['averageValue']}")
        else:
            info("  No metrics available yet")

        # Conditions
        conditions = status.get("conditions", [])
        scaling_active = False
        for condition in conditions:
            if condition.get("type") == "ScalingActive":
                scaling_active = condition.get("status") == "True"
                if not scaling_active:
                    warning(f"\n⚠ Scaling inactive: {condition.get('message', 'Unknown reason')}")

        if scaling_active:
            success("\n✓ Autoscaling is active")

        return True

    if watch:
        info("Watching HPA status (press Ctrl+C to stop)...")
        try:
            while True:
                if display_status():
                    info(f"\n[dim]Updated: {time.strftime('%H:%M:%S')}[/dim]")
                time.sleep(5)
        except KeyboardInterrupt:
            info("\nStopped watching")
    else:
        display_status()
        info("\nUse --watch to monitor autoscaling in real-time")


@app.command(name="list")
def list_workspaces():
    """List all workspaces across environments using Pulumi Automation API."""
    import pulumi.automation as auto

    from ..core.automation import workspace_options
    from ..core.paths import BACKEND_DIR, ensure_work_dir

    project_name = StackNaming.get_project_name("workspace")
    work_dir = ensure_work_dir("workspace")

    # Check if any workspaces exist
    if not BACKEND_DIR.exists():
        warning("No workspaces found")
        info("\nRun 'mops workspace up' to create a workspace")
        return

    try:
        # Create a LocalWorkspace bound to the workspace project + backend
        ws = auto.LocalWorkspace(**workspace_options(project_name, work_dir).__dict__)

        # List stacks registered for this project in this backend
        stacks = ws.list_stacks()  # -> List[StackSummary]
        if not stacks:
            warning("No workspaces found")
            return

        section("Available Workspaces")

        # Sort for stable output
        for s in sorted(stacks, key=lambda ss: ss.name):
            stack_name = s.name
            # env from the standardized stack name
            try:
                stack_env = StackNaming.parse_stack_name(stack_name)["env"]
            except Exception:
                stack_env = stack_name  # fallback: show the raw name

            status = "Unknown"
            try:
                # Select stack (no-op program) to read state safely
                from ..core.automation import noop_program as _noop

                st = auto.select_stack(
                    stack_name=stack_name,
                    project_name=project_name,
                    program=_noop,
                    opts=workspace_options(project_name, work_dir),
                )

                # Fast state read: no refresh (avoid slowing down listing)
                # Export returns a Deployment object with a .deployment dict attribute
                state = st.export_stack()

                # The Deployment object has a .deployment attribute that is a dict
                if hasattr(state, "deployment") and isinstance(state.deployment, dict):
                    resources = state.deployment.get("resources", [])
                    # Consider it "deployed" if there are any real resources beyond the Stack resource itself
                    has_real = any(r.get("type") != "pulumi:pulumi:Stack" for r in resources)
                    status = "✓ Deployed" if has_real else "⚠ Not deployed"
                else:
                    # Fallback if structure is unexpected
                    status = "⚠ Unknown state"

            except Exception:
                # Keep Unknown status on error
                pass

            info(f"  • {stack_env}: {status}")

        info("\nUse 'mops workspace status --env <env>' for details")

    except Exception as e:
        error(f"Error listing workspaces: {e}")
        raise typer.Exit(1)


@app.command()
def update(
    env: str | None = env_option(),
    # Resource overrides
    scheduler_memory: str | None = typer.Option(
        None, "--scheduler-memory", help="Scheduler memory (e.g., '2Gi')"
    ),
    scheduler_cpu: str | None = typer.Option(
        None, "--scheduler-cpu", help="Scheduler CPU (e.g., '1' or '500m')"
    ),
    worker_memory: str | None = typer.Option(
        None, "--worker-memory", help="Worker memory (e.g., '8Gi')"
    ),
    worker_cpu: str | None = typer.Option(None, "--worker-cpu", help="Worker CPU (e.g., '3.5')"),
    worker_replicas: int | None = typer.Option(
        None,
        "--worker-replicas",
        help="Fixed number of worker replicas (requires --disable-autoscaling)",
    ),
    worker_processes: int | None = typer.Option(
        None, "--worker-processes", help="Number of processes per worker pod"
    ),
    worker_threads: int | None = typer.Option(
        None, "--worker-threads", help="Number of threads per worker process"
    ),
    # Autoscaling overrides
    enable_autoscaling: bool = typer.Option(
        False, "--enable-autoscaling", help="Enable HorizontalPodAutoscaler for workers"
    ),
    disable_autoscaling: bool = typer.Option(
        False, "--disable-autoscaling", help="Disable autoscaling and use fixed replicas"
    ),
    min_workers: int | None = typer.Option(
        None, "--min-workers", help="Minimum workers for autoscaling"
    ),
    max_workers: int | None = typer.Option(
        None, "--max-workers", help="Maximum workers for autoscaling"
    ),
    target_cpu_percent: int | None = typer.Option(
        None, "--target-cpu", help="Target CPU utilization percentage for autoscaling"
    ),
    # Control flags
    yes: bool = yes_option(),
):
    """Update workspace resources with zero downtime.

    This command performs rolling updates to workspace resources without destroying
    the workspace. Most changes (worker resources, autoscaling) can be applied with
    zero downtime using Kubernetes rolling updates.

    Examples:
        # Increase worker resources
        mops workspace update --worker-cpu 4 --worker-memory 16Gi

        # Scale to fixed replicas
        mops workspace update --disable-autoscaling --worker-replicas 10

        # Enable autoscaling with new limits
        mops workspace update --enable-autoscaling --min-workers 5 --max-workers 20

        # Update scheduler resources (brief downtime)
        mops workspace update --scheduler-cpu 2 --scheduler-memory 4Gi

    Note:
        - Worker updates use RollingUpdate strategy (zero downtime)
        - Scheduler updates use Recreate strategy (brief downtime to prevent split-brain)
        - Changes are validated before applying to prevent errors
    """
    from ..core.unified_config import UnifiedModelOpsConfig
    from .validators import validate_all_workspace_params
    from .workspace_overrides import (
        apply_overrides,
        build_cli_overrides,
        compute_changes,
        requires_replacement,
        show_config_diff,
    )

    env = resolve_env(env)

    # Validate all parameters
    try:
        validate_all_workspace_params(
            scheduler_memory=scheduler_memory,
            scheduler_cpu=scheduler_cpu,
            worker_memory=worker_memory,
            worker_cpu=worker_cpu,
            worker_replicas=worker_replicas,
            worker_processes=worker_processes,
            worker_threads=worker_threads,
            enable_autoscaling=enable_autoscaling if enable_autoscaling else None,
            disable_autoscaling=disable_autoscaling if disable_autoscaling else None,
            min_workers=min_workers,
            max_workers=max_workers,
            target_cpu_percent=target_cpu_percent,
        )
    except typer.BadParameter as e:
        error(f"Validation error: {e}")
        raise typer.Exit(1)

    # Load current unified config
    if not UNIFIED_CONFIG_FILE.exists():
        error(f"Configuration file not found: {UNIFIED_CONFIG_FILE}")
        info("Run 'mops init' to generate modelops.yaml")
        raise typer.Exit(1)

    try:
        unified_config = UnifiedModelOpsConfig.from_yaml(UNIFIED_CONFIG_FILE)
        current_workspace = unified_config.workspace
    except Exception as e:
        error(f"Failed to load config: {e}")
        raise typer.Exit(1)

    # Build overrides from CLI params
    overrides = build_cli_overrides(
        scheduler_memory=scheduler_memory,
        scheduler_cpu=scheduler_cpu,
        worker_memory=worker_memory,
        worker_cpu=worker_cpu,
        worker_replicas=worker_replicas,
        worker_processes=worker_processes,
        worker_threads=worker_threads,
        autoscaling_enabled=(
            False if disable_autoscaling else (True if enable_autoscaling else None)
        ),
        autoscaling_min_workers=min_workers,
        autoscaling_max_workers=max_workers,
        autoscaling_target_cpu=target_cpu_percent,
    )

    # Check if any changes were requested
    if not overrides:
        warning("No changes specified")
        info("Use --help to see available options")
        raise typer.Exit(0)

    # Apply overrides and compute changes
    updated_workspace = apply_overrides(current_workspace, overrides)
    changes = compute_changes(current_workspace, updated_workspace)

    if not changes:
        success("Configuration is already up to date")
        raise typer.Exit(0)

    # Show changes
    section(f"Workspace Update Plan (Environment: {env})")
    show_config_diff(changes, console)

    # Check if replacement needed
    if requires_replacement(changes):
        warning("\n⚠️  These changes require full workspace restart")
        info("This will cause temporary downtime while resources are recreated")

    # Confirm changes
    if not yes:
        confirm = typer.confirm("\nApply these changes?")
        if not confirm:
            info("Update cancelled")
            raise typer.Exit(0)

    # Convert updated workspace spec to WorkspaceConfig
    validated_config = WorkspaceConfig(
        apiVersion="modelops/v1",
        kind="Workspace",
        metadata={"name": "default-workspace"},
        spec={
            "scheduler": {
                "image": updated_workspace.scheduler_image,
                "resources": {
                    "requests": {
                        "memory": updated_workspace.scheduler_memory,
                        "cpu": updated_workspace.scheduler_cpu,
                    },
                    "limits": {
                        "memory": updated_workspace.scheduler_memory,
                        "cpu": updated_workspace.scheduler_cpu,
                    },
                },
            },
            "workers": {
                "replicas": updated_workspace.worker_replicas,
                "image": updated_workspace.worker_image,
                "resources": {
                    "requests": {
                        "memory": updated_workspace.worker_memory,
                        "cpu": updated_workspace.worker_cpu,
                    },
                    "limits": {
                        "memory": updated_workspace.worker_memory,
                        "cpu": updated_workspace.worker_cpu,
                    },
                },
                "processes": updated_workspace.worker_processes,
                "threads": updated_workspace.worker_threads,
            },
            "autoscaling": {
                "enabled": updated_workspace.autoscaling_enabled,
                "min_workers": updated_workspace.autoscaling_min_workers,
                "max_workers": updated_workspace.autoscaling_max_workers,
                "target_cpu": updated_workspace.autoscaling_target_cpu,
            },
        },
    )

    # Apply update via WorkspaceService
    service = WorkspaceService(env)

    try:
        info("\n[yellow]Applying workspace update...[/yellow]")
        outputs = service.provision(config=validated_config, infra_stack_ref=None, verbose=False)

        success("\n✓ Workspace updated successfully!")

        # Show updated status
        info("\nWorkspace Status:")
        workspace_info(outputs, env, StackNaming.get_stack_name("workspace", env))

    except Exception as e:
        error(f"\nError updating workspace: {e}")
        handle_pulumi_error(
            e,
            "~/.modelops/pulumi/workspace",
            StackNaming.get_stack_name("workspace", env),
        )
        raise typer.Exit(1)


@app.command()
def scale(
    min_workers: int | None = typer.Option(
        None, "--min-workers", "-n", help="Minimum workers for autoscaling"
    ),
    max_workers: int | None = typer.Option(
        None, "--max-workers", "-x", help="Maximum workers for autoscaling"
    ),
    replicas: int | None = typer.Option(
        None,
        "--replicas",
        "-r",
        help="Fixed number of replicas (disables autoscaling)",
    ),
    env: str | None = env_option(),
    yes: bool = yes_option(),
):
    """Quick scaling adjustment for workers.

    This is a convenience command that wraps 'mops workspace update' for
    common scaling operations.

    Examples:
        # Adjust autoscaling range
        mops workspace scale --min-workers 5 --max-workers 20

        # Set fixed replicas
        mops workspace scale --replicas 10

    Note: For more control, use 'mops workspace update' instead.
    """
    env = resolve_env(env)

    # Validate that user provided some scaling parameter
    if min_workers is None and max_workers is None and replicas is None:
        error("No scaling parameters specified")
        info("Use --min-workers, --max-workers, or --replicas")
        info("Example: mops workspace scale --min-workers 5 --max-workers 20")
        raise typer.Exit(1)

    # Determine which update parameters to use
    update_params = {"env": env, "yes": yes}

    if replicas is not None:
        # Fixed replicas - disable autoscaling
        update_params["disable_autoscaling"] = True
        update_params["worker_replicas"] = replicas
    else:
        # Autoscaling parameters
        if min_workers is not None:
            update_params["min_workers"] = min_workers
        if max_workers is not None:
            update_params["max_workers"] = max_workers

    # Delegate to update command
    info(f"Scaling workspace in environment: {env}")
    update(**update_params)


@app.command()
def port_forward(
    env: str | None = env_option(),
    target: str = typer.Option(
        "dashboard",
        "--target",
        "-t",
        help="What to forward: dashboard (default), scheduler, or custom service",
    ),
    local_port: int | None = typer.Option(
        None,
        "--local-port",
        "-l",
        help="Local port to forward to (auto-assigned if not specified)",
    ),
    remote_port: int | None = typer.Option(
        None,
        "--remote-port",
        "-r",
        help="Remote port on the service (derived from target if not specified)",
    ),
    service_name: str | None = typer.Option(
        None,
        "--service",
        "-s",
        help="Override service name (uses workspace outputs if not specified)",
    ),
):
    """Port forward to Dask services using Python Kubernetes client.

    This creates a local port forward to Dask services based on workspace outputs,
    avoiding hardcoded values. The service details are read from Pulumi stack outputs.

    Examples:
        # Forward Dask dashboard (default)
        mops workspace port-forward

        # Forward scheduler port instead
        mops workspace port-forward --target scheduler

        # Custom local port
        mops workspace port-forward --local-port 9999

        # Override for custom service
        mops workspace port-forward --service my-service --remote-port 8080
    """
    env = resolve_env(env)

    try:
        # Get workspace outputs to determine namespace and kubeconfig
        outputs = automation.outputs("workspace", env, refresh=False)

        if not outputs:
            error("Workspace not deployed")
            info("Run 'mops workspace up' first")
            raise typer.Exit(1)

        namespace = automation.get_output_value(outputs, "namespace")
        if not namespace:
            error("Could not determine workspace namespace")
            raise typer.Exit(1)

        # Get service details from workspace outputs (single source of truth)
        # Future enhancement: Could also discover services dynamically via K8s API
        # by querying for services with label selector "modelops.io/component"

        # Determine service name and port based on target
        if not service_name:
            # Read service name from outputs
            service_name = automation.get_output_value(
                outputs, "scheduler_service_name", "dask-scheduler"
            )

        if not remote_port:
            # Determine port based on target
            if target == "dashboard":
                remote_port = automation.get_output_value(outputs, "dashboard_port", 8787)
            elif target == "scheduler":
                remote_port = automation.get_output_value(outputs, "scheduler_port", 8786)
            else:
                error(
                    f"Unknown target '{target}'. Use 'dashboard', 'scheduler', or specify --service and --remote-port"
                )
                raise typer.Exit(1)

        # Auto-assign local port if not specified
        if not local_port:
            local_port = remote_port  # Mirror remote port locally

        # Get kubeconfig from infrastructure stack
        infra_outputs = automation.outputs("infra", env, refresh=False)
        if not infra_outputs:
            error("Infrastructure not deployed")
            info("Run 'mops infra up' first")
            raise typer.Exit(1)

        kubeconfig = automation.get_output_value(infra_outputs, "kubeconfig")
        if not kubeconfig:
            error("Could not get kubeconfig from infrastructure")
            raise typer.Exit(1)

        # Import kubernetes client
        try:
            from kubernetes import client, config
        except ImportError:
            error("kubernetes Python client not installed")
            info("Install with: pip install kubernetes")
            raise typer.Exit(1)

        # Create temporary kubeconfig file
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(kubeconfig)
            temp_kubeconfig = f.name

        try:
            # Load kubernetes config from the temporary file
            config.load_kube_config(config_file=temp_kubeconfig)

            # Create API client
            v1 = client.CoreV1Api()

            # Find the service to verify it exists
            try:
                service = v1.read_namespaced_service(name=service_name, namespace=namespace)
                info(f"Found service '{service_name}' in namespace '{namespace}'")
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    error(f"Service '{service_name}' not found in namespace '{namespace}'")
                    info("\nAvailable services:")
                    services = v1.list_namespaced_service(namespace=namespace)
                    for svc in services.items:
                        info(f"  • {svc.metadata.name}")
                else:
                    error(f"Error accessing service: {e}")
                raise typer.Exit(1)

            # Get pod selector from service
            selector = service.spec.selector
            if not selector:
                error(f"Service '{service_name}' has no pod selector")
                raise typer.Exit(1)

            # Find pods matching the service selector
            label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
            pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)

            if not pods.items:
                error(f"No pods found for service '{service_name}'")
                raise typer.Exit(1)

            # Use the first ready pod
            target_pod = None
            for pod in pods.items:
                if pod.status.phase == "Running":
                    # Check if all containers are ready
                    all_ready = all(c.ready for c in (pod.status.container_statuses or []))
                    if all_ready:
                        target_pod = pod
                        break

            if not target_pod:
                error("No ready pods found for service")
                info("Pod statuses:")
                for pod in pods.items:
                    info(f"  • {pod.metadata.name}: {pod.status.phase}")
                raise typer.Exit(1)

            section(f"Port forwarding to {target}")
            info_dict(
                {
                    "Target": target,
                    "Namespace": namespace,
                    "Service": service_name,
                    "Pod": target_pod.metadata.name,
                    "Local": f"localhost:{local_port}",
                    "Remote": f"{remote_port}",
                }
            )

            # Check if local port is already in use for idempotency
            import socket

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("localhost", local_port))
                sock.close()
            except OSError:
                warning(f"Port {local_port} is already in use")
                info("The port forward may already be active or another process is using this port")
                raise typer.Exit(0)

            # Use kubectl subprocess for reliable cross-platform port forwarding
            # This works on Windows, macOS, and Linux as long as kubectl is installed
            import signal
            import subprocess
            import sys

            # Build kubectl command
            cmd = [
                "kubectl",
                "port-forward",
                f"pod/{target_pod.metadata.name}",
                f"{local_port}:{remote_port}",
                "-n",
                namespace,
                "--kubeconfig",
                temp_kubeconfig,
            ]

            # Start port-forward process
            info("\nStarting port forward...")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1,
            )

            # Give it a moment to establish
            import time

            time.sleep(1)

            # Check if process is still running
            if proc.poll() is not None:
                stderr_output = proc.stderr.read()
                error(f"Port forward failed to start: {stderr_output}")
                raise typer.Exit(1)

            success("\nPort forward established!")
            if target == "dashboard":
                info(f"  Dask dashboard: http://localhost:{local_port}")
            elif target == "scheduler":
                info(f"  Dask scheduler: tcp://localhost:{local_port}")
            else:
                info(f"  Service endpoint: localhost:{local_port}")
            info("\nPress Ctrl+C to stop port forwarding...")

            # Cross-platform signal handling
            def signal_handler(sig, frame):
                info("\n\nStopping port forward...")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()  # Force kill if needed
                success("Port forward stopped")
                sys.exit(0)

            # Register signal handlers (cross-platform compatible)
            if hasattr(signal, "SIGINT"):
                signal.signal(signal.SIGINT, signal_handler)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, signal_handler)

            # Wait for process to complete or be interrupted
            try:
                proc.wait()
            except KeyboardInterrupt:
                signal_handler(signal.SIGINT, None)

        finally:
            # Clean up temporary kubeconfig file
            os.unlink(temp_kubeconfig)

    except KeyboardInterrupt:
        success("\nPort forward stopped")
    except Exception as e:
        error(f"Error setting up port forward: {e}")
        raise typer.Exit(1)
