"""Input validation for CLI commands.

This module provides validators for workspace configuration parameters to prevent
runtime failures when Kubernetes rejects invalid resource formats or conflicting settings.
"""

import re
from typing import Optional

import typer


def validate_memory_format(value: str) -> str:
    """Validate Kubernetes memory format.

    Args:
        value: Memory string (e.g., "4Gi", "512Mi", "1000M")

    Returns:
        The validated memory string

    Raises:
        typer.BadParameter: If format is invalid

    Examples:
        >>> validate_memory_format("4Gi")
        '4Gi'
        >>> validate_memory_format("512Mi")
        '512Mi'
        >>> validate_memory_format("invalid")
        Traceback (most recent call last):
        ...
        typer.BadParameter: Invalid memory format: 'invalid'. Use format like '4Gi', '512Mi', '1000M'
    """
    # Kubernetes memory format: integer or decimal followed by optional suffix
    # Suffixes: E, P, T, G, M, K (power of 1000) or Ei, Pi, Ti, Gi, Mi, Ki (power of 1024)
    pattern = r"^\d+(\.\d+)?(Ei|Pi|Ti|Gi|Mi|Ki|E|P|T|G|M|K)?$"

    if not re.match(pattern, value):
        raise typer.BadParameter(
            f"Invalid memory format: '{value}'. Use format like '4Gi', '512Mi', '1000M'"
        )

    return value


def validate_cpu_format(value: str) -> str:
    """Validate Kubernetes CPU format.

    Args:
        value: CPU string (e.g., "2", "1.5", "500m")

    Returns:
        The validated CPU string

    Raises:
        typer.BadParameter: If format is invalid

    Examples:
        >>> validate_cpu_format("2")
        '2'
        >>> validate_cpu_format("1.5")
        '1.5'
        >>> validate_cpu_format("500m")
        '500m'
        >>> validate_cpu_format("invalid")
        Traceback (most recent call last):
        ...
        typer.BadParameter: Invalid CPU format: 'invalid'. Use format like '2', '1.5', '500m'
    """
    # Kubernetes CPU format: integer, decimal, or millicores (integer + 'm')
    # Examples: "1", "1.5", "500m"
    pattern = r"^\d+(\.\d+)?m?$"

    if not re.match(pattern, value):
        raise typer.BadParameter(
            f"Invalid CPU format: '{value}'. Use format like '2', '1.5', '500m'"
        )

    return value


def validate_percentage(value: int, min_val: int = 0, max_val: int = 100) -> int:
    """Validate percentage value is in range.

    Args:
        value: Percentage value
        min_val: Minimum allowed value (inclusive)
        max_val: Maximum allowed value (inclusive)

    Returns:
        The validated percentage value

    Raises:
        typer.BadParameter: If value is out of range

    Examples:
        >>> validate_percentage(70)
        70
        >>> validate_percentage(150)
        Traceback (most recent call last):
        ...
        typer.BadParameter: Percentage must be between 0 and 100, got 150
    """
    if not (min_val <= value <= max_val):
        raise typer.BadParameter(
            f"Percentage must be between {min_val} and {max_val}, got {value}"
        )

    return value


def validate_positive_int(value: int, name: str = "value") -> int:
    """Validate integer is positive.

    Args:
        value: Integer value to validate
        name: Name of the parameter for error messages

    Returns:
        The validated integer

    Raises:
        typer.BadParameter: If value is not positive

    Examples:
        >>> validate_positive_int(5)
        5
        >>> validate_positive_int(0, "replicas")
        Traceback (most recent call last):
        ...
        typer.BadParameter: replicas must be positive, got 0
    """
    if value <= 0:
        raise typer.BadParameter(f"{name} must be positive, got {value}")

    return value


