"""Storage CLI commands for blob storage management."""

import os
import typer
import yaml
from pathlib import Path
from typing import Optional
from ..client import StorageService
from ..core import StackNaming
from .utils import resolve_env, handle_pulumi_error
from .display import console, success, warning, error, info, section, commands
from .common_options import env_option, yes_option

app = typer.Typer(help="Manage blob storage for bundles and results")


@app.command()
def up(
    config: Path = typer.Argument(
        ...,
        help="Storage configuration file (YAML)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True
    ),
    env: Optional[str] = env_option(),
    standalone: bool = typer.Option(
        False,
        "--standalone",
        help="Deploy as standalone stack (not integrated with infra)"
    )
):
    """Provision blob storage account and containers.
    
    Creates Azure storage account with containers for bundles,
    results, workspace scratch, and task definitions.
    
    Example:
        mops storage up examples/storage.yaml
        mops storage up examples/storage.yaml --env prod
    """
    env = resolve_env(env)
    
    # Load configuration
    with open(config) as f:
        config_dict = yaml.safe_load(f)

    # Add environment to config
    config_dict["environment"] = env

    # Create StorageConfig from dict
    from ..components.specs.storage import StorageConfig
    storage_config = StorageConfig(**config_dict)

    # Use StorageService
    service = StorageService(env)

    try:
        section(f"Provisioning blob storage")
        info(f"  Environment: {env}")
        info(f"  Mode: {'Standalone' if standalone else 'Integrated with infrastructure'}")

        # Extract container names properly
        containers = config_dict.get('containers', [])
        if isinstance(containers, list) and containers:
            if isinstance(containers[0], dict):
                container_names = [c.get('name', 'unnamed') for c in containers[:5]]
            else:
                container_names = containers[:5]
            info(f"  Containers: {', '.join(container_names)}\n")
        else:
            info("  Containers: (none specified)\n")

        warning("\nCreating storage resources...")

        outputs = service.provision(storage_config, standalone, verbose=False)

        success("\n✓ Storage provisioned successfully!")
        info(f"  Account: {outputs.get('account_name', 'unknown')}")
        info(f"  Resource Group: {outputs.get('resource_group', 'unknown')}")
        
        # Extract container names from list of dicts
        containers = outputs.get('containers', [])
        if containers:
            container_names = [c.get('name', 'unnamed') if isinstance(c, dict) else str(c) for c in containers]
            info(f"  Containers: {', '.join(container_names)}")
        
        section("\nNext steps:")
        info("1. Get connection string for workstation access:")
        commands([("Setup", "mops storage connection-string > ~/.modelops/storage.env")])
        info("\n2. Source environment file before using storage:")
        commands([("Use", "source ~/.modelops/storage.env")])
        
    except Exception as e:
        error(f"\nError provisioning storage: {e}")
        handle_pulumi_error(e, "~/.modelops/pulumi/storage", StackNaming.get_stack_name('storage', env))
        raise typer.Exit(1)


@app.command(name="connection-string")
def connection_string(
    env: Optional[str] = env_option(),
    output_file: Optional[Path] = typer.Option(
        None,
        "--output", "-o",
        help="Output file (default: stdout)"
    )
):
    """Export storage connection string for workstation access.
    
    Outputs shell environment variables that can be sourced
    to enable local access to the storage account.
    
    Example:
        mops storage connection-string > ~/.modelops/storage.env
        source ~/.modelops/storage.env
    """
    env = resolve_env(env)
    
    # Use StorageService
    service = StorageService(env)

    try:
        conn_str = service.get_connection_string(show_secrets=True)
        info = service.get_info()
        account_name = info.get('account_name', 'unknown')

        if not conn_str:
            error("Connection string not found in stack outputs")
            raise typer.Exit(1)
        
        # Format as shell exports
        export_content = f"""# ModelOps Storage Configuration
# Generated for environment: {env}
export AZURE_STORAGE_CONNECTION_STRING="{conn_str}"
export AZURE_STORAGE_ACCOUNT="{account_name}"
export MODELOPS_STORAGE_ENV="{env}"
"""
        
        if output_file:
            # Ensure directory exists
            output_file.parent.mkdir(parents=True, exist_ok=True)
            # Write with restricted permissions (user-only)
            output_file.write_text(export_content)
            os.chmod(output_file, 0o600)  # User read/write only
            success(f"✓ Connection string saved to: {output_file}")
            info(f"\nTo use: source {output_file}")
        else:
            # Output to stdout for piping
            print(export_content)
        
    except Exception as e:
        error(f"Error getting connection string: {e}")
        raise typer.Exit(1)


