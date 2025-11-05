"""Pulumi Automation API helpers to reduce boilerplate across CLI commands.

This module provides simplified interfaces for common Pulumi operations,
eliminating repetitive code in CLI modules.

DEVELOPER NOTES - CRITICAL BUG FIX (October 2024)
=================================================

Bug: "Incorrect passphrase" errors when accessing Pulumi stacks
----------------------------------------------------------------
Different stacks were encrypted with different passphrases, making them
inaccessible after creation. This caused infrastructure operations to fail
with "incorrect passphrase" errors.

Root Causes:
1. LocalWorkspaceOptions was not passing environment variables to Pulumi
   subprocesses (env_vars was None)
2. PULUMI_CONFIG_PASSPHRASE_FILE was set in Python process but not inherited
   by Pulumi language host
3. Potential race condition in passphrase file creation (still exists but
   mitigated by sequential execution)

The Fix:
1. Set env_vars=dict(os.environ) in workspace_options() - CRITICAL
2. Remove PULUMI_CONFIG_PASSPHRASE to avoid precedence issues
3. Ensure _ensure_passphrase() is called before every Pulumi operation

Testing:
- Run tests/test_pulumi_passphrase.py to verify the fix
- All stacks should be accessible with the same passphrase
- No "incorrect passphrase" errors should occur

Future Improvements:
- Implement atomic file creation for passphrase (using tempfile + rename)
- Consider using file locking to prevent concurrent writes
- Pin secrets_provider="passphrase" in create_or_select_stack
"""

import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pulumi.automation as auto

from .naming import StackNaming
from .paths import ensure_work_dir, get_backend_url


def get_stack_output(component: str, output_key: str, env: str = "dev") -> str | None:
    """Get a single output value from a Pulumi stack.

    Args:
        component: Component name (e.g., "storage", "infra", "registry")
        output_key: Key of the output to retrieve
        env: Environment name (dev, staging, prod)

    Returns:
        The output value as a string, or None if not found
    """
    try:
        # Use the existing outputs function
        stack_outputs = outputs(component, env, refresh=False)

        # Return the specific output
        if output_key in stack_outputs:
            output = stack_outputs[output_key]
            # Handle Output objects
            if hasattr(output, "value"):
                return output.value
            return str(output)

        return None

    except Exception:
        # Silently fail - caller should handle missing outputs
        return None


def configure_quiet_environment():
    """Configure environment variables to suppress noisy output from gRPC/Pulumi."""
    # Suppress gRPC warnings about fork support
    os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
    # Set gRPC verbosity to error only
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
    # Suppress Pulumi's colorized output in non-TTY environments
    if not os.isatty(1):  # stdout is not a TTY
        os.environ.setdefault("NO_COLOR", "1")


# Apply quiet configuration on module import
configure_quiet_environment()


