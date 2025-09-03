"""Workspace management CLI commands for Dask deployment."""

import typer
import yaml
import pulumi.automation as auto
from pathlib import Path
from typing import Optional
from ..core import StackNaming
from .utils import handle_pulumi_error
from .display import (
    console, success, warning, error, info, section,
    workspace_info, workspace_commands, dim
)

app = typer.Typer(help="Manage Dask workspaces")


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
        import pulumi
        
        # Use centralized naming for infrastructure reference
        # For file backends, the organization is always "organization" (Pulumi constant)
        infra_project = StackNaming.get_project_name("infra")
        infra_stack = StackNaming.get_infra_stack_ref(env)
        infra_ref = f"organization/{infra_project}/{infra_stack}"
        
        # Pass environment to workspace config
        workspace_config["environment"] = env
        
        # Create the workspace component
        workspace = DaskWorkspace("dask", infra_ref, workspace_config)
        
        # Export outputs at stack level for visibility
        pulumi.export("scheduler_address", workspace.scheduler_address)
        pulumi.export("dashboard_url", workspace.dashboard_url)
        pulumi.export("namespace", workspace.namespace)
        pulumi.export("worker_count", workspace.worker_count)
        
        return workspace
    
    # Use centralized naming for stack and project
    stack_name = StackNaming.get_stack_name("workspace", env)
    project_name = StackNaming.get_project_name("workspace")
    
    # Use the same backend as infrastructure (Azure backend) for stack references to work
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
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
        
        info(f"\n[bold]Deploying Dask workspace to environment: {env}[/bold]")
        info(f"Infrastructure stack: {infra_stack}-{env}")
        info(f"Workspace stack: {stack_name}\n")
        
        info("[yellow]Creating Dask resources...[/yellow]")
        result = stack.up(on_output=dim)
        
        outputs = result.outputs
        
        success("\nWorkspace deployed successfully!")
        workspace_info(outputs, env, stack_name)
        
    except auto.CommandError as e:
        handle_pulumi_error(e, str(work_dir), stack_name)
        raise typer.Exit(1)
    except Exception as e:
        error(f"\nError deploying workspace: {e}")
        handle_pulumi_error(e, str(work_dir), stack_name)
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
        warning("\nWarning")
        info(f"This will destroy the Dask workspace in environment: {env}")
        info("All running Dask jobs will be terminated.")
        
        confirm = typer.confirm("\nAre you sure you want to destroy the workspace?")
        if not confirm:
            success("Destruction cancelled")
            raise typer.Exit(0)
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("workspace", env)
    project_name = StackNaming.get_project_name("workspace")
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    work_dir = Path.home() / ".modelops" / "pulumi" / "workspace"
    
    if not work_dir.exists():
        warning(f"No workspace found for environment: {env}")
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
        
        info(f"\n[yellow]Destroying workspace: {stack_name}...[/yellow]")
        stack.destroy(on_output=dim)
        
        success("\nWorkspace destroyed successfully")
        
    except auto.CommandError as e:
        handle_pulumi_error(e, str(work_dir), stack_name)
        raise typer.Exit(1)
    except Exception as e:
        error(f"\nError destroying workspace: {e}")
        handle_pulumi_error(e, str(work_dir), stack_name)
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
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    work_dir = Path.home() / ".modelops" / "pulumi" / "workspace"
    
    if not work_dir.exists() or not backend_dir.exists():
        warning(f"No workspace found for environment: {env}")
        info("\nRun 'mops workspace up' to create a workspace")
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
            warning("Workspace stack exists but has no outputs")
            info("The workspace may not be fully deployed.")
            raise typer.Exit(0)
        
        section("Workspace Status")
        workspace_info(outputs, env, stack_name)
        
        namespace = outputs.get('namespace', {}).value if outputs.get('namespace') else StackNaming.get_namespace("dask", env)
        workspace_commands(namespace)
        
    except Exception as e:
        error(f"Error querying workspace status: {e}")
        raise typer.Exit(1)


@app.command(name="list")
def list_workspaces():
    """List all workspaces across environments."""
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    
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