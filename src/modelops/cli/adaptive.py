"""Adaptive plane CLI commands for optimization runs."""

import typer
import yaml
import pulumi.automation as auto
from pathlib import Path
from typing import Optional
from datetime import datetime
from rich.console import Console
from rich.table import Table
from ..core import StackNaming
from ..core.paths import BACKEND_DIR, WORK_DIRS, ensure_work_dir

app = typer.Typer(help="Manage adaptive optimization runs")
console = Console()


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
        "modelops-infra",
        "--infra-stack",
        help="Infrastructure stack name (without env suffix)"
    ),
    workspace_stack: str = typer.Option(
        "modelops-workspace",
        "--workspace-stack",
        help="Workspace stack name (without env suffix)"
    ),
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    )
):
    """Start an adaptive optimization run.
    
    This creates Stack 3 which references both Stack 1 (infrastructure) and
    Stack 2 (workspace) to deploy adaptive workers that connect to Dask.
    
    Example:
        mops adaptive up optuna-config.yaml
        mops adaptive up experiment.yaml --run-id exp-001 --env prod
    """
    # Generate run ID if not provided
    if not run_id:
        run_id = f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    # Load configuration
    with open(config) as f:
        run_config = yaml.safe_load(f)
    
    def pulumi_program():
        """Create AdaptiveRun in Stack 3 context."""
        from ..infra.components.adaptive import AdaptiveRun
        
        # Use centralized naming for fully-qualified stack references
        infra_ref = StackNaming.ref("infra", env)
        workspace_ref = StackNaming.ref("workspace", env)
        
        return AdaptiveRun(
            run_id,
            infra_stack_ref=infra_ref,
            workspace_stack_ref=workspace_ref,
            config=run_config
        )
    
    # Use centralized naming for stack and project
    stack_name = StackNaming.get_stack_name("adaptive", env, run_id)
    project_name = StackNaming.get_project_name("adaptive")
    
    # Use unified backend for cross-stack references
    backend_dir = BACKEND_DIR
    backend_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path.home() / ".modelops" / "pulumi" / "adaptive" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        console.print(f"\n[bold]Starting adaptive run: {run_id}[/bold]")
        console.print(f"  Environment: {env}")
        console.print(f"  Algorithm: {run_config.get('algorithm', 'optuna')}")
        console.print(f"  Trials: {run_config.get('n_trials', 100)}")
        console.print(f"  Parallel workers: {run_config.get('n_parallel', 10)}\n")
        
        console.print("[yellow]Creating adaptive resources...[/yellow]")
        result = stack.up(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        outputs = result.outputs
        
        console.print(f"\n[green]✓ Adaptive run started![/green]")
        console.print(f"  Run ID: {run_id}")
        console.print(f"  Namespace: {outputs.get('namespace', {}).value if outputs.get('namespace') else 'unknown'}")
        console.print(f"  Job: {outputs.get('job_name', {}).value if outputs.get('job_name') else 'unknown'}")
        
        if outputs.get('postgres_dsn'):
            console.print(f"  Database: [green]✓[/green] Postgres provisioned")
        
        namespace = outputs.get('namespace', {}).value if outputs.get('namespace') else f'adaptive-{run_id}'
        job_name = outputs.get('job_name', {}).value if outputs.get('job_name') else f'adaptive-{run_id}'
        
        console.print(f"\n[bold]Monitor progress:[/bold]")
        console.print(f"  Logs: kubectl logs -n {namespace} -l job-name={job_name}")
        console.print(f"  Status: kubectl get job -n {namespace} {job_name}")
        console.print(f"  Pods: kubectl get pods -n {namespace} -l job-name={job_name}")
        
    except Exception as e:
        console.print(f"\n[red]Error starting run: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def down(
    run_id: str = typer.Argument(
        ...,
        help="Run ID to destroy"
    ),
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip confirmation prompt"
    )
):
    """Destroy an adaptive run and clean up resources.
    
    This removes all resources associated with the run including
    any Postgres database and persistent volumes.
    """
    if not yes:
        console.print(f"\n[bold yellow]⚠️  Warning[/bold yellow]")
        console.print(f"This will destroy adaptive run: {run_id}")
        console.print("All data associated with this run will be lost.")
        
        confirm = typer.confirm("\nAre you sure you want to destroy this run?")
        if not confirm:
            console.print("[green]Destruction cancelled[/green]")
            raise typer.Exit(0)
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("adaptive", env, run_id)
    project_name = StackNaming.get_project_name("adaptive")
    
    backend_dir = BACKEND_DIR
    work_dir = Path.home() / ".modelops" / "pulumi" / "adaptive" / run_id
    
    if not work_dir.exists():
        console.print(f"[yellow]Run not found: {run_id}[/yellow]")
        raise typer.Exit(0)
    
    try:
        # Minimal program for destroy
        def pulumi_program():
            pass
        
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        console.print(f"\n[yellow]Destroying run: {run_id}...[/yellow]")
        stack.destroy(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        # Clean up work directory
        import shutil
        shutil.rmtree(work_dir)
        
        console.print(f"\n[green]✓ Run {run_id} destroyed successfully[/green]")
        
    except Exception as e:
        console.print(f"\n[red]Error destroying run: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def status(
    run_id: str = typer.Argument(
        ...,
        help="Run ID to check"
    ),
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    )
):
    """Check status of an adaptive run."""
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("adaptive", env, run_id)
    project_name = StackNaming.get_project_name("adaptive")
    
    backend_dir = BACKEND_DIR
    work_dir = Path.home() / ".modelops" / "pulumi" / "adaptive" / run_id
    
    if not work_dir.exists():
        console.print(f"[yellow]Run not found: {run_id}[/yellow]")
        raise typer.Exit(0)
    
    try:
        # Minimal program to query stack
        def pulumi_program():
            pass
        
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        outputs = stack.outputs()
        
        if not outputs:
            console.print(f"[yellow]Run exists but has no outputs[/yellow]")
            console.print("The run may not be fully deployed.")
            raise typer.Exit(0)
        
        console.print(f"\n[bold]Run Status[/bold]")
        console.print(f"  Run ID: {run_id}")
        console.print(f"  Stack: {stack_name}")
        console.print(f"  Algorithm: {outputs.get('algorithm', {}).value if outputs.get('algorithm') else 'unknown'}")
        console.print(f"  Trials: {outputs.get('n_trials', {}).value if outputs.get('n_trials') else 'unknown'}")
        console.print(f"  Namespace: {outputs.get('namespace', {}).value if outputs.get('namespace') else 'unknown'}")
        console.print(f"  Job: {outputs.get('job_name', {}).value if outputs.get('job_name') else 'unknown'}")
        console.print(f"  Status: {outputs.get('status', {}).value if outputs.get('status') else 'unknown'}")
        
        if outputs.get('postgres_dsn'):
            console.print(f"  Database: [green]✓[/green] Postgres connected")
        
        if outputs.get('scheduler_address'):
            console.print(f"  Scheduler: {outputs.get('scheduler_address', {}).value}")
        
        namespace = outputs.get('namespace', {}).value if outputs.get('namespace') else f'adaptive-{run_id}'
        job_name = outputs.get('job_name', {}).value if outputs.get('job_name') else f'adaptive-{run_id}'
        
        console.print(f"\n[bold]Commands:[/bold]")
        console.print(f"  Logs: kubectl logs -n {namespace} -l job-name={job_name}")
        console.print(f"  Job status: kubectl describe job -n {namespace} {job_name}")
        console.print(f"  Pod details: kubectl get pods -n {namespace} -l job-name={job_name} -o wide")
        
    except Exception as e:
        console.print(f"[red]Error querying run status: {e}[/red]")
        raise typer.Exit(1)


