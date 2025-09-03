"""Workspace management CLI commands for Dask deployment."""

import typer
import pulumi.automation as auto
from pathlib import Path
from typing import Optional
from ..core import StackNaming
from ..core.paths import ensure_work_dir, get_backend_url
from ..core.config import ModelOpsConfig
from ..components import WorkspaceConfig
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
        StackNaming.get_project_name("infra"),
        "--infra-stack",
        help=f"Infrastructure stack name (default: {StackNaming.get_project_name('infra')}, auto-appends env for default)"
    ),
    env: Optional[str] = typer.Option(
        None,
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
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
    # Load and validate configuration if provided
    validated_config = WorkspaceConfig.from_yaml_optional(config)
    workspace_config = validated_config.to_pulumi_config() if validated_config else {}
    
    # Always use StackNaming.ref for consistency
    infra_ref = StackNaming.ref("infra", env)
    
    def pulumi_program():
        """Create DaskWorkspace in Stack 2 context."""
        from ..infra.components.workspace import DaskWorkspace
        import pulumi
        
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
    
    # Use paths.py for consistent directory management
    work_dir = ensure_work_dir("workspace")
    backend_url = get_backend_url()
    
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
                    backend=auto.ProjectBackend(url=backend_url)
                )
            )
        )
        
        info(f"\n[bold]Deploying Dask workspace to environment: {env}[/bold]")
        # Display the actual resolved stack reference, not the raw input
        display_name = infra_ref.split('/')[-1] if '/' in infra_ref else infra_stack
        info(f"Infrastructure stack: {display_name}")
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
    env: Optional[str] = typer.Option(
        None,
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
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
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
    
    work_dir = ensure_work_dir("workspace")
    backend_url = get_backend_url()
    
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
                    backend=auto.ProjectBackend(url=backend_url)
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
    env: Optional[str] = typer.Option(
        None,
        "--env", "-e",
        help="Environment name"
    )
):
    """Show workspace status and connection details."""
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("workspace", env)
    project_name = StackNaming.get_project_name("workspace")
    
    work_dir = ensure_work_dir("workspace")
    backend_url = get_backend_url()
    
    if not work_dir.exists():
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
                    backend=auto.ProjectBackend(url=backend_url)
                )
            )
        )
        
        # Refresh stack to get current state from backend
        # Without refresh, outputs show stale/cached data  
        stack.refresh(on_output=lambda _: None)
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
    """List all workspaces across environments using Pulumi Automation API."""
    project_name = StackNaming.get_project_name("workspace")
    work_dir = ensure_work_dir("workspace")
    backend_url = get_backend_url()

    # Check if any workspaces exist
    from ..core.paths import BACKEND_DIR
    if not BACKEND_DIR.exists():
        console.print("[yellow]No workspaces found[/yellow]")
        console.print("\nRun 'mops workspace up' to create a workspace")
        return

    try:
        # Create a LocalWorkspace bound to the workspace project + backend
        ws = auto.LocalWorkspace(
            work_dir=str(work_dir),
            project_settings=auto.ProjectSettings(
                name=project_name,
                runtime="python",
                backend=auto.ProjectBackend(url=backend_url)
            )
        )

        # List stacks registered for this project in this backend
        stacks = ws.list_stacks()  # -> List[StackSummary]
        if not stacks:
            console.print("[yellow]No workspaces found[/yellow]")
            return

        console.print("\n[bold]Available Workspaces[/bold]")

        # Sort for stable output
        for s in sorted(stacks, key=lambda ss: ss.name):
            stack_name = s.name
            # env from the standardized stack name
            try:
                env = StackNaming.parse_stack_name(stack_name)["env"]
            except Exception:
                env = stack_name  # fallback: show the raw name

            status = "[dim]Unknown[/dim]"
            try:
                # Select stack (no-op program) to read state safely
                def _noop():  # minimal program
                    pass

                st = auto.select_stack(
                    stack_name=stack_name,
                    project_name=project_name,
                    program=_noop,
                    opts=auto.LocalWorkspaceOptions(
                        work_dir=str(work_dir),
                        project_settings=auto.ProjectSettings(
                            name=project_name,
                            runtime="python",
                            backend=auto.ProjectBackend(url=backend_url)
                        )
                    )
                )

                # Fast state read: no refresh (avoid slowing down listing)
                # Export returns a Deployment object with a .deployment dict attribute
                state = st.export_stack()
                
                # The Deployment object has a .deployment attribute that is a dict
                if hasattr(state, 'deployment') and isinstance(state.deployment, dict):
                    resources = state.deployment.get("resources", [])
                    # Consider it "deployed" if there are any real resources beyond the Stack resource itself
                    has_real = any(r.get("type") != "pulumi:pulumi:Stack" for r in resources)
                    status = "[green]✓ Deployed[/green]" if has_real else "[yellow]⚠ Not deployed[/yellow]"
                else:
                    # Fallback if structure is unexpected
                    status = "[yellow]⚠ Unknown state[/yellow]"

            except Exception as e:
                # Keep Unknown status on error
                pass

            console.print(f"  • {env}: {status}")

        console.print("\nUse 'mops workspace status --env <env>' for details")

    except Exception as e:
        error(f"Error listing workspaces: {e}")
        raise typer.Exit(1)
