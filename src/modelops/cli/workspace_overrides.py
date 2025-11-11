"""Helper functions for workspace configuration overrides.

This module provides utilities for:
- Building configuration overrides from CLI parameters
- Applying precedence rules (CLI > explicit config > unified config > defaults)
- Detecting changes that require full restart vs zero-downtime update
- Displaying configuration diffs to users
"""

from typing import Any, Dict, Optional

from rich.console import Console
from rich.table import Table

from ..core.unified_config import WorkspaceSpec


def build_cli_overrides(**kwargs) -> Dict[str, Any]:
    """Build configuration overrides from CLI parameters.

    Only includes parameters that were explicitly provided (not None).

    Args:
        **kwargs: CLI parameters from typer command

    Returns:
        Dictionary of non-None overrides

    Examples:
        >>> build_cli_overrides(worker_cpu="4", worker_memory=None, worker_replicas=5)
        {'worker_cpu': '4', 'worker_replicas': 5}
    """
    return {k: v for k, v in kwargs.items() if v is not None}


def apply_overrides(base_config: WorkspaceSpec, overrides: Dict[str, Any]) -> WorkspaceSpec:
    """Apply overrides to base configuration.

    Creates a new WorkspaceSpec with overrides applied.

    Args:
        base_config: Base workspace configuration
        overrides: Dictionary of overrides to apply

    Returns:
        New WorkspaceSpec with overrides applied

    Examples:
        >>> base = WorkspaceSpec(worker_cpu="2", worker_replicas=3)
        >>> overrides = {"worker_cpu": "4"}
        >>> updated = apply_overrides(base, overrides)
        >>> updated.worker_cpu
        '4'
        >>> updated.worker_replicas
        3
    """
    # Convert base config to dict, apply overrides, create new instance
    config_dict = base_config.model_dump()
    config_dict.update(overrides)
    return WorkspaceSpec.model_validate(config_dict)


def requires_replacement(changes: Dict[str, tuple[Any, Any]]) -> bool:
    """Check if changes require full workspace replacement (down/up).

    Some configuration changes cannot be applied via Pulumi update and require
    destroying and recreating the workspace.

    Args:
        changes: Dictionary mapping field names to (old_value, new_value) tuples

    Returns:
        True if any change requires replacement, False if all can be updated

    Note:
        Currently, all workspace changes can be handled via Pulumi update.
        This function is here for future extensibility.

    Examples:
        >>> requires_replacement({"worker_cpu": ("2", "4")})
        False
        >>> requires_replacement({"worker_memory": ("8Gi", "16Gi")})
        False
    """
    # Define fields that require full replacement
    # These are typically structural changes like namespace, selectors, etc.
    replacement_fields = {
        # Future: Add any fields that require full restart
        # Example: "namespace", "cluster_name", etc.
    }

    return any(field in replacement_fields for field in changes.keys())


def compute_changes(
    current: WorkspaceSpec, updated: WorkspaceSpec
) -> Dict[str, tuple[Any, Any]]:
    """Compute differences between two workspace configurations.

    Args:
        current: Current workspace configuration
        updated: Updated workspace configuration

    Returns:
        Dictionary mapping changed field names to (old_value, new_value) tuples

    Examples:
        >>> current = WorkspaceSpec(worker_cpu="2", worker_memory="8Gi", worker_replicas=3)
        >>> updated = WorkspaceSpec(worker_cpu="4", worker_memory="8Gi", worker_replicas=3)
        >>> compute_changes(current, updated)
        {'worker_cpu': ('2', '4')}
    """
    changes = {}

    current_dict = current.model_dump()
    updated_dict = updated.model_dump()

    for key in current_dict:
        if current_dict[key] != updated_dict[key]:
            changes[key] = (current_dict[key], updated_dict[key])

    return changes


