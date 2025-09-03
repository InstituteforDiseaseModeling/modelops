"""Consolidated display utilities for CLI commands."""
from rich.console import Console
from rich.table import Table
from typing import Dict, List, Optional, Any, Tuple

console = Console()


def success(message: str) -> None:
    """Print success message."""
    console.print(f"[green]✓ {message}[/green]")


def warning(message: str) -> None:
    """Print warning message."""
    console.print(f"[yellow]⚠️  {message}[/yellow]")


def error(message: str) -> None:
    """Print error message."""
    console.print(f"[red]❌ {message}[/red]")


def info(message: str) -> None:
    """Print info message."""
    console.print(message)


def section(title: str) -> None:
    """Print section header."""
    console.print(f"\n[bold]{title}[/bold]")


def info_dict(data: Dict[str, Any], indent: str = "  ") -> None:
    """Print a dictionary as indented key-value pairs."""
    for key, value in data.items():
        console.print(f"{indent}{key}: {value}")


def commands(cmds: List[Tuple[str, str]], indent: str = "  ") -> None:
    """Print a list of commands with optional descriptions."""
    for desc, cmd in cmds:
        if desc:
            console.print(f"{indent}# {desc}")
        console.print(f"{indent}{cmd}")


def urls(url_map: Dict[str, str], indent: str = "  ") -> None:
    """Print URLs with highlighting."""
    for label, url in url_map.items():
        if url.startswith("http"):
            console.print(f"{indent}{label}: [cyan]{url}[/cyan]")
        else:
            console.print(f"{indent}{label}: {url}")


def workspace_info(outputs: Dict, env: str, stack_name: str) -> None:
    """Standard workspace info display."""
    from ..core import StackNaming
    namespace = outputs.get('namespace', {}).value if outputs.get('namespace') else StackNaming.get_namespace("dask", env)
    workers = outputs.get('worker_count', {}).value if outputs.get('worker_count') else 'unknown'
    
    info_dict({
        "Environment": env,
        "Stack": stack_name,
        "Namespace": namespace,
        "Workers": workers
    })
    
    section("Port-forward commands:")
    commands([
        ("For Dask client connections:", f"kubectl port-forward -n {namespace} svc/dask-scheduler 8786:8786"),
        ("For dashboard:", f"kubectl port-forward -n {namespace} svc/dask-scheduler 8787:8787")
    ])
    
    section("Access URLs (after port-forwarding):")
    urls({
        "Scheduler": "tcp://localhost:8786",
        "Dashboard": "http://localhost:8787"
    })


def workspace_commands(namespace: str) -> None:
    """Display useful workspace commands."""
    section("Useful commands:")
    info_dict({
        "Logs": f"kubectl logs -n {namespace} -l app=dask-scheduler",
        "Workers": f"kubectl get pods -n {namespace} -l app=dask-worker"
    })


def dim(message: str) -> None:
    """Print dimmed output (for Pulumi output)."""
    console.print(f"[dim]{message}[/dim]", end="")