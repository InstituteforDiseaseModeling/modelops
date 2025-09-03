"""Infrastructure management CLI commands.

Provider-agnostic infrastructure provisioning using ComponentResources.
"""

import typer
import pulumi
import pulumi.automation as auto
from pulumi.automation import ProjectSettings, ProjectBackend
from pathlib import Path
from typing import Optional
from rich.console import Console
from ..core import StackNaming
from ..core.paths import ensure_work_dir, get_backend_url
from ..core.config import ModelOpsConfig
from ..components import AzureProviderConfig
from .utils import handle_pulumi_error

app = typer.Typer(help="Manage infrastructure (Azure, AWS, GCP, local)")
console = Console()


# Note: We can't call these at module import time, so we use None as default
# and resolve inside the function


@app.command()
def up(
    config: Path = typer.Option(
        ...,
        "--config", "-c",
        help="Provider configuration file (YAML)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True
    ),
    env: Optional[str] = typer.Option(
        None,
        "--env", "-e",
        help="Environment name (dev, staging, prod)"
    )
):
    """Create infrastructure from zero based on provider config.
    
    This command reads a YAML configuration file and provisions
    infrastructure using Pulumi ComponentResources. The provider type
    is specified in the config file.
    
    Example:
        mops infra up --config ~/.modelops/providers/azure.yaml
    """
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
    # Load and validate configuration
    provider_config = AzureProviderConfig.from_yaml(config)
    
    console.print(f"[bold]Creating {provider_config.provider} infrastructure from zero...[/bold]")
    console.print(f"Config: {config}")
    console.print(f"Environment: {env}")
    console.print(f"Resource Group: {provider_config.resource_group}-{env}-rg-{provider_config.username}")
    
    def pulumi_program():
        """Pulumi program that creates infrastructure using ComponentResource."""
        import pulumi
        
        if provider_config.provider == "azure":
            from ..infra.components.azure import ModelOpsCluster
            # Pass validated config dict to component with environment
            config_dict = provider_config.to_pulumi_config()
            config_dict["environment"] = env
            cluster = ModelOpsCluster("modelops", config_dict)
            
            # Export outputs at the stack level for access via StackReference
            pulumi.export("kubeconfig", cluster.kubeconfig)
            pulumi.export("cluster_name", cluster.cluster_name)
            pulumi.export("resource_group", cluster.resource_group)
            pulumi.export("location", cluster.location)
            # ACR is now managed by separate registry stack
            pulumi.export("provider", pulumi.Output.from_input("azure"))
            
            return cluster
        else:
            raise ValueError(f"Provider '{provider_config.provider}' not yet implemented")
    
    # Use centralized naming for stack and project
    stack_name = StackNaming.get_stack_name("infra", env)
    project_name = StackNaming.get_project_name("infra")
    
    # Use paths.py for consistent directory management
    work_dir = ensure_work_dir("infra")
    backend_url = get_backend_url()
    
    try:
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=ProjectBackend(url=backend_url)
                )
            )
        )
        
        # Note: Azure Native provider gets configuration from environment/CLI
        # No need to set config keys - the provider will use Azure CLI credentials
        
        # Run pulumi up
        console.print("\n[yellow]Creating resources (this may take several minutes)...[/yellow]")
        result = stack.up(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        # Extract outputs from ComponentResource
        outputs = result.outputs
        
        # Verify kubeconfig exists in outputs
        if not outputs.get("kubeconfig"):
            console.print("[red]Error: No kubeconfig returned from infrastructure creation[/red]")
            raise typer.Exit(1)
        
        console.print("\n[green]✓ Infrastructure created successfully![/green]")
        console.print(f"  Provider: {provider_config.provider}")
        console.print(f"  Stack: {stack_name}")
        console.print("\nStack outputs saved. Query with:")
        console.print(f"  pulumi stack output --stack {stack_name} --cwd ~/.modelops/pulumi/{provider_config.provider}")
        console.print("\nGet kubeconfig:")
        console.print(f"  pulumi stack output kubeconfig --show-secrets --stack {stack_name} --cwd ~/.modelops/pulumi/{provider_config.provider}")
        console.print("\nNext steps:")
        console.print("  1. Run 'mops workspace up' to deploy Dask")
        console.print("  2. Run 'mops adaptive up' to start optimization")
        
    except auto.CommandError as e:
        handle_pulumi_error(e, str(work_dir), stack_name)
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"\n[red]Error creating infrastructure: {e}[/red]")
        handle_pulumi_error(e, str(work_dir), stack_name)
        raise typer.Exit(1)


