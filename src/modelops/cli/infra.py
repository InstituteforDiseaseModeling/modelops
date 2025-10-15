"""Unified infrastructure management CLI.

Orchestrates all infrastructure components with a single command.
"""

import json
import subprocess
import shutil
import typer
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

from ..client import InfrastructureService
from ..core.paths import INFRASTRUCTURE_FILE
from ..components.specs.infra import UnifiedInfraSpec
from .display import console, success, error, info, section, warning
from .common_options import env_option, yes_option
from .templates import get_infra_template

app = typer.Typer(help="Unified infrastructure management")


@app.command(hidden=True)  # Hidden: use 'mops init' instead
def init(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Custom output path"),
    interactive: bool = typer.Option(True, "--interactive/--non-interactive", help="Interactive mode"),
):
    """Generate infrastructure configuration with guided setup.

    Creates a ready-to-use infrastructure configuration file with your Azure
    subscription and sensible defaults. By default saves to ~/.modelops/infrastructure.yaml
    which will be used automatically by 'mops infra up'.

    Example:
        mops infra init                    # Interactive mode
        mops infra init --non-interactive  # Use defaults
        mops infra init --output custom.yaml  # Custom location
    """
    # Default to INFRASTRUCTURE_FILE constant
    if output is None:
        output = INFRASTRUCTURE_FILE
        output.parent.mkdir(parents=True, exist_ok=True)
    else:
        output = Path(output)

    # Check Azure CLI
    if not shutil.which("az"):
        error("Azure CLI not found. Install: https://aka.ms/azure-cli")
        raise typer.Exit(1)

    # Get subscriptions
    subs = get_azure_subscriptions()
    if not subs:
        error("No Azure subscriptions found. Run: az login")
        raise typer.Exit(1)

    # Select subscription
    if len(subs) == 1:
        subscription = subs[0]
        info(f"Using subscription: {subscription['name']}")
    elif interactive:
        from rich.table import Table
        table = Table(title="Azure Subscriptions")
        table.add_column("#", style="cyan")
        table.add_column("Name", style="bold")
        table.add_column("ID", style="dim")

        for i, sub in enumerate(subs, 1):
            table.add_row(str(i), sub['name'], sub['id'])

        console.print(table)
        choice = typer.prompt("Select subscription", type=int, default=1)
        subscription = subs[choice - 1]
    else:
        # Non-interactive: use default subscription
        subscription = next((s for s in subs if s.get('isDefault')), subs[0])
        info(f"Using subscription: {subscription['name']}")

    # Select location
    if interactive:
        location = typer.prompt("Azure location", default="eastus2")
    else:
        location = "eastus2"

    # Get AKS version (with fallback)
    try:
        versions = get_aks_versions(subscription['id'], location)
        k8s_version = versions[0] if versions else "1.30"
        info(f"Using Kubernetes {k8s_version} (latest in {location})")
    except Exception:
        k8s_version = "1.30"
        warning(f"Could not fetch AKS versions, using {k8s_version}")

    # Get username from config (or system)
    try:
        from ..core.config import get_username
        username = get_username()
    except Exception:
        import getpass
        username = getpass.getuser()

    # Generate YAML from template
    yaml_content = get_infra_template(
        subscription_id=subscription['id'],
        username=username,
        location=location,
        k8s_version=k8s_version
    )

    # Write file
    output.write_text(yaml_content)
    success(f"Created infrastructure config: {output}")

    # Next steps
    console.print("\n[bold green]Next steps:[/bold green]")
    if output == Path.home() / ".modelops" / "infrastructure.yaml":
        console.print("  mops infra up              # Deploy infrastructure")
    else:
        console.print(f"  mops infra up {output}  # Deploy infrastructure")
    console.print("  mops infra status           # Check status")


