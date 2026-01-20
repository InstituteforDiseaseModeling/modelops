"""Unified infrastructure orchestration service."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..components.specs.infra import UnifiedInfraSpec

logger = logging.getLogger(__name__)
from .base import ComponentState, ComponentStatus, InfraResult
from .cluster_service import ClusterService
from .registry_service import RegistryService
from .resource_group_service import ResourceGroupService
from .storage_service import StorageService
from .utils import DependencyGraph, stack_exists
from .workspace_service import WorkspaceService


class InfrastructureService:
    """
    Unified infrastructure orchestration.

    This orchestrates the individual component services to provide
    a single-command infrastructure provisioning experience.
    """

    def __init__(self, env: str | None = None):
        """
        Initialize infrastructure service.

        Args:
            env: Environment name (dev, staging, prod). Defaults to MODELOPS_ENV.
        """
        self.env = env or self._resolve_env()

        # Initialize individual services
        self.resource_group_service = ResourceGroupService(self.env)
        self.cluster_service = ClusterService(self.env)
        self.registry_service = RegistryService(self.env)
        self.storage_service = StorageService(self.env)
        self.workspace_service = WorkspaceService(self.env)

        # Initialize dependency graph
        self.dep_graph = DependencyGraph()

    def _resolve_env(self) -> str:
        """Resolve environment from various sources."""
        return os.environ.get("MODELOPS_ENV", "dev")

    def provision(
        self,
        spec: UnifiedInfraSpec,
        components: list[str] | None = None,
        verbose: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> InfraResult:
        """
        Provision infrastructure components.

        Args:
            spec: Unified infrastructure specification
            components: Specific components to provision (None = all)
            verbose: Show detailed output
            force: Force reprovisioning even if exists
            dry_run: Show what would be done without doing it

        Returns:
            InfraResult with status and outputs
        """
        # Validate spec
        spec.validate_dependencies()

        # Determine components to provision
        components = components or spec.get_components()

        result = InfraResult(
            success=True,
            components={},
            outputs={},
            errors={},
            logs_path=self._get_log_path(),
        )

        # Check existing state for idempotency
        if not force:
            existing = self.get_component_states()
            components_to_provision = [
                c for c in components if existing.get(c) != ComponentState.READY
            ]

            if not components_to_provision:
                print("✓ All requested components already deployed")
                for comp in components:
                    result.components[comp] = ComponentState.READY
                return result

            components = components_to_provision

        # Add explicit dependencies from spec
        if spec.depends_on:
            for comp, deps in spec.depends_on.items():
                for dep in deps:
                    self.dep_graph.add_dependency(comp, dep)

        # Get provision order
        try:
            provision_order = self.dep_graph.get_provision_order(components)
        except RuntimeError as e:
            result.success = False
            result.errors["dependency"] = str(e)
            return result

        if dry_run:
            print(f"Would provision in order: {' → '.join(provision_order)}")
            return result

        # Provision in dependency order
        logger.debug(f"provision_order: {provision_order}")
        logger.debug(f"spec.registry: {spec.registry}")
        logger.debug(f"spec.storage: {spec.storage}")
        logger.debug(f"spec.cluster: {spec.cluster}")
        logger.debug(f"spec.workspace: {spec.workspace}")

        for component in provision_order:
            print(f"\n→ Provisioning {component}...")

            try:
                outputs = None

                if component == "resource_group":
                    # Resource group is always needed for Azure components
                    # Extract config from cluster or use defaults
                    rg_config = {}
                    if spec.cluster:
                        cluster_dict = (
                            spec.cluster
                            if isinstance(spec.cluster, dict)
                            else spec.cluster.model_dump()
                        )
                        rg_config = {
                            "location": cluster_dict.get("location", "eastus2"),
                            "subscription_id": cluster_dict.get("subscription_id"),
                            "username": cluster_dict.get("username"),
                        }
                    else:
                        # Use defaults if no cluster config
                        rg_config = {
                            "location": "eastus2",
                            "subscription_id": os.environ.get("AZURE_SUBSCRIPTION_ID"),
                            "username": os.environ.get("USER"),
                        }

                    outputs = self.resource_group_service.provision(
                        config=rg_config, verbose=verbose
                    )

                elif component == "registry" and spec.registry:
                    logger.debug("Provisioning registry component...")
                    # Registry needs Azure settings - inherit from cluster if available
                    registry_config = spec.registry.copy()
                    if spec.cluster:
                        # Handle both dict and AzureProviderConfig objects
                        cluster_dict = (
                            spec.cluster
                            if isinstance(spec.cluster, dict)
                            else spec.cluster.model_dump()
                        )
                        # Copy Azure settings from cluster config
                        for key in [
                            "subscription_id",
                            "location",
                            "resource_group",
                            "username",
                        ]:
                            if key in cluster_dict and key not in registry_config:
                                registry_config[key] = cluster_dict[key]

                    outputs = self.registry_service.create(
                        name=registry_config.get("name", "modelops-registry"),
                        config=registry_config,
                        verbose=verbose,
                    )

                    logger.debug(
                        f"Registry outputs from create(): {json.dumps(outputs, indent=2, default=str)}"
                    )

                elif component == "cluster" and spec.cluster:
                    outputs = self.cluster_service.provision(config=spec.cluster, verbose=verbose)

                elif component == "storage" and spec.storage:
                    logger.debug("Provisioning storage component...")
                    # Storage should be standalone if cluster isn't being provisioned
                    standalone_storage = "cluster" not in components or not spec.cluster
                    outputs = self.storage_service.provision(
                        config=spec.storage,
                        standalone=standalone_storage,
                        verbose=verbose,
                    )

                elif component == "workspace" and spec.workspace:
                    outputs = self.workspace_service.provision(
                        config=spec.workspace, verbose=verbose
                    )
                else:
                    continue

                result.components[component] = ComponentState.READY
                result.outputs[component] = outputs or {}

                if verbose:
                    import json

                    print(
                        f"[DEBUG] Storing outputs for {component}: {json.dumps(outputs or {}, indent=2, default=str)}"
                    )

                # VALIDATION: Ensure critical components have outputs
                # Without outputs, dependent components and bundle operations will fail
                if component == "registry" and not outputs:
                    raise RuntimeError(
                        "Registry provisioned but has no outputs! "
                        "This prevents bundle push/pull operations. "
                        "Check if registry was actually created in Azure."
                    )
                if component == "storage" and not outputs:
                    raise RuntimeError(
                        "Storage provisioned but has no outputs! "
                        "This prevents bundle storage operations. "
                        "Check if storage account was actually created in Azure."
                    )

                print(f"  ✓ {component} provisioned successfully")

            except Exception as e:
                result.components[component] = ComponentState.FAILED
                error_msg = str(e)
                result.errors[component] = error_msg
                result.success = False

                # Check for Pulumi lock error and provide helpful hint
                if "lock" in error_msg.lower() and "pulumi cancel" in error_msg.lower():
                    print(f"  ✗ {component} failed: Stack is locked by another process")
                    print("\n  Hint: Clear the lock with:")

                    # Extract stack name from error if possible
                    if "modelops-" in error_msg:
                        # Try to extract the stack name
                        import re

                        stack_match = re.search(r"(modelops-[a-z]+-[a-z]+)", error_msg)
                        if stack_match:
                            stack_name = stack_match.group(1)
                            print(f"    cd ~/.modelops/pulumi/{component} && pulumi cancel")
                            print(f"    # or: pulumi cancel -s organization/{stack_name}")
                    else:
                        print(f"    cd ~/.modelops/pulumi/{component} && pulumi cancel")

                    print("\n  Then retry the operation.")
                else:
                    # Show the full error for other cases
                    print(f"  ✗ {component} failed: {error_msg}")

                if not spec.continue_on_error:
                    break

        self._write_logs(result)

        # Reconcile bundle environment file with Pulumi stack truth
        # This is the ONLY place that triggers bundle env writes/deletes
        from ..core.env_reconcile import reconcile_bundle_env

        path = reconcile_bundle_env(self.env, dry_run=dry_run)
        if path:
            print(f"  ✓ Bundle environment ready: {path}")
        elif not dry_run:
            print("  ℹ Bundle env needs both registry & storage")

        return result

    def destroy(
        self,
        components: list[str] | None = None,
        verbose: bool = False,
        force: bool = False,
        with_deps: bool = False,
        dry_run: bool = False,
        destroy_storage: bool = False,
        destroy_registry: bool = False,
        destroy_all: bool = False,
        delete_rg: bool = False,
        yes_confirmed: bool = False,
    ) -> InfraResult:
        """
        Destroy infrastructure components.

        Args:
            components: Specific components to destroy (None = compute only by default)
            verbose: Show detailed output
            force: Skip dependency checks
            with_deps: Also destroy dependent components
            dry_run: Show what would be done without doing it
            destroy_storage: Include storage in destruction
            destroy_registry: Include registry in destruction
            destroy_all: Destroy all components including data
            delete_rg: Also delete the resource group (dangerous!)
            yes_confirmed: User has confirmed via --yes flag

        Returns:
            InfraResult with status
        """
        # Build component list based on flags
        if components is None:
            # Default to compute resources only (safe by default)
            components = ["workspace", "cluster"]

            # Add data resources if explicitly requested
            if destroy_all:
                components.extend(["storage", "registry"])
            else:
                if destroy_storage:
                    components.append("storage")
                if destroy_registry:
                    components.append("registry")

            # Add resource group if explicitly requested
            # This will be destroyed LAST (after all other resources)
            if delete_rg:
                components.append("resource_group")

        # If with_deps, add all dependents
        if with_deps and not force:
            all_components = set(components)
            for comp in components:
                dependents = self.dep_graph.get_dependents(comp)
                all_components.update(dependents)
            components = list(all_components)

        # Check dependencies unless forced
        if not force and not with_deps:
            for comp in components:
                dependents = self.dep_graph.get_dependents(comp)
                existing_dependents = [
                    d for d in dependents if stack_exists(d, self.env) and d not in components
                ]
                if existing_dependents:
                    return InfraResult(
                        success=False,
                        components={},
                        outputs={},
                        errors={
                            comp: f"Has dependent components: {', '.join(existing_dependents)}"
                        },
                    )

        # Add warnings for destructive data operations
        if "storage" in components:
            print(
                "\n⚠️  WARNING: Destroying storage will permanently delete all stored results and artifacts!"
            )
        if "registry" in components:
            print("⚠️  WARNING: Destroying registry will permanently delete all container images!")

        # Get destroy order (reverse of provision order)
        destroy_order = self.dep_graph.get_destroy_order(components)

        if dry_run:
            print(f"Would destroy in order: {' → '.join(destroy_order)}")
            return InfraResult(success=True, components={}, outputs={}, errors={})

        result = InfraResult(
            success=True,
            components={},
            outputs={},
            errors={},
            logs_path=self._get_log_path(),
        )

        for component in destroy_order:
            print(f"\n→ Destroying {component}...")

            try:
                if component == "workspace":
                    self.workspace_service.destroy(verbose=verbose)
                elif component == "storage":
                    self.storage_service.destroy(verbose=verbose)
                elif component == "cluster":
                    self.cluster_service.destroy(force=force, verbose=verbose)
                elif component == "registry":
                    self.registry_service.destroy(verbose=verbose)
                elif component == "resource_group":
                    # Resource group must be destroyed last after all resources
                    self.resource_group_service.destroy(verbose=verbose)

                result.components[component] = ComponentState.NOT_DEPLOYED
                print(f"  ✓ {component} destroyed")

            except Exception as e:
                result.errors[component] = str(e)
                result.success = False
                print(f"  ✗ Failed to destroy {component}: {e}")

        self._write_logs(result)

        # Reconcile bundle environment after destroy to remove stale file
        if result.success:
            from ..core.env_reconcile import reconcile_bundle_env

            reconcile_bundle_env(self.env, dry_run=dry_run)

            # If destroy-all was specified, also remove the empty Pulumi stacks
            # This prevents issues when users later try to provision again
            if destroy_all and not dry_run:
                print("\n→ Removing empty Pulumi stacks for clean slate...")
                from ..core import automation

                # Remove stacks in reverse order of dependencies
                stacks_to_remove = [
                    "workspace",
                    "storage",
                    "cluster",
                    "registry",
                    "resource_group",
                ]
                for stack_name in stacks_to_remove:
                    try:
                        # Check if stack exists first
                        if stack_exists(stack_name, self.env):
                            automation.remove_stack(stack_name, self.env)
                            print(f"  ✓ Removed {stack_name} stack")
                    except Exception as e:
                        # Don't fail destroy if stack removal fails
                        print(f"  ⚠ Could not remove {stack_name} stack: {e}")

        return result

    def get_status(self) -> dict[str, ComponentStatus]:
        """
        Get status of all infrastructure components.

        Returns:
            Dictionary mapping component to ComponentStatus
        """
        return {
            "resource_group": self.resource_group_service.status(),
            "cluster": self.cluster_service.status(),
            "registry": self.registry_service.status(),
            "storage": self.storage_service.status(),
            "workspace": self.workspace_service.status(),
        }

    def get_component_states(self) -> dict[str, ComponentState]:
        """
        Get simplified component states.

        Returns:
            Dictionary mapping component to ComponentState
        """
        status = self.get_status()
        return {k: v.phase for k, v in status.items()}

    def get_outputs(
        self, component: str | None = None, show_secrets: bool = False
    ) -> dict[str, Any]:
        """
        Get outputs for components.

        Args:
            component: Specific component or None for all
            show_secrets: Show secret values

        Returns:
            Dictionary of outputs
        """
        from ..core import automation
        from .utils import get_safe_outputs

        if component:
            try:
                outputs = automation.outputs(component, self.env, refresh=False)
                return get_safe_outputs(outputs, show_secrets) if outputs else {}
            except Exception:
                return {}

        # Get all outputs
        all_outputs = {}
        for comp in ["cluster", "registry", "storage", "workspace"]:
            try:
                outputs = automation.outputs(comp, self.env, refresh=False)
                if outputs:
                    all_outputs[comp] = get_safe_outputs(outputs, show_secrets)
            except Exception:
                pass

        return all_outputs

    def preview(
        self, spec: UnifiedInfraSpec, components: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Preview changes without applying.

        Args:
            spec: Infrastructure specification
            components: Specific components to preview

        Returns:
            Preview results
        """
        # This would call Pulumi preview for each component
        # For now, just return what would be done
        components = components or spec.get_components()
        existing = self.get_component_states()

        preview = {"to_create": [], "to_update": [], "no_change": []}

        for comp in components:
            state = existing.get(comp, ComponentState.NOT_DEPLOYED)
            if state == ComponentState.NOT_DEPLOYED:
                preview["to_create"].append(comp)
            elif state == ComponentState.READY:
                preview["no_change"].append(comp)
            else:
                preview["to_update"].append(comp)

        return preview

    def _get_log_path(self) -> str:
        """Get path for operation logs."""
        logs_dir = Path.home() / ".modelops" / "logs" / self.env
        logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(logs_dir / f"infra_{timestamp}.log")

    def _write_logs(self, result: InfraResult):
        """Write operation logs to file."""
        if result.logs_path:
            try:
                Path(result.logs_path).write_text(
                    json.dumps(
                        {
                            "timestamp": datetime.now().isoformat(),
                            "success": result.success,
                            "components": {k: v.value for k, v in result.components.items()},
                            "errors": result.errors,
                        },
                        indent=2,
                    )
                )
            except (OSError, IOError):
                pass  # Don't fail on log writing errors
