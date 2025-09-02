"""Workspace management CLI commands for Dask deployment."""

import typer
import yaml
import pulumi.automation as auto
from pathlib import Path
from typing import Optional
from rich.console import Console
from ..core import StackNaming

app = typer.Typer(help="Manage Dask workspaces")
console = Console()


@app.command()
def up(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c",
        help="Workspace configuration file (YAML)"
    ),
    infra_stack: str = typer.Option(
        "modelops-infra",
        "--infra-stack",
        help="Infrastructure stack to reference"
    ),
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    )
):
    """Deploy Dask workspace on existing infrastructure.
    
    This creates Stack 2 which references Stack 1 (infrastructure) to get
    the kubeconfig and deploy Dask scheduler and workers.
    
    Example:
        mops workspace up --env dev
        mops workspace up --config workspace.yaml --infra-stack modelops-infra-prod
    """
    # Load configuration if provided
    workspace_config = {}
    if config and config.exists():
        with open(config) as f:
            workspace_config = yaml.safe_load(f)
    
    def pulumi_program():
        """Create DaskWorkspace in Stack 2 context."""
        from ..infra.components.workspace import DaskWorkspace
        
        # Use centralized naming for infrastructure reference
        infra_ref = StackNaming.get_infra_stack_ref(env)
        
        # Pass environment to workspace config
        workspace_config["environment"] = env
        
        return DaskWorkspace("dask", infra_ref, workspace_config)
    
    # Use centralized naming for stack and project
    stack_name = StackNaming.get_stack_name("workspace", env)
    project_name = StackNaming.get_project_name("workspace")
    
    # Use consistent backend structure
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "workspace"
    backend_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path.home() / ".modelops" / "pulumi" / "workspace"
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
        
        console.print(f"\n[bold]Deploying Dask workspace to environment: {env}[/bold]")
        console.print(f"Infrastructure stack: {infra_stack}-{env}")
        console.print(f"Workspace stack: {stack_name}\n")
        
        console.print("[yellow]Creating Dask resources...[/yellow]")
        result = stack.up(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        outputs = result.outputs
        
        console.print("\n[green]✓ Workspace deployed successfully![/green]")
        console.print(f"  Scheduler: {outputs.get('scheduler_address', {}).value if outputs.get('scheduler_address') else 'unknown'}")
        console.print(f"  Dashboard: {outputs.get('dashboard_url', {}).value if outputs.get('dashboard_url') else 'unknown'}")
        console.print(f"  Namespace: {outputs.get('namespace', {}).value if outputs.get('namespace') else 'unknown'}")
        console.print(f"  Workers: {outputs.get('worker_count', {}).value if outputs.get('worker_count') else 'unknown'}")
        
        console.print(f"\n[bold]Port-forward dashboard:[/bold]")
        namespace = outputs.get('namespace', {}).value if outputs.get('namespace') else 'modelops-dask'
        console.print(f"  kubectl port-forward -n {namespace} svc/dask-scheduler 8787:8787")
        console.print(f"\nThen visit: http://localhost:8787")
        
    except Exception as e:
        console.print(f"\n[red]Error deploying workspace: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def down(
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
    """Destroy Dask workspace.
    
    This removes all Dask resources but leaves the underlying
    Kubernetes cluster intact.
    """
    if not yes:
        console.print("\n[bold yellow]⚠️  Warning[/bold yellow]")
        console.print(f"This will destroy the Dask workspace in environment: {env}")
        console.print("All running Dask jobs will be terminated.")
        
        confirm = typer.confirm("\nAre you sure you want to destroy the workspace?")
        if not confirm:
            console.print("[green]Destruction cancelled[/green]")
            raise typer.Exit(0)
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("workspace", env)
    project_name = StackNaming.get_project_name("workspace")
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "workspace"
    work_dir = Path.home() / ".modelops" / "pulumi" / "workspace"
    
    if not work_dir.exists():
        console.print(f"[yellow]No workspace found for environment: {env}[/yellow]")
        raise typer.Exit(0)
    
    try:
        # Need minimal program for destroy
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
        
        console.print(f"\n[yellow]Destroying workspace: {stack_name}...[/yellow]")
        stack.destroy(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        console.print("\n[green]✓ Workspace destroyed successfully[/green]")
        
    except Exception as e:
        console.print(f"\n[red]Error destroying workspace: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def status(
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    )
):
    """Show workspace status and connection details."""
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("workspace", env)
    project_name = StackNaming.get_project_name("workspace")
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "workspace"
    work_dir = Path.home() / ".modelops" / "pulumi" / "workspace"
    
    if not work_dir.exists() or not backend_dir.exists():
        console.print(f"[yellow]No workspace found for environment: {env}[/yellow]")
        console.print("\nRun 'mops workspace up' to create a workspace")
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
            console.print(f"[yellow]Workspace stack exists but has no outputs[/yellow]")
            console.print("The workspace may not be fully deployed.")
            raise typer.Exit(0)
        
        console.print(f"\n[bold]Workspace Status[/bold]")
        console.print(f"  Environment: {env}")
        console.print(f"  Stack: {stack_name}")
        console.print(f"  Scheduler: {outputs.get('scheduler_address', {}).value if outputs.get('scheduler_address') else 'unknown'}")
        console.print(f"  Dashboard: {outputs.get('dashboard_url', {}).value if outputs.get('dashboard_url') else 'unknown'}")
        console.print(f"  Namespace: {outputs.get('namespace', {}).value if outputs.get('namespace') else 'unknown'}")
        console.print(f"  Workers: {outputs.get('worker_count', {}).value if outputs.get('worker_count') else 'unknown'}")
        console.print(f"  Image: {outputs.get('image', {}).value if outputs.get('image') else 'unknown'}")
        
        console.print(f"\n[bold]Connection commands:[/bold]")
        namespace = outputs.get('namespace', {}).value if outputs.get('namespace') else 'modelops-dask'
        console.print(f"  Port-forward: kubectl port-forward -n {namespace} svc/dask-scheduler 8787:8787")
        console.print(f"  Logs: kubectl logs -n {namespace} -l app=dask-scheduler")
        console.print(f"  Workers: kubectl get pods -n {namespace} -l app=dask-worker")
        
    except Exception as e:
        console.print(f"[red]Error querying workspace status: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def list():
    """List all workspaces across environments."""
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "workspace"
    
    if not backend_dir.exists():
        console.print("[yellow]No workspaces found[/yellow]")
        console.print("\nRun 'mops workspace up' to create a workspace")
        return
    
    # Find all stack files
    stack_files = list(backend_dir.rglob("*.json"))
    
    if not stack_files:
        console.print("[yellow]No workspaces found[/yellow]")
        return
    
    console.print("\n[bold]Available Workspaces[/bold]")
    
    for stack_file in stack_files:
        if ".pulumi" in str(stack_file) and "stacks" in str(stack_file):
            stack_name = stack_file.stem
            env = stack_name.replace("modelops-workspace-", "")
            
            # Try to read basic info from stack file
            try:
                import json
                with open(stack_file) as f:
                    stack_data = json.load(f)
                    
                # Check if it has outputs
                has_outputs = bool(stack_data.get("checkpoint", {}).get("latest", {}).get("resources"))
                status = "[green]✓ Deployed[/green]" if has_outputs else "[yellow]⚠ Not deployed[/yellow]"
                
                console.print(f"  • {env}: {status}")
                
            except Exception:
                console.print(f"  • {env}: [dim]Unknown status[/dim]")
    
    console.print("\nUse 'mops workspace status --env <env>' for details")