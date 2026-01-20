"""Adaptive plane CLI commands for optimization runs."""

from pathlib import Path

import typer
import yaml
from rich.table import Table

from ..core import StackNaming, automation
from ..core.k8s import dns_1123_label
from .common_options import env_option, yes_option
from .display import commands, console, dim, error, info, section, success, warning
from .utils import handle_pulumi_error, resolve_env

app = typer.Typer(help="Manage adaptive infrastructure for optimization algorithms")


@app.command()
def up(
    config: Path = typer.Argument(
        ...,
        help="Adaptive run configuration file (YAML)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    name: str = typer.Option(
        "default",
        "--name",
        "-n",
        help="Name for this adaptive infrastructure (default: 'default')",
    ),
    infra_stack: str = typer.Option(
        StackNaming.get_project_name("infra"),
        "--infra-stack",
        help=f"Infrastructure stack name (default: {StackNaming.get_project_name('infra')}, auto-appends env for default)",
    ),
    workspace_stack: str = typer.Option(
        StackNaming.get_project_name("workspace"),
        "--workspace-stack",
        help=f"Workspace stack name (default: {StackNaming.get_project_name('workspace')}, auto-appends env for default)",
    ),
    env: str | None = env_option(),
):
    """Provision adaptive infrastructure components.

    Creates stateful components (Postgres, Redis, etc.) needed by
    optimization algorithms. Multiple named component sets can exist
    per environment.

    Example:
        mops adaptive up examples/adaptive.yaml
        mops adaptive up examples/adaptive.yaml --name mlflow --env prod
    """
    env = resolve_env(env)

    # Sanitize name for Kubernetes DNS-1123 compliance
    sanitized_name = dns_1123_label(name, fallback="default")
    if sanitized_name != name:
        warning(f"Name '{name}' was sanitized to '{sanitized_name}' for Kubernetes compatibility")
        name = sanitized_name

    # Load configuration
    with open(config) as f:
        run_config = yaml.safe_load(f)

    def pulumi_program():
        """Create AdaptiveInfra in Stack 4 context."""
        import pulumi

        from ..infra.components.adaptive import AdaptiveInfra

        # Use provided stack names or default to standard naming
        if infra_stack == StackNaming.get_project_name("infra"):
            # Default value - append environment
            infra_ref = StackNaming.ref("infra", env)
        else:
            # Custom stack name provided
            infra_ref = StackNaming.ref_from_stack(infra_stack)

        if workspace_stack == StackNaming.get_project_name("workspace"):
            # Default value - append environment
            workspace_ref = StackNaming.ref("workspace", env)
        else:
            # Custom stack name provided
            workspace_ref = StackNaming.ref_from_stack(workspace_stack)

        # Check if storage stack exists and reference it
        storage_ref = None
        try:
            # Try to reference storage stack if it exists
            storage_ref = StackNaming.ref("storage", env)
        except (ValueError, FileNotFoundError, AttributeError):
            # Storage stack doesn't exist, adaptive will run without storage integration
            pass

        adaptive = AdaptiveInfra(
            name,
            infra_stack_ref=infra_ref,
            workspace_stack_ref=workspace_ref,
            config=run_config,
            storage_stack_ref=storage_ref,
        )

        # Export outputs at stack level for visibility (like workspace.py does)
        pulumi.export("name", adaptive.name)
        pulumi.export("namespace", adaptive.namespace)
        pulumi.export("scheduler_address", adaptive.scheduler_address)
        pulumi.export("postgres_dsn", adaptive.postgres_dsn)
        pulumi.export("workers_name", adaptive.workers_name)
        pulumi.export("worker_replicas", run_config.get("workers", {}).get("replicas", 2))
        pulumi.export("algorithm", run_config.get("algorithm", "optuna"))

        return adaptive

    # Ensure work directory exists for this adaptive infrastructure
    from ..core.paths import ensure_work_dir

    base_dir = ensure_work_dir("adaptive")
    work_dir = base_dir / name
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        section(f"Provisioning adaptive infrastructure: {name}")
        info(f"  Environment: {env}")
        info(f"  Central store: {run_config.get('central_store', {}).get('kind', 'none')}")
        if run_config.get("algorithm"):
            info(f"  Algorithm support: {run_config.get('algorithm')}")
        if run_config.get("workers"):
            info(f"  Worker replicas: {run_config.get('workers', {}).get('replicas', 1)}\n")

        warning("\nProvisioning infrastructure components...")

        # Use automation helper with custom work_dir for named infrastructure
        outputs = automation.up(
            "adaptive", env, name, pulumi_program, on_output=dim, work_dir=str(work_dir)
        )

        success("\n✓ Adaptive infrastructure provisioned!")
        info(f"  Name: {name}")
        info(f"  Namespace: {automation.get_output_value(outputs, 'namespace', 'unknown')}")
        info(f"  Workers: {automation.get_output_value(outputs, 'workers_name', 'unknown')}")
        info(f"  Replicas: {automation.get_output_value(outputs, 'worker_replicas', '2')}")

        if outputs.get("postgres_dsn"):
            success("  Database: ✓ Postgres provisioned")

        namespace = automation.get_output_value(
            outputs, "namespace", f"modelops-adaptive-{env}-{name}"
        )
        workers_name = automation.get_output_value(outputs, "workers_name", "adaptive-workers")

        section("\nMonitor progress:")
        commands(
            [
                ("Workers", f"kubectl get deployment -n {namespace} {workers_name}"),
                ("Pods", f"kubectl get pods -n {namespace} -l app=adaptive-worker"),
                (
                    "Logs",
                    f"kubectl logs -n {namespace} -l app=adaptive-worker --tail=50",
                ),
            ]
        )

    except Exception as e:
        error(f"\nError starting run: {e}")
        handle_pulumi_error(e, str(work_dir), StackNaming.get_stack_name("adaptive", env, name))
        raise typer.Exit(1)


