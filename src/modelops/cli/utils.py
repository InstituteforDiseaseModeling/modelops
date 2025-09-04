"""Shared utilities for CLI commands."""

import re
from pathlib import Path
from typing import Optional
import pulumi.automation as auto
import typer
from .display import console, success, warning, error, info, section, commands


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
        error("Error: Configuration not initialized")
        info("Run 'mops config init' to create configuration")
        if command_name:
            info(f"(Required for 'mops {command_name}')")
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
        error("\n❌ Error: Pulumi stack is locked by another process")
        warning("\nThis usually happens when:")
        info("  • A previous Pulumi operation was interrupted")
        info("  • Another Pulumi command is currently running")
        info("  • A Pulumi process crashed without cleaning up")
        
        section("To fix this, run:")
        commands([
            ("", f"pulumi cancel --cwd {work_dir} --stack {stack_name} --yes")
        ])
        
        info("\nIf the problem persists, check for running Pulumi processes:")
        commands([
            ("", "ps aux | grep pulumi")
        ])
        
    elif "code: 255" in error_msg:
        # Generic Pulumi error with exit code 255
        error("\n❌ Pulumi operation failed")
        
        # Try to extract more specific error information
        if "stderr:" in error_msg:
            stderr_match = re.search(r'stderr: (.+?)(?:\n|$)', error_msg)
            if stderr_match:
                warning(f"\nError details: {stderr_match.group(1)}")
        
        section("Troubleshooting steps:")
        commands([
            ("Check stack status", f"pulumi stack --cwd {work_dir} --stack {stack_name}"),
            ("View detailed logs", f"pulumi logs --cwd {work_dir} --stack {stack_name}"),
            ("If stuck, cancel", f"pulumi cancel --cwd {work_dir} --stack {stack_name} --yes")
        ])
    
    elif isinstance(e, auto.CommandError):
        # Handle other Pulumi command errors
        error(f"\n❌ Pulumi command failed: {error_msg}")
        
        if "protected" in error_msg.lower():
            warning("\n⚠️  Resource protection detected")
            info("Some resources are protected from deletion. Use appropriate flags to force deletion.")
        elif "not found" in error_msg.lower():
            warning("\n⚠️  Stack or resource not found")
            info("The requested stack or resources may not exist.")
        else:
            section("For more details, check:")
            commands([
                ("Stack status", f"pulumi stack --cwd {work_dir} --stack {stack_name}"),
                ("Recent operations", f"pulumi history --cwd {work_dir} --stack {stack_name}")
            ])
    
    else:
        # Generic error handling
        error(f"\nError: {error_msg}")
        section("Debug commands:")
        commands([
            ("Check stack", f"pulumi stack --cwd {work_dir} --stack {stack_name}"),
            ("View config", f"pulumi config --cwd {work_dir} --stack {stack_name}")
        ])