def _ensure_passphrase(verbose: bool = False):
    """Ensure Pulumi passphrase is configured from secrets file.

    Always uses ~/.modelops/secrets/pulumi-passphrase for consistency.
    Single source of truth - no fallbacks, no priority order.
    """
    # import hashlib
    # import threading

    passphrase_file = Path.home() / ".modelops" / "secrets" / "pulumi-passphrase"
    # thread_id = threading.current_thread().name
    # pid = os.getpid()

    if not passphrase_file.exists():
        # INSTRUMENTATION: Log creation attempt
        # print(f"[DIAG] Thread {thread_id} PID {pid}: Passphrase file does not exist, creating...")

        # Generate strong random passphrase ONCE
        passphrase_file.parent.mkdir(parents=True, exist_ok=True)
        passphrase = secrets.token_urlsafe(32)
        passphrase_file.write_text(passphrase)

        # INSTRUMENTATION: Log what was written
        # hash_val = hashlib.sha256(passphrase.encode()).hexdigest()[:8]
        # print(f"[DIAG] Thread {thread_id} PID {pid}: Created passphrase file with hash: {hash_val}")

        # Set permissions (skip on Windows)
        if os.name != "nt":
            passphrase_file.chmod(0o600)

        if verbose:
            print(f"  ✓ Generated Pulumi passphrase: {passphrase_file}")
    # else:
    # INSTRUMENTATION: Log that file exists and its hash
    # content = passphrase_file.read_text().strip()
    # hash_val = hashlib.sha256(content.encode()).hexdigest()[:8]
    # print(f"[DIAG] Thread {thread_id} PID {pid}: Passphrase file exists with hash: {hash_val}")

    # Always set to use this file - don't check other env vars
    os.environ["PULUMI_CONFIG_PASSPHRASE_FILE"] = str(passphrase_file)

    # INSTRUMENTATION: Log environment state
    # has_direct = "PULUMI_CONFIG_PASSPHRASE" in os.environ
    # print(f"[DIAG] Thread {thread_id} PID {pid}: PULUMI_CONFIG_PASSPHRASE_FILE set to: {passphrase_file}")
    # print(f"[DIAG] Thread {thread_id} PID {pid}: PULUMI_CONFIG_PASSPHRASE is: {'SET (DANGEROUS!)' if has_direct else 'NOT SET'}")


def workspace_options(project: str, work_dir: Path) -> auto.LocalWorkspaceOptions:
    """Create standard LocalWorkspaceOptions for Pulumi operations.

    Args:
        project: Project name
        work_dir: Working directory for Pulumi operations

    Returns:
        Configured LocalWorkspaceOptions with backend settings
    """
    # Ensure passphrase environment is loaded and clean
    _ensure_passphrase()

    # Remove any direct passphrase to avoid precedence issues
    os.environ.pop("PULUMI_CONFIG_PASSPHRASE", None)

    # INSTRUMENTATION: Check what environment variables are set
    # pass_file = os.environ.get("PULUMI_CONFIG_PASSPHRASE_FILE", "NOT SET")
    # pass_direct = "SET" if os.environ.get("PULUMI_CONFIG_PASSPHRASE") else "NOT SET"
    # print(f"[DIAG] workspace_options for {project}:")
    # print(f"[DIAG]   PULUMI_CONFIG_PASSPHRASE_FILE: {pass_file}")
    # print(f"[DIAG]   PULUMI_CONFIG_PASSPHRASE: {pass_direct}")
    # print(f"[DIAG]   env_vars will be: PASSING FULL ENVIRONMENT")

    return auto.LocalWorkspaceOptions(
        work_dir=str(work_dir),
        project_settings=auto.ProjectSettings(
            name=project,
            runtime="python",
            backend=auto.ProjectBackend(url=get_backend_url()),
        ),
        # CRITICAL FIX (Oct 2024): Pass environment to Pulumi subprocess
        # Without this, PULUMI_CONFIG_PASSPHRASE_FILE is not inherited by
        # the Pulumi language host, causing "incorrect passphrase" errors.
        # This was the root cause of stacks being encrypted with different
        # passphrases. NEVER set this to None!
        env_vars=dict(os.environ),
    )


def noop_program():
    """No-op Pulumi program for operations that only need stack access."""
    pass


def select_stack(
    component: str,
    env: str,
    run_id: str | None = None,
    program: Callable | None = None,
    work_dir: str | None = None,
) -> auto.Stack:
    """Select or create a Pulumi stack with standard configuration.

    Args:
        component: Component name (infra, workspace, adaptive, registry)
        env: Environment name
        run_id: Optional run ID for adaptive stacks
        program: Pulumi program to run (defaults to noop)
        work_dir: Optional custom work directory path

    Returns:
        Selected or created Pulumi stack
    """
    # INSTRUMENTATION: Log stack selection
    # import datetime
    # timestamp = datetime.datetime.now().isoformat()
    # print(f"[DIAG] {timestamp} Selecting stack for component: {component}, env: {env}")

    # Ensure secure passphrase is configured
    _ensure_passphrase()

    project = StackNaming.get_project_name(component)
    stack = StackNaming.get_stack_name(component, env, run_id)

    # Use provided work_dir or default based on component
    if work_dir is None:
        work_dir = ensure_work_dir(component)
    else:
        work_dir = Path(work_dir)

    # print(f"[DIAG] Creating/selecting stack: {stack} in project: {project}")

    return auto.create_or_select_stack(
        stack_name=stack,
        project_name=project,
        program=program or noop_program,
        opts=workspace_options(project, work_dir),
    )


