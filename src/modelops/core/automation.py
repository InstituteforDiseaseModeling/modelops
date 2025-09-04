"""Pulumi Automation API helpers to reduce boilerplate across CLI commands.

This module provides simplified interfaces for common Pulumi operations,
eliminating repetitive code in CLI modules.
"""

from pathlib import Path
from typing import Any, Callable, Dict, Optional
import pulumi.automation as auto
from .paths import ensure_work_dir, get_backend_url
from .naming import StackNaming


def workspace_options(project: str, work_dir: Path) -> auto.LocalWorkspaceOptions:
    """Create standard LocalWorkspaceOptions for Pulumi operations.
    
    Args:
        project: Project name
        work_dir: Working directory for Pulumi operations
        
    Returns:
        Configured LocalWorkspaceOptions with backend settings
    """
    return auto.LocalWorkspaceOptions(
        work_dir=str(work_dir),
        project_settings=auto.ProjectSettings(
            name=project,
            runtime="python",
            backend=auto.ProjectBackend(url=get_backend_url()),
        ),
    )


def noop_program():
    """No-op Pulumi program for operations that only need stack access."""
    pass


def select_stack(
    component: str,
    env: str,
    run_id: Optional[str] = None,
    program: Optional[Callable] = None,
    work_dir: Optional[str] = None
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
    project = StackNaming.get_project_name(component)
    stack = StackNaming.get_stack_name(component, env, run_id)
    
    # Use provided work_dir or default based on component
    if work_dir is None:
        work_dir = ensure_work_dir(component)
    else:
        work_dir = Path(work_dir)
    
    return auto.create_or_select_stack(
        stack_name=stack,
        project_name=project,
        program=program or noop_program,
        opts=workspace_options(project, work_dir)
    )


def outputs(
    component: str,
    env: str,
    run_id: Optional[str] = None,
    refresh: bool = True,
    work_dir: Optional[str] = None
) -> Dict[str, Any]:
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
    stack = select_stack(component, env, run_id, noop_program, work_dir)
    if refresh:
        stack.refresh(on_output=lambda _: None)
    return stack.outputs()


def up(
    component: str,
    env: str,
    run_id: Optional[str],
    program: Callable,
    on_output: Optional[Callable[[str], None]] = None,
    work_dir: Optional[str] = None
) -> Dict[str, Any]:
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
    run_id: Optional[str] = None,
    on_output: Optional[Callable[[str], None]] = None,
    work_dir: Optional[str] = None
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


def get_output_value(outputs: Dict[str, Any], key: str, default: Any = None) -> Any:
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
    if output is not None and hasattr(output, 'value'):
        return output.value
    elif output is not None and isinstance(output, dict) and 'value' in output:
        return output['value']
    return default