def validate_no_autoscaling_conflicts(
    worker_replicas: Optional[int],
    enable_autoscaling: Optional[bool],
    disable_autoscaling: Optional[bool],
    min_workers: Optional[int],
    max_workers: Optional[int],
) -> None:
    """Validate that autoscaling flags don't conflict.

    Args:
        worker_replicas: Fixed replica count (conflicts with autoscaling)
        enable_autoscaling: Enable autoscaling flag
        disable_autoscaling: Disable autoscaling flag
        min_workers: Minimum workers for autoscaling
        max_workers: Maximum workers for autoscaling

    Raises:
        typer.BadParameter: If conflicting flags are provided

    Examples:
        >>> validate_no_autoscaling_conflicts(worker_replicas=5, enable_autoscaling=True, disable_autoscaling=False, min_workers=None, max_workers=None)
        Traceback (most recent call last):
        ...
        typer.BadParameter: Cannot use --worker-replicas with autoscaling enabled. Use --disable-autoscaling first.

        >>> validate_no_autoscaling_conflicts(worker_replicas=None, enable_autoscaling=True, disable_autoscaling=True, min_workers=None, max_workers=None)
        Traceback (most recent call last):
        ...
        typer.BadParameter: Cannot use both --enable-autoscaling and --disable-autoscaling
    """
    # Check for enable/disable conflict
    if enable_autoscaling and disable_autoscaling:
        raise typer.BadParameter(
            "Cannot use both --enable-autoscaling and --disable-autoscaling"
        )

    # Determine if autoscaling will be enabled after applying flags
    # Priority: explicit disable > explicit enable > check other flags
    autoscaling_enabled = None
    if disable_autoscaling:
        autoscaling_enabled = False
    elif enable_autoscaling:
        autoscaling_enabled = True

    # If autoscaling state is determined, check conflicts
    if autoscaling_enabled is False:
        # Disabling autoscaling - min/max workers shouldn't be set
        if min_workers is not None or max_workers is not None:
            raise typer.BadParameter(
                "Cannot use --min-workers or --max-workers with --disable-autoscaling"
            )

    if autoscaling_enabled is True:
        # Enabling autoscaling - worker replicas shouldn't be set
        if worker_replicas is not None:
            raise typer.BadParameter(
                "Cannot use --worker-replicas with autoscaling enabled. Use --disable-autoscaling first."
            )

    # If autoscaling state is unknown but worker_replicas is set with min/max, that's also a conflict
    if autoscaling_enabled is None and worker_replicas is not None:
        if min_workers is not None or max_workers is not None:
            raise typer.BadParameter(
                "Cannot use --worker-replicas with --min-workers or --max-workers. "
                "Use --disable-autoscaling to set fixed replicas."
            )


def validate_min_max_workers(min_workers: Optional[int], max_workers: Optional[int]) -> None:
    """Validate min_workers <= max_workers.

    Args:
        min_workers: Minimum workers for autoscaling
        max_workers: Maximum workers for autoscaling

    Raises:
        typer.BadParameter: If min > max

    Examples:
        >>> validate_min_max_workers(2, 10)
        >>> validate_min_max_workers(10, 2)
        Traceback (most recent call last):
        ...
        typer.BadParameter: --min-workers (10) cannot be greater than --max-workers (2)
    """
    if min_workers is not None and max_workers is not None:
        if min_workers > max_workers:
            raise typer.BadParameter(
                f"--min-workers ({min_workers}) cannot be greater than --max-workers ({max_workers})"
            )


def validate_all_workspace_params(
    scheduler_memory: Optional[str] = None,
    scheduler_cpu: Optional[str] = None,
    worker_memory: Optional[str] = None,
    worker_cpu: Optional[str] = None,
    worker_replicas: Optional[int] = None,
    worker_processes: Optional[int] = None,
    worker_threads: Optional[int] = None,
    enable_autoscaling: Optional[bool] = None,
    disable_autoscaling: Optional[bool] = None,
    min_workers: Optional[int] = None,
    max_workers: Optional[int] = None,
    target_cpu_percent: Optional[int] = None,
) -> None:
    """Validate all workspace parameters in one pass.

    This is a convenience function that runs all applicable validators.

    Args:
        scheduler_memory: Scheduler memory (e.g., "2Gi")
        scheduler_cpu: Scheduler CPU (e.g., "1")
        worker_memory: Worker memory (e.g., "8Gi")
        worker_cpu: Worker CPU (e.g., "3.5")
        worker_replicas: Fixed worker replica count
        worker_processes: Number of processes per worker pod
        worker_threads: Number of threads per worker process
        enable_autoscaling: Enable autoscaling flag
        disable_autoscaling: Disable autoscaling flag
        min_workers: Minimum workers for autoscaling
        max_workers: Maximum workers for autoscaling
        target_cpu_percent: Target CPU utilization percentage

    Raises:
        typer.BadParameter: If any validation fails
    """
    # Validate resource formats
    if scheduler_memory is not None:
        validate_memory_format(scheduler_memory)
    if scheduler_cpu is not None:
        validate_cpu_format(scheduler_cpu)
    if worker_memory is not None:
        validate_memory_format(worker_memory)
    if worker_cpu is not None:
        validate_cpu_format(worker_cpu)

    # Validate positive integers
    if worker_replicas is not None:
        validate_positive_int(worker_replicas, "worker-replicas")
    if worker_processes is not None:
        validate_positive_int(worker_processes, "worker-processes")
    if worker_threads is not None:
        validate_positive_int(worker_threads, "worker-threads")
    if min_workers is not None:
        validate_positive_int(min_workers, "min-workers")
    if max_workers is not None:
        validate_positive_int(max_workers, "max-workers")

    # Validate percentage
    if target_cpu_percent is not None:
        validate_percentage(target_cpu_percent)

    # Validate autoscaling conflicts
    validate_no_autoscaling_conflicts(
        worker_replicas,
        enable_autoscaling,
        disable_autoscaling,
        min_workers,
        max_workers,
    )

    # Validate min/max relationship
    validate_min_max_workers(min_workers, max_workers)
