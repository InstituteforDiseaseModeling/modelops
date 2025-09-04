"""Adaptive plane CLI commands for optimization runs."""

import typer
import yaml
from pathlib import Path
from typing import Optional
from datetime import datetime
from rich.table import Table
from ..core import StackNaming, automation
from ..core.k8s import dns_1123_label
from .utils import handle_pulumi_error, resolve_env
from .display import console, success, warning, error, info, section, dim, commands
from .common_options import env_option, yes_option, run_id_option

app = typer.Typer(help="Manage adaptive optimization runs")


@app.command()
def up(
    config: Path = typer.Argument(
        ...,
        help="Adaptive run configuration file (YAML)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True
    ),
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id", "-r",
        help="Unique run identifier (auto-generated if not provided)"
    ),
    infra_stack: str = typer.Option(
        StackNaming.get_project_name("infra"),
        "--infra-stack",
        help=f"Infrastructure stack name (default: {StackNaming.get_project_name('infra')}, auto-appends env for default)"
    ),
    workspace_stack: str = typer.Option(
        StackNaming.get_project_name("workspace"),
        "--workspace-stack",
        help=f"Workspace stack name (default: {StackNaming.get_project_name('workspace')}, auto-appends env for default)"
    ),
    env: Optional[str] = env_option()
):
    """Start an adaptive optimization run.
    
    This creates Stack 3 which references both Stack 1 (infrastructure) and
    Stack 2 (workspace) to deploy adaptive workers that connect to Dask.
    
    Example:
        mops adaptive up optuna-config.yaml
        mops adaptive up experiment.yaml --run-id exp-001 --env prod
    """
    env = resolve_env(env)
    
    # Generate run ID if not provided and sanitize for DNS-1123 compliance
    if not run_id:
        run_id = f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    # Sanitize run_id for Kubernetes DNS-1123 compliance
    sanitized_run_id = dns_1123_label(run_id, fallback="run")
    if sanitized_run_id != run_id:
        warning(f"Run ID '{run_id}' was sanitized to '{sanitized_run_id}' for Kubernetes compatibility")
        run_id = sanitized_run_id
    
    # Load configuration
    with open(config) as f:
        run_config = yaml.safe_load(f)
    
    def pulumi_program():
        """Create AdaptiveRun in Stack 3 context."""
        from ..infra.components.adaptive import AdaptiveRun
        
        # Always use StackNaming.ref for consistency
        infra_ref = StackNaming.ref("infra", env)
        workspace_ref = StackNaming.ref("workspace", env)
        
        return AdaptiveRun(
            run_id,
            infra_stack_ref=infra_ref,
            workspace_stack_ref=workspace_ref,
            config=run_config
        )
    
    # Ensure run directory exists for this specific run
    from ..core.paths import ensure_work_dir
    base_dir = ensure_work_dir("adaptive")
    work_dir = base_dir / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        section(f"Starting adaptive run: {run_id}")
        info(f"  Environment: {env}")
        info(f"  Algorithm: {run_config.get('algorithm', 'optuna')}")
        info(f"  Trials: {run_config.get('n_trials', 100)}")
        info(f"  Parallel workers: {run_config.get('n_parallel', 10)}\n")
        
        warning("\nCreating adaptive resources...")
        
        # Use automation helper with custom work_dir for run-specific directory
        outputs = automation.up("adaptive", env, run_id, pulumi_program, on_output=dim, work_dir=str(work_dir))
        
        success("\n✓ Adaptive run started!")
        info(f"  Run ID: {run_id}")
        info(f"  Namespace: {automation.get_output_value(outputs, 'namespace', 'unknown')}")
        info(f"  Job: {automation.get_output_value(outputs, 'job_name', 'unknown')}")
        
        if outputs.get('postgres_dsn'):
            success("  Database: ✓ Postgres provisioned")
        
        namespace = automation.get_output_value(outputs, 'namespace', f'adaptive-{run_id}')
        job_name = automation.get_output_value(outputs, 'job_name', f'adaptive-{run_id}')
        
        section("\nMonitor progress:")
        commands([
            ("Logs", f"kubectl logs -n {namespace} -l job-name={job_name}"),
            ("Status", f"kubectl get job -n {namespace} {job_name}"),
            ("Pods", f"kubectl get pods -n {namespace} -l job-name={job_name}")
        ])
        
    except Exception as e:
        error(f"\nError starting run: {e}")
        handle_pulumi_error(e, str(work_dir), StackNaming.get_stack_name('adaptive', env, run_id))
        raise typer.Exit(1)


