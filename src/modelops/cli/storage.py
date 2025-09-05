"""Storage CLI commands for blob storage management."""

import os
import typer
import yaml
from pathlib import Path
from typing import Optional
from ..core import StackNaming, automation
from ..core.paths import ensure_work_dir
from .utils import handle_pulumi_error, resolve_env
from .display import console, success, warning, error, info, section, dim, commands
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
        storage_config = yaml.safe_load(f)
    
    def pulumi_program():
        """Create BlobStorage in standalone or integrated mode."""
        from ..infra.components.storage import BlobStorage
        import pulumi
        
        # Add environment to config
        storage_config["environment"] = env
        
        if standalone:
            # Standalone deployment
            storage = BlobStorage("storage", storage_config)
        else:
            # Integrated with infrastructure - reference infra stack
            infra_ref = StackNaming.ref("infra", env)
            storage = BlobStorage("storage", storage_config, infra_stack_ref=infra_ref)
        
        # Export outputs at stack level for visibility
        pulumi.export("account_name", storage.account_name)
        pulumi.export("resource_group", storage.resource_group)
        pulumi.export("connection_string", storage.connection_string)
        pulumi.export("primary_endpoint", storage.primary_endpoint)
        pulumi.export("containers", storage_config.get("containers", []))
        pulumi.export("location", storage_config.get("location", "eastus2"))
        pulumi.export("environment", env)
        
        return storage
    
    # Ensure work directory exists
    work_dir = ensure_work_dir("storage")
    
    try:
        section(f"Provisioning blob storage")
        info(f"  Environment: {env}")
        info(f"  Mode: {'Standalone' if standalone else 'Integrated with infrastructure'}")
        
        # Extract container names properly
        containers = storage_config.get('containers', [])
        if isinstance(containers, list) and containers:
            if isinstance(containers[0], dict):
                container_names = [c.get('name', 'unnamed') for c in containers[:5]]
            else:
                container_names = containers[:5]
            info(f"  Containers: {', '.join(container_names)}\n")
        else:
            info("  Containers: (none specified)\n")
        
        warning("\nCreating storage resources...")
        
        outputs = automation.up("storage", env, None, pulumi_program, on_output=dim, work_dir=str(work_dir))
        
        success("\n✓ Storage provisioned successfully!")
        info(f"  Account: {automation.get_output_value(outputs, 'account_name', 'unknown')}")
        info(f"  Resource Group: {automation.get_output_value(outputs, 'resource_group', 'unknown')}")
        
        # Extract container names from list of dicts
        containers = automation.get_output_value(outputs, 'containers', [])
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
        handle_pulumi_error(e, str(work_dir), StackNaming.get_stack_name('storage', env))
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
    
    try:
        # Get outputs from storage stack
        outputs = automation.outputs("storage", env, refresh=False)
        
        if not outputs:
            warning("Storage not found or not deployed")
            info("Run 'mops storage up' to provision storage first")
            raise typer.Exit(1)
        
        # Get connection string and account name
        conn_str = automation.get_output_value(outputs, 'connection_string', '')
        account_name = automation.get_output_value(outputs, 'account_name', '')
        
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
    
    try:
        # Get outputs from storage stack
        work_dir = ensure_work_dir("storage")
        outputs = automation.outputs("storage", env, refresh=False, work_dir=str(work_dir))
        
        if not outputs:
            warning("Storage not found")
            info("Run 'mops storage up' to provision storage")
            raise typer.Exit(0)
        
        section("Storage Account Information")
        info(f"  Environment: {env}")
        info(f"  Account Name: {automation.get_output_value(outputs, 'account_name', 'unknown')}")
        info(f"  Resource Group: {automation.get_output_value(outputs, 'resource_group', 'unknown')}")
        info(f"  Location: {automation.get_output_value(outputs, 'location', 'unknown')}")
        info(f"  Endpoint: {automation.get_output_value(outputs, 'primary_endpoint', 'unknown')}")
        
        containers = automation.get_output_value(outputs, 'containers', [])
        if containers:
            info(f"  Containers: {', '.join(containers)}")
        
        account_name = automation.get_output_value(outputs, 'account_name', '')
        
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
    
    work_dir = ensure_work_dir("storage")
    
    try:
        warning(f"\nDestroying storage account...")
        
        automation.destroy("storage", env, None, on_output=dim, work_dir=str(work_dir))
        
        success(f"\n✓ Storage destroyed successfully")
        info("All data has been permanently deleted")
        
    except Exception as e:
        error(f"\nError destroying storage: {e}")
        handle_pulumi_error(e, str(work_dir), StackNaming.get_stack_name('storage', env))
        raise typer.Exit(1)


@app.command()
def status(
    env: Optional[str] = env_option()
):
    """Show storage stack status."""
    env = resolve_env(env)
    
    try:
        work_dir = ensure_work_dir("storage")
        outputs = automation.outputs("storage", env, refresh=True, work_dir=str(work_dir))
        
        if not outputs:
            warning("Storage not deployed")
            info("Run 'mops storage up' to provision storage")
            raise typer.Exit(0)
        
        section("Storage Status")
        info(f"  Stack: {StackNaming.get_stack_name('storage', env)}")
        info(f"  Account: {automation.get_output_value(outputs, 'account_name', 'unknown')}")
        
        containers = automation.get_output_value(outputs, 'containers', [])
        success(f"  ✓ {len(containers)} containers active")
        
        section("\nContainers:")
        for container in containers:
            info(f"  • {container}")
        
    except Exception as e:
        error(f"Error querying status: {e}")
        raise typer.Exit(1)