"""Unified infrastructure management CLI.

Orchestrates all infrastructure components with a single command.
"""

import typer
from pathlib import Path
from typing import Optional, List

from ..client import InfrastructureService
from ..components.specs.infra import UnifiedInfraSpec
from .display import console, success, error, info, section, warning
from .common_options import env_option, yes_option

app = typer.Typer(help="Unified infrastructure management")


@app.command()
def up(
    config: Path = typer.Argument(
        ...,
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
        mops infra up infrastructure.yaml
        mops infra up infrastructure.yaml --components storage,workspace
        mops infra up infrastructure.yaml --plan
    """
    from .utils import resolve_env

    env = resolve_env(env)

    # Load spec
    try:
        spec = UnifiedInfraSpec.from_yaml(str(config))
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

        warning(f"This will destroy: {' and '.join(warning_msg)}")

        if not typer.confirm("Continue?"):
            success("Cancelled")
            raise typer.Exit(0)

    result = service.destroy(
        components, verbose, force, with_deps,
        destroy_storage=destroy_storage,
        destroy_registry=destroy_registry,
        destroy_all=destroy_all
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
            symbol = "✓" if component_status.deployed else "✗"

            console.print(f"  {symbol} {component}: {state}")

            if component_status.deployed and component_status.details:
                # Show key details
                details = component_status.details

                if component == "cluster":
                    if "cluster_name" in details:
                        console.print(f"      cluster: {details['cluster_name']}")
                    if "connectivity" in details:
                        conn_status = "connected" if details["connectivity"] else "unreachable"
                        console.print(f"      status: {conn_status}")

                elif component == "storage":
                    if "account_name" in details:
                        console.print(f"      account: {details['account_name']}")
                    if "container_count" in details:
                        console.print(f"      containers: {details['container_count']}")

                elif component == "workspace":
                    if "workers" in details:
                        console.print(f"      workers: {details['workers']}")
                    if "autoscaling" in details and details["autoscaling"]:
                        console.print(f"      autoscaling: enabled")

                elif component == "registry":
                    if "registry_name" in details:
                        console.print(f"      name: {details['registry_name']}")

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
