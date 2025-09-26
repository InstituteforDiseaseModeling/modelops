"""Unified infrastructure orchestration service."""

from typing import Dict, List, Optional, Any, Set
import os
import json
from datetime import datetime
from pathlib import Path

from .base import InfraResult, ComponentState, ComponentStatus
from .utils import (
    stack_exists,
    DependencyGraph
)
from .cluster_service import ClusterService
from .workspace_service import WorkspaceService
from .storage_service import StorageService
from .registry_service import RegistryService
from ..components.specs.infra import UnifiedInfraSpec


class InfrastructureService:
    """
    Unified infrastructure orchestration.

    This orchestrates the individual component services to provide
    a single-command infrastructure provisioning experience.
    """

    def __init__(self, env: Optional[str] = None):
        """
        Initialize infrastructure service.

        Args:
            env: Environment name (dev, staging, prod). Defaults to MODELOPS_ENV.
        """
        self.env = env or self._resolve_env()

        # Initialize individual services
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
        components: Optional[List[str]] = None,
        verbose: bool = False,
        force: bool = False,
        dry_run: bool = False
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
            logs_path=self._get_log_path()
        )

        # Check existing state for idempotency
        if not force:
            existing = self.get_component_states()
            components_to_provision = [
                c for c in components
                if existing.get(c) != ComponentState.READY
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
        for component in provision_order:
            print(f"\n→ Provisioning {component}...")

            try:
                outputs = None

                if component == "registry" and spec.registry:
                    # Registry needs Azure settings - inherit from cluster if available
                    registry_config = spec.registry.copy()
                    if spec.cluster:
                        # Handle both dict and AzureProviderConfig objects
                        cluster_dict = spec.cluster if isinstance(spec.cluster, dict) else spec.cluster.model_dump()
                        # Copy Azure settings from cluster config
                        for key in ["subscription_id", "location", "resource_group", "username"]:
                            if key in cluster_dict and key not in registry_config:
                                registry_config[key] = cluster_dict[key]

                    outputs = self.registry_service.create(
                        name=registry_config.get("name", "modelops-registry"),
                        config=registry_config,
                        verbose=verbose
                    )

                elif component == "cluster" and spec.cluster:
                    outputs = self.cluster_service.provision(
                        config=spec.cluster,
                        verbose=verbose
                    )

                elif component == "storage" and spec.storage:
                    # Storage should be standalone if cluster isn't being provisioned
                    standalone_storage = "cluster" not in components or not spec.cluster
                    outputs = self.storage_service.provision(
                        config=spec.storage,
                        standalone=standalone_storage,
                        verbose=verbose
                    )

                elif component == "workspace" and spec.workspace:
                    outputs = self.workspace_service.provision(
                        config=spec.workspace,
                        verbose=verbose
                    )
                else:
                    continue

                result.components[component] = ComponentState.READY
                result.outputs[component] = outputs or {}
                print(f"  ✓ {component} provisioned successfully")

            except Exception as e:
                result.components[component] = ComponentState.FAILED
                error_msg = str(e)
                result.errors[component] = error_msg
                result.success = False

                # Check for Pulumi lock error and provide helpful hint
                if "lock" in error_msg.lower() and "pulumi cancel" in error_msg.lower():
                    print(f"  ✗ {component} failed: Stack is locked by another process")
                    print(f"\n  Hint: Clear the lock with:")

                    # Extract stack name from error if possible
                    if "modelops-" in error_msg:
                        # Try to extract the stack name
                        import re
                        stack_match = re.search(r'(modelops-[a-z]+-[a-z]+)', error_msg)
                        if stack_match:
                            stack_name = stack_match.group(1)
                            print(f"    cd ~/.modelops/pulumi/{component} && pulumi cancel")
                            print(f"    # or: pulumi cancel -s organization/{stack_name}")
                    else:
                        print(f"    cd ~/.modelops/pulumi/{component} && pulumi cancel")

                    print(f"\n  Then retry the operation.")
                else:
                    # Show the full error for other cases
                    print(f"  ✗ {component} failed: {error_msg}")

                if not spec.continue_on_error:
                    break

        self._write_logs(result)

        # Save environment config if provisioning was successful
        if result.success and result.outputs:
            # Get resolved outputs (plain values) instead of Pulumi Output objects
            resolved_outputs = {}
            for component in result.outputs:
                try:
                    from ..core import automation
                    # Get the actual resolved values from the stack
                    component_outputs = automation.outputs(component, self.env, refresh=False)
                    if component_outputs:
                        resolved_outputs[component] = component_outputs
                except Exception:
                    # If we can't get resolved outputs, skip this component
                    pass

            if resolved_outputs:
                self._save_environment_config(resolved_outputs)

        return result

    def destroy(
        self,
        components: Optional[List[str]] = None,
        verbose: bool = False,
        force: bool = False,
        with_deps: bool = False,
        dry_run: bool = False,
        destroy_storage: bool = False,
        destroy_registry: bool = False,
        destroy_all: bool = False
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
                    d for d in dependents
                    if stack_exists(d, self.env) and d not in components
                ]
                if existing_dependents:
                    return InfraResult(
                        success=False,
                        components={},
                        outputs={},
                        errors={
                            comp: f"Has dependent components: {', '.join(existing_dependents)}"
                        }
                    )

        # Add warnings for destructive data operations
        if "storage" in components:
            print("\n⚠️  WARNING: Destroying storage will permanently delete all stored results and artifacts!")
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
            logs_path=self._get_log_path()
        )

        for component in destroy_order:
            print(f"\n→ Destroying {component}...")

            try:
                if component == "workspace":
                    self.workspace_service.destroy(verbose=verbose)
                elif component == "storage":
                    self.storage_service.destroy(verbose=verbose)
                elif component == "cluster":
                    self.cluster_service.destroy(verbose=verbose)
                elif component == "registry":
                    self.registry_service.destroy(verbose=verbose)

                result.components[component] = ComponentState.NOT_DEPLOYED
                print(f"  ✓ {component} destroyed")

            except Exception as e:
                result.errors[component] = str(e)
                result.success = False
                print(f"  ✗ Failed to destroy {component}: {e}")

        self._write_logs(result)

        # Update environment config after successful destruction
        if result.success:
            self._update_environment_config_after_destroy(destroy_order)

        return result

    def get_status(self) -> Dict[str, ComponentStatus]:
        """
        Get status of all infrastructure components.

        Returns:
            Dictionary mapping component to ComponentStatus
        """
        return {
            "cluster": self.cluster_service.status(),
            "registry": self.registry_service.status(),
            "storage": self.storage_service.status(),
            "workspace": self.workspace_service.status(),
        }

    def get_component_states(self) -> Dict[str, ComponentState]:
        """
        Get simplified component states.

        Returns:
            Dictionary mapping component to ComponentState
        """
        status = self.get_status()
        return {k: v.phase for k, v in status.items()}

    def get_outputs(
        self,
        component: Optional[str] = None,
        show_secrets: bool = False
    ) -> Dict[str, Any]:
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
            except:
                return {}

        # Get all outputs
        all_outputs = {}
        for comp in ["cluster", "registry", "storage", "workspace"]:
            try:
                outputs = automation.outputs(comp, self.env, refresh=False)
                if outputs:
                    all_outputs[comp] = get_safe_outputs(outputs, show_secrets)
            except:
                pass

        return all_outputs

    def preview(
        self,
        spec: UnifiedInfraSpec,
        components: Optional[List[str]] = None
    ) -> Dict[str, Any]:
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

        preview = {
            "to_create": [],
            "to_update": [],
            "no_change": []
        }

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
                Path(result.logs_path).write_text(json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "success": result.success,
                    "components": {k: v.value for k, v in result.components.items()},
                    "errors": result.errors
                }, indent=2))
            except:
                pass  # Don't fail on log writing errors

    def _save_environment_config(self, outputs: Dict[str, Any]):
        """Save environment configuration for modelops-bundle discovery.

        Args:
            outputs: Component outputs from provisioning
        """
        try:
            from ..core.env_config import save_environment_config

            # Helper to extract plain value from Pulumi Output or dict
            def extract_value(obj):
                """Extract plain value from various output formats."""
                if obj is None:
                    return None
                # Handle Pulumi Output objects
                if hasattr(obj, 'value'):
                    return obj.value
                # Handle dict with 'value' key (from automation.outputs)
                if isinstance(obj, dict) and 'value' in obj:
                    return obj['value']
                # Already a plain value
                return obj

            # Helper to extract all values from a dict
            def extract_all_values(output_dict):
                """Recursively extract plain values from output dict."""
                if not output_dict:
                    return None
                result = {}
                for key, value in output_dict.items():
                    extracted = extract_value(value)
                    # Handle nested structures like containers list
                    if extracted is not None and isinstance(extracted, list) and extracted and isinstance(extracted[0], dict):
                        # Keep the list structure for containers
                        result[key] = extracted
                    else:
                        result[key] = extracted
                return result

            # Extract registry outputs if present
            registry_plain = None
            if "registry" in outputs and outputs["registry"]:
                registry_plain = extract_all_values(outputs["registry"])

            # Extract storage outputs if present
            storage_plain = None
            if "storage" in outputs and outputs["storage"]:
                storage_plain = extract_all_values(outputs["storage"])

            # Save if we have either registry or storage
            if registry_plain or storage_plain:
                config_path = save_environment_config(
                    self.env,
                    registry_outputs=registry_plain,
                    storage_outputs=storage_plain
                )
                print(f"  ✓ Environment config saved to {config_path}")
        except Exception as e:
            # Don't fail provisioning if config save fails
            print(f"  ⚠ Could not save environment config: {e}")

    def _update_environment_config_after_destroy(self, destroyed_components: List[str]):
        """Update environment configuration after component destruction.

        Args:
            destroyed_components: List of components that were destroyed
        """
        try:
            from ..core.env_config import load_environment_config, save_environment_config

            # Try to load existing config
            try:
                existing_config = load_environment_config(self.env)
            except FileNotFoundError:
                # No config to update
                return

            # Check if config exists
            if not existing_config:
                return

            # Check what was destroyed
            destroyed_storage = "storage" in destroyed_components
            destroyed_registry = "registry" in destroyed_components

            # If both data components were destroyed, remove the entire config
            if destroyed_storage and destroyed_registry:
                config_path = Path.home() / ".modelops" / "environments" / f"{self.env}.yaml"
                if config_path.exists():
                    config_path.unlink()
                    print(f"  ✓ Removed environment config for {self.env}")
                return

            # Otherwise, update the config to remove destroyed components
            registry_outputs = None
            storage_outputs = None

            # Keep registry if it wasn't destroyed
            if not destroyed_registry and existing_config and existing_config.registry:
                registry_outputs = existing_config.registry.model_dump()

            # Keep storage if it wasn't destroyed
            if not destroyed_storage and existing_config and existing_config.storage:
                storage_outputs = existing_config.storage.model_dump()

            # Save updated config if there's anything left
            if registry_outputs or storage_outputs:
                config_path = save_environment_config(
                    self.env,
                    registry_outputs=registry_outputs,
                    storage_outputs=storage_outputs
                )
                print(f"  ✓ Updated environment config at {config_path}")
            else:
                # Nothing left, remove the config
                config_path = Path.home() / ".modelops" / "environments" / f"{self.env}.yaml"
                if config_path.exists():
                    config_path.unlink()
                    print(f"  ✓ Removed environment config for {self.env}")

        except Exception as e:
            # Don't fail destruction if config update fails
            print(f"  ⚠ Could not update environment config: {e}")