@app.command()
def down(
    run_id: str = run_id_option(),
    env: Optional[str] = env_option(),
    yes: bool = yes_option()
):
    """Destroy an adaptive run and clean up resources.
    
    This removes all resources associated with the run including
    any Postgres database and persistent volumes.
    """
    env = resolve_env(env)
    
    # Sanitize run_id for consistency
    run_id = dns_1123_label(run_id, fallback="run")
    
    if not yes:
        warning("\n⚠️  Warning")
        info(f"This will destroy adaptive run: {run_id}")
        info("All data associated with this run will be lost.")
        
        confirm = typer.confirm("\nAre you sure you want to destroy this run?")
        if not confirm:
            success("Destruction cancelled")
            raise typer.Exit(0)
    
    # Check if run directory exists
    from ..core.paths import ensure_work_dir
    base_dir = ensure_work_dir("adaptive")
    work_dir = base_dir / run_id
    
    if not work_dir.exists():
        warning(f"Run not found: {run_id}")
        raise typer.Exit(0)
    
    try:
        warning(f"\nDestroying run: {run_id}...")
        
        # Use automation helper with custom work_dir
        automation.destroy("adaptive", env, run_id, on_output=dim, work_dir=str(work_dir))
        
        # Clean up work directory
        import shutil
        shutil.rmtree(work_dir)
        
        success(f"\n✓ Run {run_id} destroyed successfully")
        
    except Exception as e:
        error(f"\nError destroying run: {e}")
        handle_pulumi_error(e, str(work_dir), StackNaming.get_stack_name('adaptive', env, run_id))
        raise typer.Exit(1)


@app.command()
def status(
    run_id: str = run_id_option(),
    env: Optional[str] = env_option()
):
    """Check status of an adaptive run."""
    env = resolve_env(env)
    
    # Sanitize run_id for consistency
    run_id = dns_1123_label(run_id, fallback="run")
    
    # Check if run directory exists
    from ..core.paths import ensure_work_dir
    base_dir = ensure_work_dir("adaptive")
    work_dir = base_dir / run_id
    
    if not work_dir.exists():
        warning(f"Run not found: {run_id}")
        raise typer.Exit(0)
    
    try:
        # Use automation helper to get outputs with custom work_dir
        outputs = automation.outputs("adaptive", env, run_id, refresh=True, work_dir=str(work_dir))
        
        if not outputs:
            warning("Run exists but has no outputs")
            info("The run may not be fully deployed.")
            raise typer.Exit(0)
        
        section("Run Status")
        info(f"  Run ID: {run_id}")
        info(f"  Stack: {StackNaming.get_stack_name('adaptive', env, run_id)}")
        info(f"  Algorithm: {automation.get_output_value(outputs, 'algorithm', 'unknown')}")
        info(f"  Trials: {automation.get_output_value(outputs, 'n_trials', 'unknown')}")
        info(f"  Namespace: {automation.get_output_value(outputs, 'namespace', 'unknown')}")
        info(f"  Job: {automation.get_output_value(outputs, 'job_name', 'unknown')}")
        info(f"  Status: {automation.get_output_value(outputs, 'status', 'unknown')}")
        
        if outputs.get('postgres_dsn'):
            success("  Database: ✓ Postgres connected")
        
        if outputs.get('scheduler_address'):
            info(f"  Scheduler: {automation.get_output_value(outputs, 'scheduler_address')}")
        
        namespace = automation.get_output_value(outputs, 'namespace', f'adaptive-{run_id}')
        job_name = automation.get_output_value(outputs, 'job_name', f'adaptive-{run_id}')
        
        section("\nCommands:")
        commands([
            ("Logs", f"kubectl logs -n {namespace} -l job-name={job_name}"),
            ("Job status", f"kubectl describe job -n {namespace} {job_name}"),
            ("Pod details", f"kubectl get pods -n {namespace} -l job-name={job_name} -o wide")
        ])
        
    except Exception as e:
        error(f"Error querying run status: {e}")
        handle_pulumi_error(e, str(work_dir), StackNaming.get_stack_name('adaptive', env, run_id))
        raise typer.Exit(1)


