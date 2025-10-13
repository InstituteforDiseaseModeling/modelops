"""Consolidated status command for all ModelOps infrastructure."""

import typer
import pulumi.automation as auto
from pathlib import Path
from typing import Optional, Dict, Any, List
from rich.table import Table
from ..core import StackNaming, automation
from ..core.paths import ensure_work_dir, BACKEND_DIR, WORK_DIRS
from .utils import handle_pulumi_error, resolve_env
from .display import console, success, warning, error, info, section, dim, commands
from .common_options import env_option

app = typer.Typer(help="Infrastructure status and health checks")


def get_all_stacks(env: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Get all stacks across all components.
    
    Args:
        env: Optional environment filter
        
    Returns:
        Dictionary mapping component name to list of stack info
    """
    all_stacks = {}
    
    for component in WORK_DIRS.keys():
        try:
            work_dir = ensure_work_dir(component)
            project_name = StackNaming.get_project_name(component)
            
            # Skip if directory doesn't have Pulumi.yaml
            if not (work_dir / "Pulumi.yaml").exists():
                continue
            
            # Create workspace to list stacks
            from ..core.automation import workspace_options
            ws = auto.LocalWorkspace(
                **workspace_options(project_name, work_dir).__dict__
            )
            
            stacks = ws.list_stacks()
            if stacks:
                stack_infos = []
                for s in stacks:
                    # Parse stack name
                    try:
                        parsed = StackNaming.parse_stack_name(s.name)
                        stack_env = parsed.get("env", "unknown")
                        
                        # Filter by environment if specified
                        if env and stack_env != env:
                            continue
                        
                        # Get basic info
                        info_dict = {
                            "name": s.name,
                            "env": stack_env,
                            "component": component,
                            "status": "Unknown"
                        }
                        
                        # Try to get outputs for more details
                        try:
                            # For adaptive, handle named infrastructures
                            if component == "adaptive" and parsed.get("run_id"):
                                outputs = automation.outputs(
                                    component, stack_env, parsed["run_id"],
                                    refresh=False, work_dir=str(work_dir / parsed["run_id"])
                                )
                            else:
                                outputs = automation.outputs(
                                    component, stack_env, refresh=False, work_dir=str(work_dir)
                                )
                            
                            if outputs:
                                info_dict["status"] = "✓ Deployed"
                                info_dict["outputs"] = outputs
                            else:
                                info_dict["status"] = "⚠ Not deployed"
                        except:
                            # Keep Unknown status if we can't get outputs
                            pass
                        
                        stack_infos.append(info_dict)
                    except:
                        # If we can't parse the stack name, skip it
                        continue
                
                if stack_infos:
                    all_stacks[component] = stack_infos
        except Exception:
            # Skip components that have issues
            continue
    
    return all_stacks


@app.command(name="all")
def status_all(
    env: Optional[str] = env_option(),
    smoke_test: bool = typer.Option(
        False,
        "--smoke-test",
        help="Run smoke tests for connectivity validation"
    )
):
    """Show status of all ModelOps infrastructure.
    
    Displays a comprehensive view of all deployed components including
    infrastructure, storage, workspaces, and adaptive runs.
    """
    env_filter = resolve_env(env) if env else None
    
    # Check if backend exists
    if not BACKEND_DIR.exists():
        warning("No ModelOps infrastructure found")
        info("\nGet started with: mops infra up --config ~/.modelops/providers/azure.yaml")
        return
    
    # Get all stacks
    all_stacks = get_all_stacks(env_filter)
    
    if not all_stacks:
        warning("No deployed infrastructure found")
        if env_filter:
            info(f"No stacks found for environment: {env_filter}")
        return
    
    # Display header
    console.print("\n[bold cyan]ModelOps Infrastructure Status[/bold cyan]")
    if env_filter:
        console.print(f"[dim]Environment filter: {env_filter}[/dim]\n")
    
    # Display each component
    component_order = ["infra", "storage", "registry", "workspace", "adaptive"]
    
    for component in component_order:
        if component not in all_stacks:
            continue
        
        stacks = all_stacks[component]
        
        # Component header with color
        console.print(f"\n[bold cyan]{component.upper()}[/bold cyan]")
        
        for stack_info in stacks:
            stack_name = stack_info["name"]
            status = stack_info["status"]
            outputs = stack_info.get("outputs", {})
            
            # Main status line with color-coded status
            if "✓" in status:
                console.print(f"  [green]✓[/green] {stack_name}")
            elif "⚠" in status:
                console.print(f"  [yellow]⚠[/yellow] {stack_name}")
            elif "Unknown" in status:
                console.print(f"  [yellow]?[/yellow] {stack_name}")
            else:
                console.print(f"  [red]✗[/red] {stack_name}")
            
            # Show key details from outputs
            if outputs and status == "✓ Deployed":
                if component == "infra":
                    rg = automation.get_output_value(outputs, "resource_group_name", "")
                    cluster = automation.get_output_value(outputs, "cluster_name", "")
                    if rg:
                        console.print(f"    [dim]• Resource Group:[/dim] {rg}")
                    if cluster:
                        console.print(f"    [dim]• AKS Cluster:[/dim] {cluster}")
                
                elif component == "storage":
                    account = automation.get_output_value(outputs, "account_name", "")
                    containers = automation.get_output_value(outputs, "containers", [])
                    if account:
                        console.print(f"    [dim]• Account:[/dim] {account}")
                    if containers:
                        # Handle different serialization formats from Pulumi
                        container_names = []
                        if isinstance(containers[0], dict):
                            # Normal dict format
                            container_names = [c.get('name', '') for c in containers]
                        elif isinstance(containers[0], list):
                            # Nested list format [[['name', 'value'], ...], ...]
                            for container_data in containers:
                                container_dict = dict(container_data) if container_data else {}
                                if "name" in container_dict:
                                    container_names.append(container_dict["name"])
                        elif isinstance(containers[0], str):
                            # Already a list of strings
                            container_names = containers

                        if container_names:
                            console.print(f"    [dim]• Containers:[/dim] {', '.join(container_names[:4])}")
                
                elif component == "workspace":
                    namespace = automation.get_output_value(outputs, "namespace", "")
                    workers = automation.get_output_value(outputs, "worker_count", "")
                    scheduler = automation.get_output_value(outputs, "scheduler_address", "")
                    if namespace:
                        console.print(f"    [dim]• Namespace:[/dim] {namespace}")
                    if workers:
                        console.print(f"    [dim]• Workers:[/dim] [green]{workers}[/green]")
                    if scheduler:
                        console.print(f"    [dim]• Scheduler:[/dim] {scheduler}")
                
                elif component == "adaptive":
                    namespace = automation.get_output_value(outputs, "namespace", "")
                    algorithm = automation.get_output_value(outputs, "algorithm", "")
                    replicas = automation.get_output_value(outputs, "worker_replicas", "")
                    if namespace:
                        console.print(f"    [dim]• Namespace:[/dim] {namespace}")
                    if algorithm:
                        console.print(f"    [dim]• Algorithm:[/dim] {algorithm}")
                    if replicas:
                        console.print(f"    [dim]• Worker replicas:[/dim] [green]{replicas}[/green]")
                    if outputs.get("postgres_dsn"):
                        console.print(f"    [dim]• Database:[/dim] [green]✓ Postgres[/green]")
    
    # Run smoke tests if requested
    if smoke_test:
        section("\nSmoke Tests")
        run_smoke_tests(all_stacks)
    
    # Show helpful commands
    console.print("\n[bold]Useful Commands[/bold]")
    commands([
        ("Workspace details", f"mops workspace status{' --env ' + env_filter if env_filter else ''}"),
        ("Storage info", f"mops storage info{' --env ' + env_filter if env_filter else ''}"),
    ])


def run_smoke_tests(all_stacks: Dict[str, List[Dict[str, Any]]]):
    """Run smoke tests for deployed components.
    
    Args:
        all_stacks: Dictionary of all deployed stacks
    """
    from .k8s_client import (
        check_cluster_connectivity, 
        get_pod_status,
        namespace_exists,
        run_kubectl_with_fresh_config
    )
    
    info("Running connectivity tests...\n")
    
    # First check if we have infrastructure
    if "infra" not in all_stacks or not all_stacks["infra"]:
        error("  ✗ No infrastructure deployed")
        info("\n  Run 'mops infra up' to deploy infrastructure first")
        return
    
    # Get environment from first infra stack
    infra_stack = all_stacks["infra"][0]
    env = infra_stack.get("env", "dev")
    
    # Check cluster connectivity
    info("Checking cluster connectivity...")
    connected, message = check_cluster_connectivity(env)
    if not connected:
        error(f"  ✗ {message}")
        info("\n  Your infrastructure may need to be refreshed.")
        info("  Try: mops infra status")
        return
    success(f"  ✓ {message}\n")
    
    # Check storage connectivity from workspace
    if "workspace" in all_stacks and "storage" in all_stacks:
        for ws_stack in all_stacks.get("workspace", []):
            if ws_stack["status"] != "✓ Deployed":
                continue

            outputs = ws_stack.get("outputs", {})
            namespace = automation.get_output_value(outputs, "namespace", "")

            if namespace:
                info(f"Testing storage access from workspace ({namespace})...")

                # Check if namespace exists
                if not namespace_exists(namespace, env):
                    warning(f"  ⚠ Namespace {namespace} not found")
                    continue

                # Simple check: verify the storage secret exists in the namespace
                check_cmd = [
                    "get", "secret", "modelops-storage",
                    "-n", namespace,
                    "-o", "name"
                ]

                try:
                    result = run_kubectl_with_fresh_config(check_cmd, env, timeout=10)

                    if result.returncode == 0 and "secret/modelops-storage" in result.stdout:
                        success("  ✓ Storage secret configured in workspace")

                        # Check if the secret has the expected keys
                        get_keys_cmd = [
                            "get", "secret", "modelops-storage",
                            "-n", namespace,
                            "-o", "jsonpath={.data}"
                        ]
                        keys_result = run_kubectl_with_fresh_config(get_keys_cmd, env, timeout=10)

                        if keys_result.returncode == 0 and "AZURE_STORAGE_CONNECTION_STRING" in keys_result.stdout:
                            success("  ✓ Storage connection string present")
                    else:
                        warning("  ⚠ Storage secret not found in workspace")

                except Exception as e:
                    error(f"  ✗ Test failed: {e}")
    
    # Check Dask connectivity
    if "workspace" in all_stacks:
        for ws_stack in all_stacks.get("workspace", []):
            if ws_stack["status"] != "✓ Deployed":
                continue
            
            outputs = ws_stack.get("outputs", {})
            namespace = automation.get_output_value(outputs, "namespace", "")
            
            if namespace:
                info(f"\nTesting Dask scheduler connectivity ({namespace})...")
                
                try:
                    # Use K8s client to check scheduler pods
                    scheduler_pods = get_pod_status(namespace, "app=dask-scheduler", env)
                    
                    if scheduler_pods:
                        running = all(pod["phase"] == "Running" and pod["ready"] for pod in scheduler_pods)
                        if running:
                            success(f"  ✓ Dask scheduler running")
                        else:
                            warning(f"  ⚠ Dask scheduler not ready")
                    else:
                        warning("  ⚠ No Dask scheduler found")
                    
                    # Check workers too
                    worker_pods = get_pod_status(namespace, "app=dask-worker", env)
                    worker_count = automation.get_output_value(outputs, "worker_count", 0)
                    
                    if worker_pods:
                        running_count = sum(1 for pod in worker_pods if pod["phase"] == "Running")
                        success(f"  ✓ {running_count}/{worker_count} workers running")
                    else:
                        warning("  ⚠ No worker pods found")
                    
                except Exception as e:
                    error(f"  ✗ Test failed: {e}")
    
    info("\nSmoke tests completed")


# Also export a simpler default command at module level
@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    env: Optional[str] = env_option(),
    smoke_test: bool = typer.Option(
        False,
        "--smoke-test",
        help="Run smoke tests for connectivity validation"
    )
):
    """Show status of all ModelOps infrastructure.
    
    This is the default command when no subcommand is specified.
    """
    if ctx.invoked_subcommand is None:
        status_all(env=env, smoke_test=smoke_test)