def remove_stack(
    component: str,
    env: str,
    run_id: str | None = None,
    work_dir: str | None = None,
) -> None:
    """Remove a Pulumi stack completely.

    This is used when doing a full teardown with --destroy-all to ensure
    no empty stacks remain that could cause issues on next provisioning.

    Args:
        component: Component name
        env: Environment name
        run_id: Optional run ID for adaptive stacks
        work_dir: Optional custom work directory path
    """
    # Ensure secure passphrase is configured
    _ensure_passphrase()

    try:
        stack = select_stack(component, env, run_id, noop_program, work_dir)
        workspace = stack.workspace
        stack_name = stack.name

        # Remove the stack
        workspace.remove_stack(stack_name)
    except Exception as e:
        # If stack doesn't exist, that's fine
        if "no stack named" not in str(e).lower():
            raise


def outputs(
    component: str,
    env: str,
    run_id: str | None = None,
    refresh: bool = True,
    work_dir: str | None = None,
) -> dict[str, Any]:
    """Get outputs from a Pulumi stack.

    Args:
        component: Component name
        env: Environment name
        run_id: Optional run ID for adaptive stacks
        refresh: Whether to refresh stack before getting outputs
        work_dir: Optional custom work directory path

    Returns:
        Stack outputs dictionary
    """
    # Ensure secure passphrase is configured
    _ensure_passphrase()

    stack = select_stack(component, env, run_id, noop_program, work_dir)
    if refresh:
        stack.refresh(on_output=lambda _: None)
    return stack.outputs()


def up(
    component: str,
    env: str,
    run_id: str | None,
    program: Callable,
    on_output: Callable[[str], None] | None = None,
    work_dir: str | None = None,
) -> dict[str, Any]:
    """Run pulumi up on a stack.

    Args:
        component: Component name
        env: Environment name
        run_id: Optional run ID for adaptive stacks
        program: Pulumi program to run
        on_output: Optional callback for output messages
        work_dir: Optional custom work directory path

    Returns:
        Stack outputs after update
    """
    stack = select_stack(component, env, run_id, program, work_dir)
    result = stack.up(on_output=on_output or (lambda _: None))
    return result.outputs


def destroy(
    component: str,
    env: str,
    run_id: str | None = None,
    on_output: Callable[[str], None] | None = None,
    work_dir: str | None = None,
) -> None:
    """Destroy a Pulumi stack.

    Args:
        component: Component name
        env: Environment name
        run_id: Optional run ID for adaptive stacks
        on_output: Optional callback for output messages
        work_dir: Optional custom work directory path
    """
    stack = select_stack(component, env, run_id, noop_program, work_dir)
    stack.destroy(on_output=on_output or (lambda _: None))


def get_output_value(outputs: dict[str, Any], key: str, default: Any = None) -> Any:
    """Extract value from Pulumi outputs dictionary safely.

    Args:
        outputs: Pulumi stack outputs dictionary
        key: Key to extract
        default: Default value if key not found or has no value

    Returns:
        The output value or default

    Example:
        >>> outputs = {"namespace": {"value": "modelops-dev"}}
        >>> get_output_value(outputs, "namespace")
        "modelops-dev"
        >>> get_output_value(outputs, "missing", "default")
        "default"
    """
    output = outputs.get(key)
    if output is not None and hasattr(output, "value"):
        return output.value
    elif output is not None and isinstance(output, dict) and "value" in output:
        return output["value"]
    return default
