"""Workspace management commands for ModelOps CLI."""

import typer
import subprocess
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from datetime import datetime
import pulumi.automation as auto

from ..state.manager import StateManager
from ..state.models import WorkspaceState
from ..infra.config import WorkspaceConfig
from ..infra.workspace import WorkspaceStack, WorkspaceOutputs
from ..infra.providers.registry import ProviderRegistry

app = typer.Typer(help="Manage Dask workspaces for simulation execution")
console = Console()
state = StateManager()


def require_workspace(name: str) -> WorkspaceState:
    """Helper to get workspace or exit."""
    workspace = state.get_workspace(name)
    if not workspace:
        console.print(f"[red]Workspace '{name}' not found.[/red]")
        raise typer.Exit(1)
    return workspace

@app.command()
def up(
    name: str = typer.Option("default", "--name", "-n", help="Workspace name"),
    provider: str = typer.Option("orbstack", "--provider", "-p", help="Infrastructure provider"),
    min_workers: Optional[int] = typer.Option(None, "--min-workers", help="Override minimum number of workers"),
    max_workers: Optional[int] = typer.Option(None, "--max-workers", help="Override maximum number of workers"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without provisioning")
):
    """Provision a new Dask workspace for simulation execution."""
    
    # Check if workspace already exists
    if state.workspace_exists(name):
        console.print(f"[yellow]Workspace '{name}' already exists.[/yellow]")
        console.print("Use 'mops workspace status' to check its state.")
        raise typer.Exit(1)
    
    try:
        # Load provider FIRST to get its defaults
        console.print("\n[dim]Loading provider configuration...[/dim]")
        workspace_provider = ProviderRegistry.get(provider)
        workspace_provider.validate()
        
        # Get provider-specific defaults
        provider_defaults = workspace_provider.get_resource_defaults()
        
        # Create workspace configuration with provider defaults + CLI overrides
        config = WorkspaceConfig.from_cli_args(
            name=name,
            provider_defaults=provider_defaults,
            min_workers=min_workers,  # None if not specified by user
            max_workers=max_workers    # None if not specified by user
        )
        config.validate()
        
        # Show what we're using (after defaults are applied)
        console.print(f"[bold]Creating workspace '{name}'...[/bold]")
        console.print(f"  Provider: {provider}")
        console.print(f"  Workers: {config.min_workers} (memory: {config.worker_memory}, cpu: {config.worker_cpu})")
        console.print(f"  Scheduler: memory: {config.scheduler_memory}, cpu: {config.scheduler_cpu}")
        
        # Create Pulumi stack
        workspace_stack = WorkspaceStack(config, workspace_provider)
        
        # Setup Pulumi automation
        stack_name = f"modelops-{name}"
        project_name = "modelops-workspace"
        
        console.print(f"[dim]Initializing Pulumi stack '{stack_name}'...[/dim]")
        
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=workspace_stack.create_program()
        )
        
        # Configure stack
        stack.set_config("kubernetes:suppressDeprecationWarnings", auto.ConfigValue("true"))
        
        if dry_run:
            console.print("\n[yellow]Preview mode - showing planned changes:[/yellow]")
            preview_result = stack.preview(on_output=console.print)
            return
        
        # Run Pulumi up
        console.print("\n[bold]Provisioning infrastructure...[/bold]")
        console.print("[dim]This may take a few minutes...[/dim]\n")
        
        up_result = stack.up(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        # Extract outputs
        outputs = WorkspaceOutputs(
            name=name,
            namespace=up_result.outputs["namespace"].value,
            scheduler_address=up_result.outputs["scheduler_address"].value,
            dashboard_hint=up_result.outputs["dashboard_hint"].value
        )
        
        # Create WorkspaceState with all configuration
        workspace_state = WorkspaceState(
            outputs=outputs,
            provider=provider,
            status="running",
            image=config.image,
            min_workers=config.min_workers,
            max_workers=config.max_workers,
            worker_memory=config.worker_memory,
            worker_cpu=config.worker_cpu,
            scheduler_memory=config.scheduler_memory,
            scheduler_cpu=config.scheduler_cpu
        )
        
        # Save to state
        state.save_workspace(name, workspace_state)
        
        console.print(f"\n[green]✓ Workspace '{name}' created successfully![/green]")
        console.print(f"  Scheduler: {outputs.scheduler_address}")
        console.print(f"  Namespace: {outputs.namespace}")
        console.print(f"\n[dim]{outputs.dashboard_hint}[/dim]")
        
    except Exception as e:
        console.print(f"\n[red]Failed to create workspace: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def down(
    name: str = typer.Option("default", "--name", "-n", help="Workspace name"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt")
):
    """Destroy a Dask workspace and clean up resources."""
    
    # Check if workspace exists
    workspace = require_workspace(name)
    
    # Confirmation prompt
    if not force:
        confirm = typer.confirm(
            f"Are you sure you want to destroy workspace '{name}'? "
            "This will delete all associated resources."
        )
        if not confirm:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(0)
    
    console.print(f"[bold]Destroying workspace '{name}'...[/bold]")
    
    try:
        # Select the existing Pulumi stack
        stack_name = f"modelops-{name}"
        project_name = "modelops-workspace"
        
        console.print(f"[dim]Selecting Pulumi stack '{stack_name}'...[/dim]")
        
        stack = auto.select_stack(
            stack_name=stack_name,
            project_name=project_name,
            # Program is required but not used for destroy
            program=lambda: None
        )
        
        # Destroy the stack
        console.print("[dim]Destroying infrastructure...[/dim]\n")
        stack.destroy(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        # Remove the stack completely
        console.print(f"\n[dim]Removing stack '{stack_name}'...[/dim]")
        stack.workspace.remove_stack(stack_name)
        
        # Remove from state
        state.remove_workspace(name)
        
        console.print(f"\n[green]✓ Workspace '{name}' destroyed successfully.[/green]")
        
    except Exception as e:
        console.print(f"[red]Failed to destroy workspace: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def status(
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Workspace name (shows all if not specified)")
):
    """Show status of workspace(s)."""
    
    if name:
        # Show specific workspace
        workspace = require_workspace(name)
        
        # Create status panel
        panel_content = f"""[bold]Status:[/bold] {workspace.status}
[bold]Provider:[/bold] {workspace.provider}
[bold]Scheduler:[/bold] {workspace.scheduler_address}
[bold]Namespace:[/bold] {workspace.namespace}
[bold]Workers:[/bold] {workspace.worker_range} (autoscaling)
[bold]Resources:[/bold] {workspace.resource_summary}
[bold]Created:[/bold] {workspace.created_at or 'unknown'}
[bold]Updated:[/bold] {workspace.updated_at or 'unknown'}"""
        
        console.print(Panel(panel_content, title=f"Workspace: {name}", border_style="green"))
        
        # TODO: Query actual Kubernetes resources
        # kubectl get pods -n {namespace}
        
    else:
        # Show all workspaces
        workspaces = state.list_workspaces()
        
        if not workspaces:
            console.print("[yellow]No workspaces found.[/yellow]")
            console.print("Create one with: mops workspace up")
            return
        
        # Create table
        table = Table(title="ModelOps Workspaces", show_lines=True)
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Status", style="green")
        table.add_column("Provider", style="blue")
        table.add_column("Workers", justify="center")
        table.add_column("Created", style="dim")
        
        for name, workspace in workspaces.items():
            created = workspace.created_at or 'unknown'
            if created != 'unknown':
                try:
                    dt = datetime.fromisoformat(created)
                    created = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    pass
            
            table.add_row(
                name,
                workspace.status,
                workspace.provider,
                workspace.worker_range,
                created
            )
        
        console.print(table)


@app.command()
def list():
    """List all workspaces (alias for status)."""
    status(name=None)


@app.command() 
def connect(
    name: str = typer.Option("default", "--name", "-n", help="Workspace name")
):
    """Get connection details for a workspace."""
    
    workspace = require_workspace(name)
    
    scheduler_addr = workspace.scheduler_address
    namespace = workspace.namespace
    
    # Show port-forward instructions first for laptop access
    external_instructions = f"""[bold]From your laptop (recommended):[/bold]
1. Port-forward the scheduler:
   [dim]mops workspace port-forward --name {name} --scheduler[/dim]
   
2. Connect from Python:
   [dim]from modelops.services import DaskSimulationService
   sim = DaskSimulationService("tcp://localhost:8786")[/dim]

3. View dashboard:
   [dim]mops workspace port-forward --name {name} --dashboard
   # Then open http://localhost:8787[/dim]
"""

    internal_instructions = f"""[bold]From pods in namespace '{namespace}':[/bold]
   [dim]from modelops.services import DaskSimulationService
   sim = DaskSimulationService("{scheduler_addr}")[/dim]
"""
    
    console.print(Panel(external_instructions + "\n" + internal_instructions, 
                       title=f"Connection Details: {name}", 
                       border_style="blue"))


@app.command()
def port_forward(
    name: str = typer.Option("default", "--name", "-n", help="Workspace name"),
    dashboard: bool = typer.Option(True, "--dashboard/--no-dashboard", help="Forward dashboard port"),
    scheduler: bool = typer.Option(False, "--scheduler", help="Forward scheduler port"),
    dashboard_port: int = typer.Option(8787, "--dashboard-port", help="Local dashboard port"),
    scheduler_port: int = typer.Option(8786, "--scheduler-port", help="Local scheduler port")
):
    """Forward Dask services to localhost for development access."""
    
    workspace = require_workspace(name)
    
    namespace = workspace.namespace
    if not namespace:
        console.print(f"[red]Workspace '{name}' has no namespace.[/red]")
        raise typer.Exit(1)
    
    if not dashboard and not scheduler:
        console.print("[yellow]Nothing to forward. Use --dashboard or --scheduler.[/yellow]")
        raise typer.Exit(1)
    
    processes = []
    
    try:
        if dashboard:
            console.print(f"[bold]Forwarding Dask dashboard...[/bold]")
            console.print(f"  Dashboard: http://localhost:{dashboard_port}")
            
            # Start dashboard port-forward
            p = subprocess.Popen([
                "kubectl", "port-forward",
                "-n", namespace,
                "svc/dask-dashboard",
                f"{dashboard_port}:8787"
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(("dashboard", p))
        
        if scheduler:
            console.print(f"[bold]Forwarding Dask scheduler...[/bold]")
            console.print(f"  Scheduler: tcp://localhost:{scheduler_port}")
            console.print(f"  Python: Client('tcp://localhost:{scheduler_port}')")
            
            # Start scheduler port-forward
            p = subprocess.Popen([
                "kubectl", "port-forward",
                "-n", namespace,
                "svc/dask-scheduler",
                f"{scheduler_port}:8786"
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(("scheduler", p))
        
        console.print("\n[yellow]Press Ctrl+C to stop port forwarding[/yellow]\n")
        
        # Wait for interrupt
        for name, p in processes:
            try:
                p.wait()
            except KeyboardInterrupt:
                break
                
    except KeyboardInterrupt:
        console.print("\n[dim]Stopping port forwarding...[/dim]")
    finally:
        # Clean up processes
        for name, p in processes:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
        
        console.print("[green]Port forwarding stopped.[/green]")
