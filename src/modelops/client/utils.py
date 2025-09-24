"""Utilities for service layer."""

from typing import Dict, List, Set, Optional, Any, Iterable
import pulumi.automation as auto
from pathlib import Path

from ..core import StackNaming
from ..core.paths import WORK_DIRS, get_backend_url
from ..core.automation import workspace_options, get_output_value


def stack_exists(component: str, env: str) -> bool:
    """
    Check if a stack exists without broad exception handling.

    Args:
        component: Component name (cluster, storage, workspace, registry)
        env: Environment name

    Returns:
        True if stack exists, False otherwise
    """
    if component not in WORK_DIRS:
        return False

    try:
        work_dir = WORK_DIRS[component]
        project_name = StackNaming.get_project_name(component)
        stack_name = StackNaming.get_stack_name(component, env)

        # Create workspace to check stack
        ws = auto.LocalWorkspace(
            **workspace_options(project_name, work_dir).__dict__
        )

        # Try to get the stack
        try:
            stack = ws.select_stack(stack_name)
            # Stack exists if we can select it
            return stack is not None
        except auto.errors.StackNotFoundError:
            return False

    except Exception as e:
        # Log for debugging but treat as not exists
        print(f"Warning: Error checking stack {component}-{env}: {e}")
        return False




def mask_secret_value(value: Any, mask: str = "****") -> Any:
    """
    Mask secret values for display.

    Args:
        value: Value to potentially mask
        mask: Mask string to use

    Returns:
        Masked value if secret, original otherwise
    """
    # Check if value looks like a secret
    if isinstance(value, str):
        # Common patterns for secrets
        secret_patterns = [
            "password",
            "secret",
            "key",
            "token",
            "connection",
            "credential",
            "auth",
        ]

        # Check if the value or its context suggests it's a secret
        value_lower = value.lower()
        for pattern in secret_patterns:
            if pattern in value_lower and len(value) > 10:
                return mask

    # Check for Pulumi Output with secret bit
    if hasattr(value, "__class__") and "Output" in str(value.__class__):
        # This is a Pulumi Output, check if it's marked secret
        # Note: This is a simplified check; real implementation would
        # need to handle Pulumi's Output type properly
        return mask

    return value


def get_safe_outputs(
    outputs: Dict[str, Any],
    show_secrets: bool = False
) -> Dict[str, Any]:
    """
    Get outputs with secrets masked unless explicitly requested.

    Args:
        outputs: Raw outputs dictionary
        show_secrets: If True, show secret values

    Returns:
        Dictionary with secrets potentially masked
    """
    if show_secrets:
        return outputs

    safe_outputs = {}
    for key, value in outputs.items():
        # Check if key suggests it's a secret
        key_lower = key.lower()
        secret_keys = [
            "password",
            "secret",
            "key",
            "token",
            "connection_string",
            "credential",
            "kubeconfig",  # Kubeconfig contains sensitive cluster access
        ]

        if any(s in key_lower for s in secret_keys):
            safe_outputs[key] = "****"
        else:
            safe_outputs[key] = value

    return safe_outputs


class DependencyGraph:
    """Manages component dependencies for infrastructure."""

    def __init__(self, dependencies: Optional[Dict[str, Set[str]]] = None):
        """
        Initialize dependency graph.

        Args:
            dependencies: Optional explicit dependencies
        """
        self.dependencies = dependencies or {
            "workspace": {"cluster", "storage"},
            "storage": set(),
            "registry": set(),
            "cluster": set(),
        }

    def add_dependency(self, component: str, depends_on: str):
        """Add a dependency relationship."""
        if component not in self.dependencies:
            self.dependencies[component] = set()
        self.dependencies[component].add(depends_on)

    def remove_dependency(self, component: str, depends_on: str):
        """Remove a dependency relationship."""
        if component in self.dependencies:
            self.dependencies[component].discard(depends_on)

    def get_provision_order(self, components: List[str]) -> List[str]:
        """Get provision order for components using topological sort.

        Args:
            components: List of components to provision

        Returns:
            List of components in provision order (dependencies first)

        Raises:
            RuntimeError: If circular dependencies detected
        """
        # Build graph of components we care about
        graph = {}
        for comp in components:
            graph[comp] = self.dependencies.get(comp, set()).intersection(components)

        # Topological sort using Kahn's algorithm
        result = []
        in_degree = {comp: 0 for comp in graph}

        # Calculate in-degrees
        for comp in graph:
            for dep in graph[comp]:
                in_degree[dep] = in_degree.get(dep, 0) + 1

        # Find nodes with no incoming edges
        queue = [comp for comp, degree in in_degree.items() if degree == 0]

        while queue:
            # Sort for deterministic order
            queue.sort()
            current = queue.pop(0)
            result.append(current)

            # Remove edges from current
            for neighbor in graph.get(current, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # Check for cycles
        if len(result) != len(graph):
            missing = set(graph.keys()) - set(result)
            raise RuntimeError(f"Circular dependencies detected among: {missing}")

        # Reverse to get provision order (dependencies first)
        return list(reversed(result))

    def get_destroy_order(self, components: List[str]) -> List[str]:
        """Get destroy order for components (reverse of provision order).

        Args:
            components: List of components to destroy

        Returns:
            List of components in destroy order (dependents first)
        """
        return list(reversed(self.get_provision_order(components)))

    def get_dependencies(self, component: str) -> Set[str]:
        """Get direct dependencies of a component."""
        return self.dependencies.get(component, set())

    def get_dependents(self, component: str) -> Set[str]:
        """Get components that depend on this component."""
        dependents = set()
        for comp, deps in self.dependencies.items():
            if component in deps:
                dependents.add(comp)
        return dependents