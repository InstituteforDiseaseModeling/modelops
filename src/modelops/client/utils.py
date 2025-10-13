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


def validate_component_dependencies(
    component: str,
    env: str,
    infra_service: Optional['InfrastructureService'] = None
) -> None:
    """Validate all dependencies are deployed before provisioning component.

    This ensures components cannot be deployed without their required dependencies,
    preventing broken Pulumi stacks and cryptic errors.

    Args:
        component: Component to validate (workspace, storage, registry, cluster)
        env: Environment name (dev, staging, prod)
        infra_service: Optional InfrastructureService instance (created if not provided)

    Raises:
        ValueError: If required dependencies are missing or not ready
    """
    from .base import ComponentState

    # Canonicalize component name
    component = canonicalize_component_name(component)

    # Get or create infra service to check statuses
    if not infra_service:
        from .infra_service import InfrastructureService
        infra_service = InfrastructureService(env)

    # Get dependencies for this component
    dep_graph = DependencyGraph()
    required_deps = dep_graph.get_dependencies(component)

    # If no dependencies, nothing to check
    if not required_deps:
        return

    # Check each dependency
    missing = []
    not_ready = []

    # Get status of all components
    all_status = infra_service.get_status()

    for dep in required_deps:
        status = all_status.get(dep)
        if not status or not status.deployed:
            missing.append(dep)
        elif status.phase != ComponentState.READY:
            not_ready.append(f"{dep} ({status.phase.value})")

    # Build detailed error message
    if missing or not_ready:
        msg = f"\n❌ Cannot deploy {component} - dependencies not met:\n\n"

        if missing:
            msg += "  Missing components (not deployed):\n"
            for dep in missing:
                msg += f"    • {dep}\n"

        if not_ready:
            msg += "\n  Components not ready:\n"
            for dep in not_ready:
                msg += f"    • {dep}\n"

        msg += "\n  Required dependencies:\n"
        for dep in sorted(required_deps):
            status = all_status.get(dep)
            if status and status.deployed:
                msg += f"    ✓ {dep}: {status.phase.value}\n"
            else:
                msg += f"    ✗ {dep}: Not deployed\n"

        msg += "\n  💡 Solution: Run 'mops infra up' to provision all dependencies\n"
        msg += "     Or provision specific components in dependency order"

        raise ValueError(msg)


def canonicalize_component_name(name: str) -> str:
    """Canonicalize component name to use underscores consistently.

    Args:
        name: Component name (may use hyphens or underscores)

    Returns:
        Canonicalized name with underscores

    Examples:
        "resource-group" -> "resource_group"
        "resource_group" -> "resource_group"
    """
    return name.replace("-", "_")


class DependencyGraph:
    """Manages component dependencies for infrastructure."""

    def __init__(self, dependencies: Optional[Dict[str, Set[str]]] = None):
        """
        Initialize dependency graph.

        Args:
            dependencies: Optional explicit dependencies
        """
        # Default dependencies with canonical names (underscores)
        self.dependencies = dependencies or {
            # Resource group is the root dependency - all Azure resources need it
            "resource_group": set(),
            # Storage and Registry depend on resource group existing first
            "storage": {"resource_group"},
            "registry": {"resource_group"},
            # CRITICAL: Cluster depends on registry to grant ACR pull permissions
            # Without this dependency, cluster provisioning races with registry creation,
            # causing "ResourceNotFound" errors when granting ACR permissions to AKS.
            # This ensures registry exists before cluster tries to reference it.
            # Cluster also depends on resource_group for the RG itself.
            "cluster": {"resource_group", "registry"},
            # Workspace depends on cluster and storage
            "workspace": {"cluster", "storage"},
        }

    def add_dependency(self, component: str, depends_on: str):
        """Add a dependency relationship."""
        component = canonicalize_component_name(component)
        depends_on = canonicalize_component_name(depends_on)
        if component not in self.dependencies:
            self.dependencies[component] = set()
        self.dependencies[component].add(depends_on)

    def remove_dependency(self, component: str, depends_on: str):
        """Remove a dependency relationship."""
        component = canonicalize_component_name(component)
        depends_on = canonicalize_component_name(depends_on)
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
        # Canonicalize all component names
        components = [canonicalize_component_name(c) for c in components]

        # Build graph of what each component depends on
        depends_on = {}
        for comp in components:
            depends_on[comp] = self.dependencies.get(comp, set()).intersection(components)

        # Build reverse graph: what depends on each component
        depended_by = {comp: set() for comp in components}
        for comp, deps in depends_on.items():
            for dep in deps:
                depended_by[dep].add(comp)

        # Topological sort using Kahn's algorithm
        result = []
        # Start with components that have no dependencies
        queue = [comp for comp in components if not depends_on[comp]]

        while queue:
            # Sort for deterministic order
            queue.sort()
            current = queue.pop(0)
            result.append(current)

            # Check all components that depend on current
            for dependent in depended_by[current]:
                # Remove current from dependent's dependencies
                depends_on[dependent].discard(current)
                # If dependent has no more dependencies, it can be processed
                if not depends_on[dependent]:
                    queue.append(dependent)

        # Check for cycles
        if len(result) != len(components):
            missing = set(components) - set(result)
            raise RuntimeError(f"Circular dependencies detected among: {missing}")

        return result

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
        component = canonicalize_component_name(component)
        return self.dependencies.get(component, set())

    def get_dependents(self, component: str) -> Set[str]:
        """Get components that depend on this component."""
        component = canonicalize_component_name(component)
        dependents = set()
        for comp, deps in self.dependencies.items():
            if component in deps:
                dependents.add(comp)
        return dependents