@app.command(name="info")
def storage_info(
    env: Optional[str] = env_option()
):
    """Show storage account information.
    
    Displays storage account details including name, containers,
    and example access commands.
    """
    env = resolve_env(env)
    
    # Use StorageService
    service = StorageService(env)

    try:
        status = service.status()

        if not status.deployed:
            warning("Storage not found")
            info("Run 'mops storage up' to provision storage")
            raise typer.Exit(0)

        section("Storage Account Information")
        info(f"  Environment: {env}")
        info(f"  Account Name: {status.details.get('account_name', 'unknown')}")
        info(f"  Resource Group: {status.details.get('resource_group', 'unknown')}")
        info(f"  Location: {status.details.get('location', 'unknown')}")
        info(f"  Endpoint: {status.details.get('primary_endpoint', 'unknown')}")

        containers = status.details.get('containers', [])
        if containers:
            if isinstance(containers, int):
                info(f"  Containers: {containers} active")
            else:
                info(f"  Containers: {', '.join(str(c) for c in containers)}")

        account_name = status.details.get('account_name', '')
        
        section("\nAccess Methods:")
        
        info("1. From workstation (after setting up connection string):")
        commands([
            ("Setup", "mops storage connection-string > ~/.modelops/storage.env"),
            ("Use", "source ~/.modelops/storage.env")
        ])
        
        info("\n2. Using Azure CLI:")
        commands([
            ("List", f"az storage blob list --account-name {account_name} --container-name bundles"),
            ("Upload", f"az storage blob upload --account-name {account_name} --container-name bundles --file myfile")
        ])
        
        info("\n3. From Python:")
        print("   ```python")
        print("   from azure.storage.blob import BlobServiceClient")
        print("   client = BlobServiceClient.from_connection_string(")
        print('       os.environ["AZURE_STORAGE_CONNECTION_STRING"]')
        print("   )")
        print("   ```")
        
    except Exception as e:
        error(f"Error querying storage: {e}")
        raise typer.Exit(1)


@app.command()
def down(
    env: Optional[str] = env_option(),
    yes: bool = yes_option()
):
    """Destroy blob storage account and all containers.
    
    WARNING: This permanently deletes all data in the storage account!
    """
    env = resolve_env(env)
    
    if not yes:
        warning("\n⚠️  Warning")
        info("This will permanently delete the storage account and ALL data within it:")
        info("  - All bundles and artifacts")
        info("  - All experiment results")
        info("  - All workspace scratch data")
        info("  - All task definitions")
        
        confirm = typer.confirm("\nAre you sure you want to destroy storage?")
        if not confirm:
            success("Destruction cancelled")
            raise typer.Exit(0)
    
    # Use StorageService
    service = StorageService(env)

    try:
        warning(f"\nDestroying storage account...")

        service.destroy(verbose=False)

        success(f"\n✓ Storage destroyed successfully")
        info("All data has been permanently deleted")
        
    except Exception as e:
        error(f"\nError destroying storage: {e}")
        handle_pulumi_error(e, "~/.modelops/pulumi/storage", StackNaming.get_stack_name('storage', env))
        raise typer.Exit(1)


@app.command()
def status(
    env: Optional[str] = env_option()
):
    """Show storage stack status."""
    env = resolve_env(env)
    
    # Use StorageService
    service = StorageService(env)

    try:
        status = service.status()

        if not status.deployed:
            warning("Storage not deployed")
            info("Run 'mops storage up' to provision storage")
            raise typer.Exit(0)
        
        section("Storage Status")
        info(f"  Stack: {StackNaming.get_stack_name('storage', env)}")
        info(f"  Account: {status.details.get('account_name', 'unknown')}")

        containers = status.details.get('containers', [])
        if isinstance(containers, int):
            success(f"  ✓ {containers} containers active")
        else:
            success(f"  ✓ {len(containers)} containers active")

        section("\nContainers:")
        if isinstance(containers, list):
            for container in containers:
                info(f"  • {container}")
        
    except Exception as e:
        error(f"Error querying status: {e}")
        raise typer.Exit(1)