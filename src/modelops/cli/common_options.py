"""Common Typer options shared across CLI commands.

This module provides reusable option definitions to ensure consistency
and reduce duplication across CLI modules.
"""

import typer


def env_option(
    default: str | None = None,
    help_text: str = "Environment name (dev, staging, prod)",
) -> typer.Option:
    """Create a standard environment option.

    Args:
        default: Default environment value
        help_text: Custom help text

    Returns:
        Configured Typer Option
    """
    return typer.Option(default, "--env", "-e", help=help_text)


def yes_option(help_text: str = "Skip confirmation prompt") -> typer.Option:
    """Create a standard yes/skip confirmation option.

    Args:
        help_text: Custom help text

    Returns:
        Configured Typer Option
    """
    return typer.Option(False, "--yes", "-y", help=help_text)


def config_option(
    exists: bool = True, help_text: str = "Configuration file (YAML)"
) -> typer.Option:
    """Create a standard configuration file option.

    Args:
        exists: Whether file must exist
        help_text: Custom help text

    Returns:
        Configured Typer Option
    """
    return typer.Option(
        ...,
        "--config",
        "-c",
        help=help_text,
        exists=exists,
        file_okay=True,
        dir_okay=False,
        readable=True,
    )


def stack_option(component: str, help_text: str | None = None) -> typer.Option:
    """Create a stack name option.

    Args:
        component: Component name for default help text
        help_text: Custom help text

    Returns:
        Configured Typer Option
    """
    if not help_text:
        help_text = f"{component.capitalize()} stack name"

    return typer.Option(None, "--stack", "-s", help=help_text)


def run_id_option(help_text: str = "Run ID for the adaptive run") -> typer.Argument:
    """Create a run ID argument.

    Args:
        help_text: Custom help text

    Returns:
        Configured Typer Argument
    """
    return typer.Argument(..., help=help_text)


def verbose_option(help_text: str = "Show detailed output") -> typer.Option:
    """Create a verbose output option.

    Args:
        help_text: Custom help text

    Returns:
        Configured Typer Option
    """
    return typer.Option(False, "--verbose", "-v", help=help_text)


def output_format_option(default: str = "table", help_text: str = "Output format") -> typer.Option:
    """Create an output format option.

    Args:
        default: Default format
        help_text: Custom help text

    Returns:
        Configured Typer Option
    """
    return typer.Option(default, "--format", "-f", help=help_text)