@app.command()
def down(
    name: str = typer.Option(
        "default", "--name", "-n", help="Name of adaptive infrastructure to destroy"
    ),
    env: str | None = env_option(),
    yes: bool = yes_option(),
):
    """Destroy adaptive infrastructure components.

    This removes all resources associated with the named infrastructure
    including any databases, persistent volumes, and other stateful components.
    """
    env = resolve_env(env)

    # Sanitize name for consistency
    name = dns_1123_label(name, fallback="default")

    if not yes:
        warning("\n⚠️  Warning")
        info(f"This will destroy adaptive infrastructure: {name}")
        info("All data in databases and persistent volumes will be lost.")

        confirm = typer.confirm("\nAre you sure you want to destroy this infrastructure?")
        if not confirm:
            success("Destruction cancelled")
            raise typer.Exit(0)

    # Check if work directory exists
    from ..core.paths import ensure_work_dir

    base_dir = ensure_work_dir("adaptive")
    work_dir = base_dir / name

    if not work_dir.exists():
        warning(f"Adaptive infrastructure not found: {name}")
        raise typer.Exit(0)

    try:
        warning(f"\nDestroying infrastructure: {name}...")

        # Use automation helper with custom work_dir
        automation.destroy("adaptive", env, name, on_output=dim, work_dir=str(work_dir))

        # Clean up work directory
        import shutil

        shutil.rmtree(work_dir)

        success(f"\n✓ Adaptive infrastructure '{name}' destroyed successfully")

    except Exception as e:
        error(f"\nError destroying infrastructure: {e}")
        handle_pulumi_error(e, str(work_dir), StackNaming.get_stack_name("adaptive", env, name))
        raise typer.Exit(1)


@app.command()
def status(
    name: str = typer.Option("default", "--name", "-n", help="Name of adaptive infrastructure"),
    env: str | None = env_option(),
):
    """Show status of adaptive infrastructure."""
    env = resolve_env(env)

    # Sanitize name for consistency
    name = dns_1123_label(name, fallback="default")

    # Check if work directory exists
    from ..core.paths import ensure_work_dir

    base_dir = ensure_work_dir("adaptive")
    work_dir = base_dir / name

    if not work_dir.exists():
        warning(f"Adaptive infrastructure not found: {name}")
        raise typer.Exit(0)

    try:
        # Use automation helper to get outputs with custom work_dir
        outputs = automation.outputs("adaptive", env, name, refresh=True, work_dir=str(work_dir))

        if not outputs:
            warning("Infrastructure exists but has no outputs")
            info("The infrastructure may not be fully deployed.")
            raise typer.Exit(0)

        section("Adaptive Infrastructure Status")
        info(f"  Name: {name}")
        info(f"  Stack: {StackNaming.get_stack_name('adaptive', env, name)}")
        info(f"  Algorithm: {automation.get_output_value(outputs, 'algorithm', 'unknown')}")
        info(f"  Namespace: {automation.get_output_value(outputs, 'namespace', 'unknown')}")
        info(f"  Workers: {automation.get_output_value(outputs, 'workers_name', 'unknown')}")
        info(f"  Replicas: {automation.get_output_value(outputs, 'worker_replicas', 'unknown')}")

        if outputs.get("postgres_dsn"):
            success("  Database: ✓ Postgres connected")

        if outputs.get("scheduler_address"):
            info(f"  Scheduler: {automation.get_output_value(outputs, 'scheduler_address')}")

        namespace = automation.get_output_value(
            outputs, "namespace", f"modelops-adaptive-{env}-{name}"
        )
        workers_name = automation.get_output_value(outputs, "workers_name", "adaptive-workers")

        section("\nCommands:")
        commands(
            [
                (
                    "Deployment",
                    f"kubectl describe deployment -n {namespace} {workers_name}",
                ),
                (
                    "Pods",
                    f"kubectl get pods -n {namespace} -l app=adaptive-worker -o wide",
                ),
                (
                    "Logs",
                    f"kubectl logs -n {namespace} -l app=adaptive-worker --tail=50",
                ),
            ]
        )

    except Exception as e:
        error(f"Error querying infrastructure status: {e}")
        handle_pulumi_error(e, str(work_dir), StackNaming.get_stack_name("adaptive", env, name))
        raise typer.Exit(1)


