"""Unified initialization command for ModelOps.

This module provides the single 'mops init' command that replaces
both 'mops config init' and 'mops infra init'.
"""

import json
import subprocess
import shutil
import getpass
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

import typer
from rich.table import Table

from ..core.unified_config import (
    UnifiedModelOpsConfig,
    GeneralSettings,
    PulumiSettings,
    ClusterSpec,
    AKSSpec,
    NodePoolSpec,
    StorageSpec,
    RegistrySpec,
    WorkspaceSpec
)
from .display import console, success, error, info, warning, section


def get_azure_subscriptions() -> List[Dict[str, str]]:
    """Get list of Azure subscriptions."""
    result = subprocess.run(
        ["az", "account", "list", "--query",
         "[].{name:name, id:id, isDefault:isDefault}", "-o", "json"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    return []


def get_aks_versions(subscription_id: str, location: str) -> List[str]:
    """Get supported AKS versions for location."""
    result = subprocess.run(
        ["az", "aks", "get-versions",
         "--subscription", subscription_id,
         "--location", location,
         "--query", "values[?isPreview==null].version",
         "-o", "json"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        versions = json.loads(result.stdout)
        return sorted(versions, reverse=True)  # Latest first
    return []


def get_azure_user_email() -> Optional[str]:
    """Get the email of the currently logged-in Azure user."""
    result = subprocess.run(
        ["az", "account", "show", "--query", "user.name", "-o", "tsv"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def prompt_for_subscription(subscriptions: List[Dict[str, str]]) -> Dict[str, str]:
    """Prompt user to select an Azure subscription."""
    table = Table(title="Azure Subscriptions")
    table.add_column("#", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("ID", style="dim")
    table.add_column("Default", style="green")

    for i, sub in enumerate(subscriptions, 1):
        is_default = "✓" if sub.get('isDefault') else ""
        table.add_row(str(i), sub['name'], sub['id'], is_default)

    console.print(table)

    while True:
        try:
            choice = typer.prompt("Select subscription", type=int, default=1)
            if 1 <= choice <= len(subscriptions):
                return subscriptions[choice - 1]
            error(f"Please enter a number between 1 and {len(subscriptions)}")
        except (ValueError, KeyboardInterrupt):
            raise typer.Exit(1)


def init(
    interactive: bool = typer.Option(
        False, "--interactive", "-i",
        help="Interactive mode with prompts for all settings"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Custom output path (default: ~/.modelops/modelops.yaml)"
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Overwrite existing configuration without prompting"
    )
):
    """Initialize ModelOps with unified configuration.

    Creates a complete configuration file that combines all settings needed
    for ModelOps operation. By default uses smart defaults with minimal prompting.

    Examples:
        mops init                    # Quick setup with defaults
        mops init --interactive      # Customize all settings
        mops init --force           # Overwrite existing config
    """
    section("ModelOps Initialization")

    # Check prerequisites
    info("✓ Checking prerequisites...")
    if not shutil.which("az"):
        error("  ✗ Azure CLI not found")
        info("  Install from: https://aka.ms/azure-cli")
        raise typer.Exit(1)

    # Check Azure CLI version (optional)
    try:
        result = subprocess.run(
            ["az", "version", "--query", '"azure-cli"', "-o", "tsv"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            info(f"  • Azure CLI: found ({version})")
    except:
        info("  • Azure CLI: found")

    # Get logged-in user
    user_email = get_azure_user_email()
    if user_email:
        info(f"  • Logged in as: {user_email}")

    # Get Azure subscriptions
    info("\n✓ Detecting Azure subscriptions...")
    subscriptions = get_azure_subscriptions()
    if not subscriptions:
        error("  ✗ No Azure subscriptions found")
        info("  Please run: az login")
        raise typer.Exit(1)

    # Select subscription
    if len(subscriptions) == 1:
        subscription = subscriptions[0]
        info(f"  • {subscription['name']} ({subscription['id'][:8]}...)")
    elif interactive:
        console.print()
        subscription = prompt_for_subscription(subscriptions)
    else:
        # Use default subscription in non-interactive mode
        subscription = next((s for s in subscriptions if s.get('isDefault')), subscriptions[0])
        default_note = " (Azure CLI default)" if subscription.get('isDefault') else ""
        info(f"  • {subscription['name']} ({subscription['id'][:8]}...){default_note}")

    # Get username
    username = getpass.getuser()

    # Set defaults
    organization = "institutefordiseasemodeling"
    environment = "dev"
    provider = "azure"
    location = "eastus2"
    k8s_version = "1.30"
    worker_vm_size = "Standard_D4s_v3"
    max_workers = 20

    # Try to get actual K8s version
    try:
        versions = get_aks_versions(subscription['id'], location)
        if versions:
            k8s_version = versions[0]
    except:
        pass

    info(f"\n✓ Using defaults:")
    info(f"  • Location: {location}")
    info(f"  • Kubernetes: {k8s_version}")
    info(f"  • Username: {username}")

    # Interactive mode - allow customization
    if interactive:
        section("\nCustomization")

        # General settings
        info("General Settings:")
        organization = typer.prompt("  Organization", default=organization)
        environment = typer.prompt("  Environment", default=environment)
        username = typer.prompt("  Username", default=username)

        # Infrastructure settings
        info("\nInfrastructure Settings:")
        location = typer.prompt("  Azure location", default=location)

        # Get available K8s versions for selected location
        try:
            versions = get_aks_versions(subscription['id'], location)
            if versions:
                info(f"  Available Kubernetes versions: {', '.join(versions[:3])}")
                k8s_version = typer.prompt("  Kubernetes version", default=versions[0])
            else:
                k8s_version = typer.prompt("  Kubernetes version", default=k8s_version)
        except:
            k8s_version = typer.prompt("  Kubernetes version", default=k8s_version)

        # Worker configuration
        info("\nWorker Configuration:")
        worker_vm_size = typer.prompt("  Worker VM size", default=worker_vm_size)
        max_workers = typer.prompt("  Maximum workers", default=max_workers, type=int)

    # Build configuration
    config = UnifiedModelOpsConfig(
        generated=datetime.now(),
        settings=GeneralSettings(
            username=username,
            environment=environment,
            provider=provider
        ),
        pulumi=PulumiSettings(
            backend_url=None,  # Use default file backend
            organization=organization
        ),
        cluster=ClusterSpec(
            provider=provider,
            subscription_id=subscription['id'],
            resource_group=f"modelops-{username}",
            location=location,
            aks=AKSSpec(
                name="modelops-cluster",
                kubernetes_version=k8s_version,
                node_pools=[
                    NodePoolSpec(
                        name="system",
                        mode="System",
                        vm_size="Standard_B2s",
                        count=1
                    ),
                    NodePoolSpec(
                        name="workers",
                        mode="User",
                        vm_size=worker_vm_size,
                        min=1,
                        max=max_workers
                    )
                ]
            )
        ),
        storage=StorageSpec(),
        registry=RegistrySpec(),
        workspace=WorkspaceSpec()
    )

    # Determine output path
    output_path = output or config.get_config_path()

    # Check if file exists
    if output_path.exists() and not force:
        if not typer.confirm(f"\n{output_path} already exists. Overwrite?"):
            warning("Configuration not saved")
            raise typer.Exit(0)

    # Save configuration
    config.save() if not output else config.to_yaml(output_path)

    success(f"\n✓ Configuration saved to {output_path}")

    # Show next steps
    console.print("\n[bold green]Ready to deploy! Next steps:[/bold green]")
    console.print("  mops infra up       # Create cloud resources")
    console.print("  mops job submit     # Run your first experiment")
    console.print("")
    console.print("[dim]To view configuration: mops config show[/dim]")
    console.print("[dim]To modify settings: mops config set <key> <value>[/dim]")


def migrate(
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Output path for migrated config (default: ~/.modelops/modelops.yaml)"
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Overwrite existing unified config"
    )
):
    """Migrate from old separate config files to unified format.

    Reads existing config.yaml and infrastructure.yaml files and
    combines them into the new unified modelops.yaml format.

    Example:
        mops init migrate           # Migrate existing configs
        mops init migrate --force   # Overwrite if unified config exists
    """
    from ..core.unified_config import UnifiedModelOpsConfig

    section("Configuration Migration")

    # Check for old files
    old_config = Path.home() / ".modelops" / "config.yaml"
    old_infra = Path.home() / ".modelops" / "infrastructure.yaml"

    if not old_config.exists() and not old_infra.exists():
        warning("No existing configuration files found to migrate")
        info("Run 'mops init' to create new configuration")
        raise typer.Exit(1)

    info("Found existing configuration files:")
    if old_config.exists():
        info(f"  ✓ {old_config}")
    if old_infra.exists():
        info(f"  ✓ {old_infra}")

    # Create unified config from legacy files
    try:
        config = UnifiedModelOpsConfig.from_legacy_configs(
            config_path=old_config if old_config.exists() else None,
            infra_path=old_infra if old_infra.exists() else None
        )
    except Exception as e:
        error(f"Failed to migrate configuration: {e}")
        raise typer.Exit(1)

    # Determine output path
    output_path = output or config.get_config_path()

    # Check if unified config already exists
    if output_path.exists() and not force:
        if not typer.confirm(f"\n{output_path} already exists. Overwrite?"):
            warning("Migration cancelled")
            raise typer.Exit(0)

    # Save unified configuration
    config.save() if not output else config.to_yaml(output_path)

    success(f"\n✓ Configuration migrated to {output_path}")
    info("\nOld configuration files have been preserved.")
    info("You can safely delete them once you've verified the migration:")
    if old_config.exists():
        info(f"  rm {old_config}")
    if old_infra.exists():
        info(f"  rm {old_infra}")