@app.command()
def up(
    config: Optional[Path] = typer.Argument(
        None,
        help="Infrastructure configuration file (YAML)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True
    ),
    components: Optional[List[str]] = typer.Option(
        None,
        "--components", "-c",
        help="Specific components to provision (registry,cluster,storage,workspace)"
    ),
    env: Optional[str] = env_option(),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed output"),
    force: bool = typer.Option(False, "--force", "-f", help="Force reprovisioning"),
    plan: bool = typer.Option(False, "--plan", help="Show what would be done without doing it"),
    json_output: bool = typer.Option(False, "--json", help="Output in JSON format")
):
    """
    Provision infrastructure from unified configuration.

    This orchestrates cluster, storage, workspace provisioning in the correct order.

    Example:
        mops infra up                      # Uses ~/.modelops/infrastructure.yaml
        mops infra up infrastructure.yaml   # Custom config
        mops infra up --components storage,workspace
        mops infra up --plan
    """
    from .utils import resolve_env

    env = resolve_env(env)

    # Smart default: look for unified config first, then infrastructure.yaml
    if config is None:
        from ..core.paths import UNIFIED_CONFIG_FILE
        if UNIFIED_CONFIG_FILE.exists():
            config = UNIFIED_CONFIG_FILE
            info(f"Using unified config: {config}")
        elif INFRASTRUCTURE_FILE.exists():
            config = INFRASTRUCTURE_FILE
            info(f"Using legacy config: {config}")
        else:
            error("No configuration found")
            error("Run 'mops init' to create configuration")
            error("Or specify config: mops infra up <config.yaml>")
            raise typer.Exit(1)

    # Validate subscription before expensive operations
    try:
        # Check if this is unified config or legacy infra config
        from ..core.paths import UNIFIED_CONFIG_FILE
        if config == UNIFIED_CONFIG_FILE or str(config).endswith('modelops.yaml'):
            # Load unified config and convert to infra spec
            from ..core.unified_config import UnifiedModelOpsConfig
            unified = UnifiedModelOpsConfig.from_yaml(config)

            # Convert to legacy infra spec format
            from ..components.specs.azure import NodePool, AKSConfig, AzureProviderConfig
            from ..components.specs.storage import StorageConfig
            from ..components.specs.workspace import WorkspaceConfig

            # Convert node pools
            node_pools = []
            for pool in unified.cluster.aks.node_pools:
                # NodePool expects EITHER count OR min/max, not both
                pool_config = {
                    "name": pool.name,
                    "mode": pool.mode,
                    "vm_size": pool.vm_size
                }

                # Add sizing configuration - either fixed or autoscaling
                if pool.count is not None:
                    pool_config["count"] = pool.count
                elif pool.min is not None and pool.max is not None:
                    pool_config["min"] = pool.min
                    pool_config["max"] = pool.max
                else:
                    # Default to count=1 for safety
                    pool_config["count"] = 1

                node_pools.append(NodePool(**pool_config))

            # Build legacy cluster spec
            cluster_spec = AzureProviderConfig(
                provider=unified.cluster.provider,
                subscription_id=unified.cluster.subscription_id,
                resource_group=unified.cluster.resource_group,
                location=unified.cluster.location,
                aks=AKSConfig(
                    name=unified.cluster.aks.name,
                    kubernetes_version=unified.cluster.aks.kubernetes_version,
                    node_pools=node_pools
                )
            )

            # Build workspace spec
            workspace_spec = WorkspaceConfig(
                apiVersion="modelops/v1",
                kind="Workspace",
                metadata={"name": "main-workspace"},
                spec={
                    "scheduler": {
                        "image": unified.workspace.scheduler_image,
                        "replicas": unified.workspace.scheduler_replicas
                    },
                    "workers": {
                        "image": unified.workspace.worker_image,
                        "replicas": unified.workspace.worker_replicas,
                        "processes": unified.workspace.worker_processes,
                        "threads": unified.workspace.worker_threads
                    }
                }
            )

            # Create unified infra spec
            spec = UnifiedInfraSpec(
                schemaVersion=1,
                cluster=cluster_spec,
                storage=StorageConfig(account_tier=unified.storage.account_tier),
                registry={"sku": unified.registry.sku},  # Registry is just a dict
                workspace=workspace_spec
            )
        else:
            # Load legacy infrastructure.yaml format
            spec = UnifiedInfraSpec.from_yaml(str(config))

        # Check for placeholder subscription IDs
        if spec.cluster and spec.cluster.subscription_id:
            if spec.cluster.subscription_id in ["YOUR_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000"]:
                error("Invalid subscription ID in configuration")
                error("Run 'mops infra init' to regenerate with valid subscription")
                raise typer.Exit(1)

            # Verify subscription is accessible
            if not verify_subscription(spec.cluster.subscription_id):
                error(f"Cannot access subscription {spec.cluster.subscription_id[:8]}...")
                error("Verify you're logged in: az login")
                error("List available subscriptions: az account list --output table")
                raise typer.Exit(1)
    except Exception as e:
        error(f"Failed to load configuration: {e}")
        raise typer.Exit(1)

    # Create service
    service = InfrastructureService(env)

    if plan:
        # Preview mode
        section(f"Infrastructure plan for environment: {env}")
        preview = service.preview(spec, components)

        if json_output:
            import json
            console.print(json.dumps(preview, indent=2))
        else:
            if preview["to_create"]:
                info("Components to create:")
                for comp in preview["to_create"]:
                    info(f"  + {comp}")
            if preview["to_update"]:
                info("Components to update:")
                for comp in preview["to_update"]:
                    info(f"  ~ {comp}")
            if preview["no_change"]:
                info("Components unchanged:")
                for comp in preview["no_change"]:
                    info(f"  = {comp}")

        raise typer.Exit(0)

    # Handle comma-separated components
    if components and len(components) == 1 and "," in components[0]:
        components = [c.strip() for c in components[0].split(",")]

    section(f"Provisioning infrastructure for environment: {env}")
    info(f"Components: {', '.join(components or spec.get_components())}")

    result = service.provision(spec, components, verbose, force)

    if json_output:
        console.print(result.to_json())
    else:
        if result.success:
            success("\n✓ Infrastructure provisioned successfully!")

            # Show key outputs
            if "cluster" in result.outputs:
                cluster_outputs = result.outputs.get("cluster", {})
                cluster_name = cluster_outputs.get("cluster_name")
                if cluster_name:
                    info(f"\nCluster: {cluster_name}")

            if "storage" in result.outputs:
                storage_outputs = result.outputs.get("storage", {})
                account_name = storage_outputs.get("account_name")
                if account_name:
                    info(f"Storage: {account_name}")

            if "workspace" in result.outputs:
                workspace_outputs = result.outputs.get("workspace", {})
                dashboard_url = workspace_outputs.get("dashboard_url")
                if dashboard_url:
                    info(f"Dask Dashboard: {dashboard_url}")

            if result.logs_path:
                info(f"\nLogs: {result.logs_path}")

            # Note: BundleEnvironment reconciliation happens in InfrastructureService
        else:
            error("\n✗ Some components failed to provision")
            for comp, err in result.errors.items():
                error(f"  {comp}: {err}")
            raise typer.Exit(1)


