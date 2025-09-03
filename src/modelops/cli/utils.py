"""Shared utilities for CLI commands."""

import re
from pathlib import Path
from typing import Optional
from rich.console import Console
import pulumi.automation as auto
import typer

console = Console()


def get_config_or_exit(command_name: str = None):
    """Get config instance or exit with helpful message.
    
    Args:
        command_name: Optional command name for better error context
        
    Returns:
        ModelOpsConfig instance
        
    Raises:
        typer.Exit: If config not found
    """
    from ..core.config import ModelOpsConfig, ConfigNotFoundError
    
    try:
        return ModelOpsConfig.get_instance()
    except ConfigNotFoundError:
        console.print("[red]Error: Configuration not initialized[/red]")
        console.print("Run 'mops config init' to create configuration")
        if command_name:
            console.print(f"[dim](Required for 'mops {command_name}')[/dim]")
        raise typer.Exit(1)


def resolve_env(env: Optional[str]) -> str:
    """Resolve environment from parameter or config defaults.
    
    Args:
        env: Environment parameter from CLI (may be None)
        
    Returns:
        Resolved environment string
        
    Raises:
        typer.Exit: If config not found
    """
    if env is None:
        config = get_config_or_exit()
        return config.defaults.environment
    return env


def resolve_provider(provider: Optional[str]) -> str:
    """Resolve provider from parameter or config defaults.
    
    Args:
        provider: Provider parameter from CLI (may be None)
        
    Returns:
        Resolved provider string
        
    Raises:
        typer.Exit: If config not found
    """
    if provider is None:
        config = get_config_or_exit()
        return config.defaults.provider
    return provider


def handle_pulumi_error(e: Exception, work_dir: str, stack_name: str) -> None:
    """
    Handle Pulumi errors with helpful recovery commands.
    
    Args:
        e: The exception that was raised
        work_dir: The Pulumi working directory
        stack_name: The name of the Pulumi stack
    """
    error_msg = str(e)
    
    # Check for lock file errors
    if "locked by" in error_msg or "lock file" in error_msg:
        console.print("\n[red]❌ Error: Pulumi stack is locked by another process[/red]")
        console.print("\n[yellow]This usually happens when:[/yellow]")
        console.print("  • A previous Pulumi operation was interrupted")
        console.print("  • Another Pulumi command is currently running")
        console.print("  • A Pulumi process crashed without cleaning up")
        
        console.print("\n[bold]To fix this, run:[/bold]")
        console.print(f"\n  [cyan]pulumi cancel --cwd {work_dir} --stack {stack_name} --yes[/cyan]")
        
        console.print("\n[dim]If the problem persists, check for running Pulumi processes:[/dim]")
        console.print("  [dim]ps aux | grep pulumi[/dim]")
        
    elif "code: 255" in error_msg:
        # Generic Pulumi error with exit code 255
        console.print("\n[red]❌ Pulumi operation failed[/red]")
        
        # Try to extract more specific error information
        if "stderr:" in error_msg:
            stderr_match = re.search(r'stderr: (.+?)(?:\n|$)', error_msg)
            if stderr_match:
                console.print(f"\n[yellow]Error details:[/yellow] {stderr_match.group(1)}")
        
        console.print("\n[bold]Troubleshooting steps:[/bold]")
        console.print(f"  1. Check stack status: [cyan]pulumi stack --cwd {work_dir} --stack {stack_name}[/cyan]")
        console.print(f"  2. View detailed logs: [cyan]pulumi logs --cwd {work_dir} --stack {stack_name}[/cyan]")
        console.print(f"  3. If stuck, cancel: [cyan]pulumi cancel --cwd {work_dir} --stack {stack_name} --yes[/cyan]")
    
    elif isinstance(e, auto.CommandError):
        # Handle other Pulumi command errors
        console.print(f"\n[red]❌ Pulumi command failed:[/red] {error_msg}")
        
        if "protected" in error_msg.lower():
            console.print("\n[yellow]⚠️  Resource protection detected[/yellow]")
            console.print("Some resources are protected from deletion. Use appropriate flags to force deletion.")
        elif "not found" in error_msg.lower():
            console.print("\n[yellow]⚠️  Stack or resource not found[/yellow]")
            console.print("The requested stack or resources may not exist.")
        else:
            console.print("\n[bold]For more details, check:[/bold]")
            console.print(f"  • Stack status: [cyan]pulumi stack --cwd {work_dir} --stack {stack_name}[/cyan]")
            console.print(f"  • Recent operations: [cyan]pulumi history --cwd {work_dir} --stack {stack_name}[/cyan]")
    
    else:
        # Generic error handling
        console.print(f"\n[red]Error: {error_msg}[/red]")
        console.print("\n[bold]Debug commands:[/bold]")
        console.print(f"  • Check stack: [cyan]pulumi stack --cwd {work_dir} --stack {stack_name}[/cyan]")
        console.print(f"  • View config: [cyan]pulumi config --cwd {work_dir} --stack {stack_name}[/cyan]")