def show_config_diff(
    changes: Dict[str, tuple[Any, Any]],
    console: Optional[Console] = None,
) -> None:
    """Display configuration changes in a formatted table.

    Args:
        changes: Dictionary mapping field names to (old_value, new_value) tuples
        console: Rich console for output (creates new if None)

    Examples:
        >>> changes = {"worker_cpu": ("2", "4"), "worker_memory": ("8Gi", "16Gi")}
        >>> show_config_diff(changes)
        ┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
        ┃ Configuration ┃ Current   ┃ New       ┃ Impact             ┃
        ┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
        │ worker_cpu    │ 2         │ 4         │ Rolling Update     │
        │ worker_memory │ 8Gi       │ 16Gi      │ Rolling Update     │
        └───────────────┴───────────┴───────────┴────────────────────┘
    """
    if console is None:
        console = Console()

    if not changes:
        console.print("[green]No configuration changes detected[/green]")
        return

    # Create rich table
    table = Table(title="Workspace Configuration Changes")
    table.add_column("Configuration", style="cyan", no_wrap=True)
    table.add_column("Current", style="yellow")
    table.add_column("New", style="green")
    table.add_column("Impact", style="magenta")

    # Determine update type
    needs_replacement = requires_replacement(changes)
    update_type = "Full Restart" if needs_replacement else "Rolling Update"

    # Add rows for each change
    for field, (old_val, new_val) in sorted(changes.items()):
        # Determine field-specific impact
        if field in ("scheduler_memory", "scheduler_cpu"):
            impact = "Recreate (brief downtime)"
        elif field in ("worker_memory", "worker_cpu", "worker_replicas"):
            impact = "Rolling Update (zero downtime)"
        elif field in ("autoscaling_enabled", "autoscaling_min_workers", "autoscaling_max_workers"):
            impact = "HPA Update (zero downtime)"
        elif field in ("worker_processes", "worker_threads"):
            impact = "Rolling Update (zero downtime)"
        else:
            impact = update_type

        table.add_row(
            field,
            str(old_val),
            str(new_val),
            impact,
        )

    console.print(table)

    # Print summary
    if needs_replacement:
        console.print(
            "\n[yellow]⚠️  Some changes require full workspace restart (down/up)[/yellow]"
        )
        console.print("This will cause temporary downtime while resources are recreated.")
    else:
        console.print(
            "\n[green]✓ All changes can be applied with zero downtime[/green]"
        )


def get_autoscaling_effective_state(
    current_enabled: bool,
    enable_flag: Optional[bool],
    disable_flag: Optional[bool],
) -> bool:
    """Determine effective autoscaling state after applying flags.

    Precedence: disable_flag > enable_flag > current_enabled

    Args:
        current_enabled: Current autoscaling state
        enable_flag: --enable-autoscaling flag
        disable_flag: --disable-autoscaling flag

    Returns:
        Effective autoscaling state

    Examples:
        >>> get_autoscaling_effective_state(True, None, None)
        True
        >>> get_autoscaling_effective_state(True, None, True)
        False
        >>> get_autoscaling_effective_state(False, True, None)
        True
        >>> get_autoscaling_effective_state(True, True, True)  # disable wins
        False
    """
    if disable_flag:
        return False
    if enable_flag:
        return True
    return current_enabled


def format_resource_summary(config: WorkspaceSpec) -> str:
    """Format workspace configuration as human-readable summary.

    Args:
        config: Workspace configuration

    Returns:
        Formatted summary string

    Examples:
        >>> config = WorkspaceSpec(
        ...     scheduler_cpu="1", scheduler_memory="2Gi",
        ...     worker_cpu="4", worker_memory="16Gi", worker_replicas=3
        ... )
        >>> print(format_resource_summary(config))
        Scheduler: 1 CPU, 2Gi memory
        Workers: 4 CPU, 16Gi memory, 3 replicas
    """
    lines = [
        f"Scheduler: {config.scheduler_cpu} CPU, {config.scheduler_memory} memory",
        f"Workers: {config.worker_cpu} CPU, {config.worker_memory} memory",
    ]

    if config.autoscaling_enabled:
        lines.append(
            f"  Autoscaling: {config.autoscaling_min_workers}-{config.autoscaling_max_workers} workers"
            f" (target {config.autoscaling_target_cpu}% CPU)"
        )
    else:
        lines.append(f"  Fixed: {config.worker_replicas} replicas")

    lines.append(
        f"  Processes: {config.worker_processes} processes/pod, {config.worker_threads} threads/process"
    )

    return "\n".join(lines)