@app.command()
def down(
    components: Optional[List[str]] = typer.Option(
        None,
        "--components", "-c",
        help="Specific components to destroy"
    ),
    env: Optional[str] = env_option(),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed output"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip dependency checks"),
    with_deps: bool = typer.Option(False, "--with-deps", help="Also destroy dependent components"),
    destroy_storage: bool = typer.Option(False, "--destroy-storage", help="Include storage (contains results/artifacts)"),
    destroy_registry: bool = typer.Option(False, "--destroy-registry", help="Include registry (contains images)"),
    destroy_all: bool = typer.Option(False, "--destroy-all", help="Destroy all components including data"),
    delete_rg: bool = typer.Option(False, "--delete-rg", help="Also delete the resource group (dangerous!)"),
    plan: bool = typer.Option(False, "--plan", help="Show what would be done"),
    yes: bool = yes_option(),
    json_output: bool = typer.Option(False, "--json", help="Output in JSON format")
):
    """
    Destroy infrastructure components.

    By default, only destroys compute resources (cluster, workspace).
    Use flags to include data resources (storage, registry).

    Example:
        mops infra down                           # Destroy compute only
        mops infra down --destroy-storage         # Include storage
        mops infra down --destroy-all             # Destroy everything
        mops infra down --components workspace    # Specific components
    """
    from .utils import resolve_env

    env = resolve_env(env)

    # Handle comma-separated components
    if components and len(components) == 1 and "," in components[0]:
        components = [c.strip() for c in components[0].split(",")]

    service = InfrastructureService(env)

    if plan:
        # Just show what would be done
        result = service.destroy(
            components, verbose, force, with_deps,
            dry_run=True,
            destroy_storage=destroy_storage,
            destroy_registry=destroy_registry,
            destroy_all=destroy_all
        )
        raise typer.Exit(0)

    # Build warning message based on what will be destroyed
    if not yes:
        warning_msg = []
        if components:
            warning_msg.append(f"Components: {', '.join(components)}")
        else:
            # Show what default behavior will destroy
            if destroy_all:
                warning_msg.append("ALL infrastructure including data resources")
            else:
                default_components = ["cluster", "workspace"]
                if destroy_storage:
                    default_components.append("storage")
                if destroy_registry:
                    default_components.append("registry")
                warning_msg.append(f"Components: {', '.join(default_components)}")

        if delete_rg:
            warning_msg.append("RESOURCE GROUP (complete deletion)")

        warning(f"This will destroy: {' and '.join(warning_msg)}")

        if delete_rg:
            error("\n⚠️  WARNING: Resource Group Deletion")
            info("This will delete the ENTIRE resource group and ALL resources within it.")
            info("This action cannot be undone!")

        if not typer.confirm("Continue?"):
            success("Cancelled")
            raise typer.Exit(0)

    result = service.destroy(
        components, verbose, force, with_deps,
        destroy_storage=destroy_storage,
        destroy_registry=destroy_registry,
        destroy_all=destroy_all,
        delete_rg=delete_rg,
        yes_confirmed=yes
    )

    if json_output:
        console.print(result.to_json())
    else:
        if result.success:
            success("\n✓ Infrastructure destroyed")
        else:
            error("\n✗ Some components failed to destroy")
            for comp, err in result.errors.items():
                error(f"  {comp}: {err}")
            raise typer.Exit(1)