@app.command(name="list")
def list_runs():
    """List all adaptive runs using Pulumi Automation API."""
    import pulumi.automation as auto
    from ..core.paths import ensure_work_dir, get_backend_url, BACKEND_DIR
    from ..core.automation import workspace_options
    
    # Check if backend exists
    if not BACKEND_DIR.exists():
        warning("No adaptive runs found")
        info("\nRun 'mops adaptive up <config>' to start a run")
        return
    
    project_name = StackNaming.get_project_name("adaptive")
    
    # Use the adaptive base directory for listing
    base_dir = ensure_work_dir("adaptive")
    
    try:
        # Create a LocalWorkspace bound to the adaptive project + backend
        # We use the base directory to list all stacks in this project
        ws = auto.LocalWorkspace(
            **workspace_options(project_name, base_dir).__dict__
        )
        
        # List stacks registered for this project in this backend
        stacks = ws.list_stacks()
        if not stacks:
            warning("No adaptive runs found")
            return
        
        # Create table
        table = Table(title="Adaptive Runs")
        table.add_column("Run ID", style="cyan")
        table.add_column("Environment", style="yellow") 
        table.add_column("Status", style="green")
        table.add_column("Algorithm", style="dim")
        
        # Process each stack
        for s in sorted(stacks, key=lambda ss: ss.name):
            stack_name = s.name
            
            # Parse stack name to get env and run_id
            try:
                parsed = StackNaming.parse_stack_name(stack_name)
                stack_env = parsed["env"]
                run_id = parsed.get("run_id", stack_name)
            except Exception:
                # Fallback if parsing fails
                stack_env = "unknown"
                run_id = stack_name
            
            # Get status and algorithm from stack state
            status = "Unknown"
            algorithm = "-"
            
            # Check if run directory exists
            run_dir = base_dir / run_id
            if run_dir.exists():
                try:
                    # Try to get outputs without refresh (fast)
                    outputs = automation.outputs("adaptive", stack_env, run_id, refresh=False, work_dir=str(run_dir))
                    if outputs:
                        algorithm = automation.get_output_value(outputs, 'algorithm', '-')
                        job_status = automation.get_output_value(outputs, 'status', '')
                        if job_status:
                            status = f"✓ {job_status}"
                        else:
                            status = "✓ Deployed"
                    else:
                        status = "⚠ Not deployed"
                except Exception:
                    # Keep unknown status on error
                    pass
            
            table.add_row(run_id, stack_env, status, algorithm)
        
        console.print(table)
        info("\nUse 'mops adaptive status <run-id>' for details")
        info("Use 'mops adaptive down <run-id>' to clean up")
        
    except Exception as e:
        error(f"Error listing runs: {e}")
        raise typer.Exit(1)


@app.command()
def logs(
    run_id: str = run_id_option(),
    env: Optional[str] = env_option(),
    follow: bool = typer.Option(
        False,
        "--follow", "-f",
        help="Follow log output"
    ),
    tail: int = typer.Option(
        100,
        "--tail", "-t",
        help="Number of lines to show from the end"
    )
):
    """Get logs from an adaptive run.
    
    This is a convenience wrapper around kubectl logs.
    """
    env = resolve_env(env)
    
    # Sanitize run_id for consistency
    run_id = dns_1123_label(run_id, fallback="run")
    
    # Check if run directory exists
    from ..core.paths import ensure_work_dir
    base_dir = ensure_work_dir("adaptive")
    work_dir = base_dir / run_id
    
    if not work_dir.exists():
        warning(f"Run not found: {run_id}")
        raise typer.Exit(0)
    
    try:
        # Get namespace from stack outputs with custom work_dir
        outputs = automation.outputs("adaptive", env, run_id, refresh=False, work_dir=str(work_dir))
        
        if not outputs:
            warning(f"Run {run_id} has no outputs")
            raise typer.Exit(0)
        
        namespace = automation.get_output_value(outputs, 'namespace', f'adaptive-{run_id}')
        job_name = automation.get_output_value(outputs, 'job_name', f'adaptive-{run_id}')
        
        # Build kubectl command
        import subprocess
        
        cmd = [
            "kubectl", "logs",
            "-n", namespace,
            "-l", f"job-name={job_name}",
            f"--tail={tail}"
        ]
        
        if follow:
            cmd.append("-f")
        
        info(f"Fetching logs from namespace: {namespace}\n")
        
        # Run kubectl
        subprocess.run(cmd)
        
    except Exception as e:
        error(f"Error getting logs: {e}")
        raise typer.Exit(1)