@app.command()
def down(
    config: Path = typer.Option(
        ...,
        "--config", "-c",
        help="Provider configuration file (YAML)",
        exists=True
    ),
    env: Optional[str] = typer.Option(
        None,
        "--env", "-e",
        help="Environment name (dev, staging, prod)"
    ),
    delete_rg: bool = typer.Option(
        False,
        "--delete-rg",
        help="Also delete the resource group (dangerous!)"
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip confirmation prompt"
    )
):
    """Destroy infrastructure, optionally keeping resource group.
    
    By default, destroys AKS cluster and ACR but preserves the resource group.
    Use --delete-rg to also delete the resource group.
    """
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
    # Load and validate configuration
    provider_config = AzureProviderConfig.from_yaml(config)
    
    # Confirm destruction
    if not yes:
        if delete_rg:
            console.print("\n[bold red]⚠️  WARNING: Complete Destruction[/bold red]")
            console.print(f"This will destroy the ENTIRE resource group and ALL resources")
            console.print("This action cannot be undone!")
        else:
            console.print("\n[yellow]⚠️  Infrastructure Teardown[/yellow]")
            console.print(f"This will destroy {provider_config.provider} resources (AKS, ACR)")
            console.print("but will preserve the resource group for future use.")
        
        confirm = typer.confirm("\nAre you sure you want to proceed?")
        if not confirm:
            console.print("[green]Destruction cancelled[/green]")
            raise typer.Exit(0)
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("infra", env)
    project_name = StackNaming.get_project_name("infra")
    
    # Use paths.py for consistent directory management
    work_dir = ensure_work_dir("infra")
    backend_url = get_backend_url()
    
    try:
        # Always use the same program as `up`. RG has retain_on_delete=True,
        # so default destroy will keep RG; we delete it explicitly only when requested.
        def pulumi_program():
            if provider_config.provider == "azure":
                from ..infra.components.azure import ModelOpsCluster
                config_dict = provider_config.to_pulumi_config()
                config_dict["environment"] = env
                return ModelOpsCluster("modelops", config_dict)
            else:
                raise ValueError(f"Provider '{provider_config.provider}' not supported")
        
        # Use same stack configuration as 'up' command
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=ProjectBackend(url=backend_url)
                )
            )
        )
        
        console.print(f"\n[yellow]Destroying {provider_config.provider} infrastructure...[/yellow]")
        
        if delete_rg:
            # When deleting RG, we need to unprotect it first
            console.print("[dim]Note: Resource group is protected. Use --delete-rg to force deletion.[/dim]")
        
        # Destroy will fail for protected RG unless --delete-rg is used
        try:
            stack.destroy(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        except auto.CommandError as e:
            if "protected" in str(e).lower() and not delete_rg:
                console.print("\n[yellow]Resource group is protected and was not deleted.[/yellow]")
                console.print("Use --delete-rg flag to force deletion of resource group.")
            else:
                raise
        
        if delete_rg and provider_config.provider == "azure":
            # Use centralized naming to compute RG name
            import subprocess
            rg_name = StackNaming.get_resource_group_name(env, provider_config.username)
            
            console.print(f"\n[yellow]Deleting resource group '{rg_name}'...[/yellow]")
            # Use Azure CLI to delete the retained RG
            subprocess.run(["az", "group", "delete", "-n", rg_name, "--yes", "--no-wait"], check=False)
            console.print("\n[green]✓ Infrastructure destroyed; resource group deletion initiated[/green]")
        else:
            console.print("\n[green]✓ Infrastructure destroyed; resource group retained[/green]")
            console.print("Resource group preserved for future deployments")
        
    except auto.CommandError as e:
        handle_pulumi_error(e, str(work_dir), stack_name)
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"\n[red]Error destroying infrastructure: {e}[/red]")
        handle_pulumi_error(e, str(work_dir), stack_name)
        raise typer.Exit(1)


@app.command()
def status(
    env: Optional[str] = typer.Option(
        None,
        "--env", "-e",
        help="Environment name (dev, staging, prod)"
    ),
    provider: Optional[str] = typer.Option(
        None,
        "--provider", "-p",
        help="Cloud provider (azure, aws, gcp)"
    )
):
    """Show current infrastructure status from Pulumi stack."""
    import pulumi.automation as auto
    
    # Resolve defaults from config if not provided
    from .utils import resolve_env, resolve_provider
    env = resolve_env(env)
    provider = resolve_provider(provider)
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("infra", env)
    project_name = StackNaming.get_project_name("infra")
    
    # Use paths.py for consistent directory management
    work_dir = ensure_work_dir("infra")
    backend_url = get_backend_url()
    
    if not work_dir.exists():
        console.print("[yellow]No infrastructure found[/yellow]")
        console.print("\nRun 'mops infra up --config <file>' to create infrastructure")
        raise typer.Exit(0)
    
    try:
        # Need minimal program to query stack (just for outputs)
        def pulumi_program():
            pass
        
        # Get stack to query outputs
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=ProjectBackend(url=backend_url)
                )
            )
        )
        
        # Refresh stack to get current state from backend
        # Without refresh, outputs show stale/cached data
        stack.refresh(on_output=lambda _: None)
        outputs = stack.outputs()
        
        if not outputs:
            console.print("[yellow]Infrastructure stack exists but has no outputs[/yellow]")
            console.print("The infrastructure may not be fully deployed.")
            raise typer.Exit(0)
        
        console.print("\n[bold]Infrastructure Status[/bold]")
        console.print(f"  Stack: {stack_name}")
        console.print(f"  Cluster: {outputs.get('cluster_name', {}).value if outputs.get('cluster_name') else 'unknown'}")
        console.print(f"  Resource Group: {outputs.get('resource_group', {}).value if outputs.get('resource_group') else 'unknown'}")
        console.print(f"  Location: {outputs.get('location', {}).value if outputs.get('location') else 'unknown'}")
        
        if outputs.get("kubeconfig"):
            console.print(f"  [green]✓[/green] Kubeconfig available")
        else:
            console.print(f"  [red]✗[/red] Kubeconfig missing")
        
        console.print("\nQuery outputs:")
        console.print(f"  pulumi stack output --stack {stack_name} --cwd {work_dir}")
        console.print("\nNext steps:")
        console.print("  1. Run 'mops workspace up' to deploy Dask")
        console.print("  2. Run 'mops adaptive up' to start optimization")
        
    except auto.CommandError as e:
        handle_pulumi_error(e, str(work_dir), stack_name)
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error querying infrastructure status: {e}[/red]")
        handle_pulumi_error(e, str(work_dir), stack_name)
        raise typer.Exit(1)