@app.command(name="list")
def list_infra():
    """List all adaptive infrastructure deployments using Pulumi Automation API."""
    import pulumi.automation as auto

    from ..core.automation import workspace_options
    from ..core.paths import BACKEND_DIR, ensure_work_dir

    # Check if backend exists
    if not BACKEND_DIR.exists():
        warning("No adaptive infrastructure found")
        info("\nRun 'mops adaptive up <config>' to provision infrastructure")
        return

    project_name = StackNaming.get_project_name("adaptive")

    # Use the adaptive base directory for listing
    base_dir = ensure_work_dir("adaptive")

    try:
        # Create a LocalWorkspace bound to the adaptive project + backend
        # We use the base directory to list all stacks in this project
        ws = auto.LocalWorkspace(**workspace_options(project_name, base_dir).__dict__)

        # List stacks registered for this project in this backend
        stacks = ws.list_stacks()
        if not stacks:
            warning("No adaptive infrastructure found")
            return

        # Create table
        table = Table(title="Adaptive Infrastructure")
        table.add_column("Name", style="cyan")
        table.add_column("Environment", style="yellow")
        table.add_column("Status", style="green")
        table.add_column("Algorithm", style="dim")

        # Process each stack
        for s in sorted(stacks, key=lambda ss: ss.name):
            stack_name = s.name

            # Parse stack name to get env and name
            try:
                parsed = StackNaming.parse_stack_name(stack_name)
                stack_env = parsed["env"]
                infra_name = parsed.get("run_id", stack_name)  # run_id field contains the name
            except Exception:
                # Fallback if parsing fails
                stack_env = "unknown"
                infra_name = stack_name

            # Get status and algorithm from stack state
            status = "Unknown"
            algorithm = "-"

            # Check if infrastructure directory exists
            infra_dir = base_dir / infra_name
            if infra_dir.exists():
                try:
                    # Try to get outputs without refresh (fast)
                    outputs = automation.outputs(
                        "adaptive",
                        stack_env,
                        infra_name,
                        refresh=False,
                        work_dir=str(infra_dir),
                    )
                    if outputs:
                        algorithm = automation.get_output_value(outputs, "algorithm", "-")
                        worker_replicas = automation.get_output_value(
                            outputs, "worker_replicas", "0"
                        )
                        if worker_replicas and int(worker_replicas) > 0:
                            status = f"✓ Running ({worker_replicas} workers)"
                        else:
                            status = "✓ Deployed"
                    else:
                        status = "⚠ Not deployed"
                except Exception:
                    # Keep unknown status on error
                    pass

            table.add_row(infra_name, stack_env, status, algorithm)

        console.print(table)
        info("\nUse 'mops adaptive status --name <name>' for details")
        info("Use 'mops adaptive down --name <name>' to clean up")

    except Exception as e:
        error(f"Error listing infrastructure: {e}")
        raise typer.Exit(1)


@app.command()
def logs(
    name: str = typer.Option("default", "--name", "-n", help="Name of adaptive infrastructure"),
    env: str | None = env_option(),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    tail: int = typer.Option(100, "--tail", "-t", help="Number of lines to show from the end"),
):
    """Get logs from adaptive infrastructure.

    This is a convenience wrapper around kubectl logs for the running
    optimization job associated with the infrastructure.
    """
    env = resolve_env(env)

    # Sanitize name for consistency
    name = dns_1123_label(name, fallback="default")

    # Check if infrastructure directory exists
    from ..core.paths import ensure_work_dir

    base_dir = ensure_work_dir("adaptive")
    work_dir = base_dir / name

    if not work_dir.exists():
        warning(f"Adaptive infrastructure not found: {name}")
        raise typer.Exit(0)

    try:
        # Get namespace from stack outputs with custom work_dir
        outputs = automation.outputs("adaptive", env, name, refresh=False, work_dir=str(work_dir))

        if not outputs:
            warning(f"Infrastructure {name} has no outputs")
            raise typer.Exit(0)

        namespace = automation.get_output_value(
            outputs, "namespace", f"modelops-adaptive-{env}-{name}"
        )
        workers_name = automation.get_output_value(outputs, "workers_name", "adaptive-workers")

        # Build kubectl command
        import subprocess

        cmd = [
            "kubectl",
            "logs",
            "-n",
            namespace,
            "-l",
            "app=adaptive-worker",
            f"--tail={tail}",
        ]

        if follow:
            cmd.append("-f")

        info(f"Fetching logs from namespace: {namespace}\n")

        # Run kubectl
        subprocess.run(cmd)

    except Exception as e:
        error(f"Error getting logs: {e}")
        raise typer.Exit(1)