@app.command()
def status(
    env: Optional[str] = env_option(),
    detailed: bool = typer.Option(False, "--detailed", "-d", help="Show detailed operational information"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output in JSON format"),
    show_outputs: bool = typer.Option(False, "--outputs", "-o", help="Show stack outputs")
):
    """
    Show infrastructure status.

    Displays the status of all infrastructure components.
    Use --detailed to see connection commands and operational info.

    Example:
        mops infra status                # Basic overview
        mops infra status --detailed      # Full operational details
        mops infra status --json
        mops infra status --outputs
    """
    from .utils import resolve_env

    env = resolve_env(env)

    service = InfrastructureService(env)
    status = service.get_status()

    if json_output:
        import json
        status_dict = {k: v.to_json() for k, v in status.items()}
        console.print(json.dumps(status_dict, indent=2))
    else:
        section(f"Infrastructure Status ({env})")

        for component, component_status in status.items():
            state = component_status.phase.value

            # Choose color and symbol based on status
            if component_status.deployed:
                if component_status.phase.value == "ready":
                    # Green for ready
                    console.print(f"  [green]✓[/green] {component}: [green]{state}[/green]")
                elif component_status.phase.value == "provisioning":
                    # Yellow for provisioning
                    console.print(f"  [yellow]⟳[/yellow] {component}: [yellow]{state}[/yellow]")
                else:
                    # Default deployed but unknown state
                    console.print(f"  [yellow]✓[/yellow] {component}: [yellow]{state}[/yellow]")
            else:
                if component_status.phase.value == "failed":
                    # Red for failed
                    console.print(f"  [red]✗[/red] {component}: [red]{state}[/red]")
                else:
                    # Gray for not deployed
                    console.print(f"  [dim]✗[/dim] {component}: [dim]{state}[/dim]")

            if component_status.deployed and component_status.details:
                # Show key details
                details = component_status.details

                if component == "resource_group":
                    if "resource_group_name" in details:
                        console.print(f"      [dim]name:[/dim] {details['resource_group_name']}")
                    if "location" in details:
                        console.print(f"      [dim]location:[/dim] {details['location']}")

                elif component == "cluster":
                    if "cluster_name" in details:
                        console.print(f"      [dim]cluster:[/dim] {details['cluster_name']}")
                    if "resource_group" in details:
                        console.print(f"      [dim]resource group:[/dim] {details['resource_group']}")
                    if "connectivity" in details:
                        if details["connectivity"]:
                            console.print(f"      [dim]status:[/dim] [green]connected[/green]")
                        else:
                            console.print(f"      [dim]status:[/dim] [red]unreachable[/red]")

                elif component == "storage":
                    if "account_name" in details:
                        console.print(f"      [dim]account:[/dim] {details['account_name']}")
                    if "container_count" in details:
                        console.print(f"      [dim]containers:[/dim] {details['container_count']}")

                elif component == "workspace":
                    if "workers" in details:
                        worker_count = details['workers']
                        # Convert to int if it's a string
                        if isinstance(worker_count, str):
                            try:
                                worker_count = int(worker_count)
                            except (ValueError, TypeError):
                                worker_count = 0
                        if worker_count > 0:
                            console.print(f"      [dim]workers:[/dim] [green]{worker_count}[/green]")
                        else:
                            console.print(f"      [dim]workers:[/dim] [yellow]{worker_count}[/yellow]")
                    if "autoscaling" in details and details["autoscaling"]:
                        console.print(f"      [dim]autoscaling:[/dim] [green]enabled[/green]")

                elif component == "registry":
                    if "registry_name" in details:
                        console.print(f"      [dim]name:[/dim] {details['registry_name']}")

        # Show detailed operational information
        if detailed:
            section("\n=== Operational Details ===")

            # Get outputs for connection details
            outputs = service.get_outputs()

            # Workspace details
            if "workspace" in status and status["workspace"].deployed:
                info("\n[bold]Workspace (Dask):[/bold]")
                namespace = outputs.get("workspace", {}).get("namespace", "modelops-dask-dev")
                info(f"  Namespace: {namespace}")
                info("\n  Port-forward commands:")
                info(f"    kubectl port-forward -n {namespace} svc/dask-scheduler 8786:8786")
                info(f"    kubectl port-forward -n {namespace} svc/dask-scheduler 8787:8787")
                info("\n  Access URLs (after port-forwarding):")
                info("    Scheduler: tcp://localhost:8786")
                info("    Dashboard: http://localhost:8787")
                info("\n  Monitoring:")
                info(f"    Logs: kubectl logs -n {namespace} -l app=dask-scheduler")
                info(f"    Workers: kubectl get pods -n {namespace} -l app=dask-worker")

            # Registry details
            if "registry" in status and status["registry"].deployed:
                registry_details = status["registry"].details
                registry_name = registry_details.get("registry_name", "")
                login_server = registry_details.get("login_server", "")

                if registry_name:
                    info("\n[bold]Registry (ACR):[/bold]")
                    info(f"  Login server: {login_server}")
                    info("\n  Commands:")
                    info(f"    Login: az acr login --name {registry_name}")
                    info(f"    Push: docker push {login_server}/image:tag")
                    info(f"    List: az acr repository list --name {registry_name}")

            # Storage details
            if "storage" in status and status["storage"].deployed:
                storage_details = status["storage"].details
                account_name = storage_details.get("account_name", "")

                if account_name:
                    info("\n[bold]Storage (Blob):[/bold]")
                    info(f"  Account: {account_name}")
                    info("\n  Setup connection:")
                    info("    mops storage connection-string > ~/.modelops/storage.env")
                    info("    source ~/.modelops/storage.env")
                    info("\n  Containers:")
                    containers = storage_details.get("containers", [])
                    if isinstance(containers, list):
                        for container in containers[:5]:  # Show first 5
                            if isinstance(container, dict):
                                info(f"    • {container.get('name', 'unnamed')}")
                            else:
                                info(f"    • {container}")

            # Cluster details
            if "cluster" in status and status["cluster"].deployed:
                cluster_details = status["cluster"].details
                cluster_name = cluster_details.get("cluster_name", "")

                if cluster_name:
                    info("\n[bold]Cluster (AKS):[/bold]")
                    info(f"  Name: {cluster_name}")
                    info("\n  Kubeconfig:")
                    info("    mops cluster kubeconfig --merge")
                    info(f"    kubectl config use-context {cluster_name}")

        if show_outputs:
            section("\nOutputs")
            outputs = service.get_outputs()
            for comp, comp_outputs in outputs.items():
                if comp_outputs:
                    info(f"\n{comp}:")
                    for key, value in comp_outputs.items():
                        # Skip large values
                        if isinstance(value, str) and len(value) > 100:
                            info(f"  {key}: <truncated>")
                        else:
                            info(f"  {key}: {value}")


@app.command()
def outputs(
    component: Optional[str] = typer.Argument(
        None,
        help="Specific component to get outputs for"
    ),
    env: Optional[str] = env_option(),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output in JSON format"),
    show_secrets: bool = typer.Option(False, "--show-secrets", help="Show secret values")
):
    """
    Get infrastructure outputs.

    Retrieves outputs from infrastructure stacks.

    Example:
        mops infra outputs
        mops infra outputs cluster
        mops infra outputs --json
    """
    from .utils import resolve_env

    env = resolve_env(env)

    service = InfrastructureService(env)
    outputs = service.get_outputs(component, show_secrets)

    if json_output:
        import json
        console.print(json.dumps(outputs, indent=2, default=str))
    else:
        if not outputs:
            warning("No outputs found")
            raise typer.Exit(0)

        if component:
            section(f"Outputs for {component}")
            for key, value in outputs.items():
                if isinstance(value, str) and len(value) > 100:
                    info(f"{key}: <truncated>")
                else:
                    info(f"{key}: {value}")
        else:
            section("Infrastructure Outputs")
            for comp, comp_outputs in outputs.items():
                if comp_outputs:
                    info(f"\n{comp}:")
                    for key, value in comp_outputs.items():
                        if isinstance(value, str) and len(value) > 100:
                            info(f"  {key}: <truncated>")
                        else:
                            info(f"  {key}: {value}")


# Helper functions for infra init
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


def verify_subscription(subscription_id: str) -> bool:
    """Check if subscription is accessible."""
    result = subprocess.run(
        ["az", "account", "show", "--subscription", subscription_id],
        capture_output=True
    )
    return result.returncode == 0