@app.command(name="list")
def list_runs():
    """List all adaptive runs."""
    
    adaptive_dir = Path.home() / ".modelops" / "pulumi" / "adaptive"
    
    if not adaptive_dir.exists():
        console.print("[yellow]No adaptive runs found[/yellow]")
        console.print("\nRun 'mops adaptive up <config>' to start a run")
        return
    
    # Find all run directories
    run_dirs = [d for d in adaptive_dir.iterdir() if d.is_dir()]
    
    if not run_dirs:
        console.print("[yellow]No adaptive runs found[/yellow]")
        return
    
    # Create table
    table = Table(title="Adaptive Runs")
    table.add_column("Run ID", style="cyan")
    table.add_column("Stack", style="yellow")
    table.add_column("Status", style="green")
    table.add_column("Created", style="dim")
    
    for run_dir in sorted(run_dirs):
        run_id = run_dir.name
        # Parse environment from directory structure or use default
        env = "dev"  # Default, could be enhanced to parse from stack files
        stack_name = StackNaming.get_stack_name("adaptive", env, run_id)
        
        # Check if stack exists in backend
        backend_dir = BACKEND_DIR
        stack_file = backend_dir / ".pulumi" / "stacks" / f"{stack_name}.json"
        
        if stack_file.exists():
            # Try to get basic info
            try:
                import json
                with open(stack_file) as f:
                    stack_data = json.load(f)
                
                # Check deployment status
                has_resources = bool(stack_data.get("checkpoint", {}).get("latest", {}).get("resources"))
                status = "✓ Deployed" if has_resources else "⚠ Not deployed"
                
                # Get creation time
                created = datetime.fromtimestamp(run_dir.stat().st_ctime).strftime("%Y-%m-%d %H:%M")
                
            except Exception:
                status = "? Unknown"
                created = "-"
        else:
            status = "✗ No stack"
            created = "-"
        
        table.add_row(run_id, stack_name, status, created)
    
    console.print(table)
    console.print("\nUse 'mops adaptive status <run-id>' for details")
    console.print("Use 'mops adaptive down <run-id>' to clean up")


@app.command()
def logs(
    run_id: str = typer.Argument(
        ...,
        help="Run ID to get logs for"
    ),
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    ),
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
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("adaptive", env, run_id)
    work_dir = Path.home() / ".modelops" / "pulumi" / "adaptive" / run_id
    
    if not work_dir.exists():
        console.print(f"[yellow]Run not found: {run_id}[/yellow]")
        raise typer.Exit(0)
    
    try:
        # Get namespace from stack outputs
        project_name = StackNaming.get_project_name("adaptive")
        backend_dir = BACKEND_DIR
        
        def pulumi_program():
            pass
        
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        outputs = stack.outputs()
        
        if not outputs:
            console.print(f"[yellow]Run {run_id} has no outputs[/yellow]")
            raise typer.Exit(0)
        
        namespace = outputs.get('namespace', {}).value if outputs.get('namespace') else f'adaptive-{run_id}'
        job_name = outputs.get('job_name', {}).value if outputs.get('job_name') else f'adaptive-{run_id}'
        
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
        
        console.print(f"[dim]Fetching logs from namespace: {namespace}[/dim]\n")
        
        # Run kubectl
        subprocess.run(cmd)
        
    except Exception as e:
        console.print(f"[red]Error getting logs: {e}[/red]")
        raise typer.Exit(1)