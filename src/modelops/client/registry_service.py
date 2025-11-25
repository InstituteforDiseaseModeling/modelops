"""Service for container registry management."""

import subprocess
from typing import Any

from ..core import StackNaming, automation
from ..core.automation import get_output_value
from ..core.state_manager import PulumiStateManager
from .base import BaseService, ComponentState, ComponentStatus, OutputCapture
from .utils import stack_exists


class RegistryService(BaseService):
    """Service for container registry management."""

    def __init__(self, env: str):
        """Initialize registry service."""
        super().__init__(env)

    def provision(self, config: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
        """
        Provision registry using unified contract.

        Args:
            config: Registry configuration
            verbose: Show detailed output

        Returns:
            Stack outputs

        Raises:
            Exception: If provisioning fails
        """
        # The provision method just delegates to create for backward compatibility
        return self.create(config.get("name", "modelops-registry"), config, verbose)

    def create(
        self,
        name: str = "modelops-registry",
        config: dict[str, Any] | None = None,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """
        Create container registry.

        Args:
            name: Registry name
            config: Registry configuration
            verbose: Show detailed output

        Returns:
            Stack outputs

        Raises:
            Exception: If creation fails
        """
        config = config or {}

        def pulumi_program():
            """Create ContainerRegistry in registry stack context."""
            import pulumi

            from ..infra.components.registry import ContainerRegistry

            # Add environment to config
            registry_config = config.copy()
            registry_config["environment"] = self.env

            # Create the registry component
            registry = ContainerRegistry(name, registry_config)

            # Security: Grant AKS cluster ACR pull permissions to access private images
            # Without this, pods fail with ImagePullBackOff for private registry images
            if registry_config.get("grant_cluster_pull", True) and stack_exists("infra", self.env):
                try:
                    infra_ref = StackNaming.ref("infra", self.env)
                    role_assignment = registry.setup_cluster_pull_permissions(infra_ref)
                    if role_assignment:
                        pulumi.export("cluster_pull_configured", pulumi.Output.from_input(True))
                except:
                    pulumi.export("cluster_pull_configured", pulumi.Output.from_input(False))

            # Export outputs at stack level for StackReference access
            pulumi.export("login_server", registry.login_server)
            pulumi.export("registry_name", registry.registry_name)
            pulumi.export("provider", pulumi.Output.from_input(config.get("provider", "azure")))
            pulumi.export("requires_auth", registry.requires_auth)
            # CRITICAL: Export registry_id for cluster to use when granting ACR permissions
            # This allows cluster to reference the actual Azure resource ID, not compute it
            if hasattr(registry, "registry_id") and registry.registry_id:
                pulumi.export("registry_id", registry.registry_id)

            # Export bundle credentials if available (needed by workspace)
            if hasattr(registry, "bundles_pull_username") and registry.bundles_pull_username:
                pulumi.export("bundles_pull_username", registry.bundles_pull_username)
            if hasattr(registry, "bundles_pull_password") and registry.bundles_pull_password:
                pulumi.export("bundles_pull_password", registry.bundles_pull_password)
            if hasattr(registry, "bundle_repo") and registry.bundle_repo:
                pulumi.export("bundle_repo", registry.bundle_repo)

            return registry

        # Use PulumiStateManager for automatic lock recovery and state management
        state_manager = PulumiStateManager("registry", self.env)
        capture = OutputCapture(verbose)

        # State manager handles:
        # - Stale lock detection and clearing
        # - State reconciliation with Azure
        # - Environment YAML updates
        result = state_manager.execute_with_recovery(
            "up", program=pulumi_program, on_output=capture
        )

        return result.outputs if result else {}

    def destroy(self, verbose: bool = False) -> None:
        """
        Destroy registry.

        Args:
            verbose: Show detailed output

        Raises:
            Exception: If destruction fails
        """
        # Use PulumiStateManager for automatic lock recovery and cleanup
        state_manager = PulumiStateManager("registry", self.env)
        capture = OutputCapture(verbose)

        # State manager handles:
        # - Stale lock detection and clearing
        # - Environment YAML cleanup
        state_manager.execute_with_recovery("destroy", on_output=capture)

    def status(self) -> ComponentStatus:
        """
        Get registry status with unified contract.

        Returns:
            ComponentStatus with registry details
        """
        try:
            outputs = automation.outputs("registry", self.env, refresh=False)

            if outputs:
                # Get last update timestamp from Pulumi stack
                last_update = self._get_stack_last_update("registry")

                return ComponentStatus(
                    deployed=True,
                    phase=ComponentState.READY,
                    details={
                        "login_server": get_output_value(outputs, "login_server", "unknown"),
                        "registry_name": get_output_value(outputs, "registry_name", "unknown"),
                        "provider": get_output_value(outputs, "provider", "unknown"),
                        "requires_auth": get_output_value(outputs, "requires_auth", False),
                        "cluster_pull_configured": get_output_value(
                            outputs, "cluster_pull_configured", False
                        ),
                    },
                    last_update=last_update,
                )
            else:
                return ComponentStatus(
                    deployed=False, phase=ComponentState.NOT_DEPLOYED, details={}
                )
        except Exception as e:
            return ComponentStatus(
                deployed=False, phase=ComponentState.UNKNOWN, details={"error": str(e)}
            )

    def login(self) -> bool:
        """
        Login to container registry.

        Returns:
            True if login successful

        Raises:
            Exception: If login fails
        """
        outputs = automation.outputs("registry", self.env, refresh=False)

        if not outputs:
            raise Exception("Registry not found")

        provider = get_output_value(outputs, "provider")

        if provider == "azure":
            registry_name = get_output_value(outputs, "registry_name")
            if not registry_name:
                raise Exception("Registry name not found")

            # Run az acr login
            result = subprocess.run(
                ["az", "acr", "login", "--name", registry_name],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                raise Exception(f"Login failed: {result.stderr}")

            return True
        else:
            login_server = get_output_value(outputs, "login_server")
            raise Exception(
                f"Manual login required for {provider} registry: docker login {login_server}"
            )

    def get_env_vars(self, format: str = "bash") -> str:
        """
        Get registry configuration as environment variables.

        Args:
            format: Output format (bash, json, make)

        Returns:
            Formatted environment variables
        """
        outputs = automation.outputs("registry", self.env, refresh=False)

        if not outputs:
            if format == "json":
                return "{}"
            return ""

        # Extract values
        login_server = get_output_value(outputs, "login_server")
        registry_name = get_output_value(outputs, "registry_name")
        provider = get_output_value(outputs, "provider")

        if format == "bash":
            # Output as shell export statements
            result = []
            if login_server:
                result.append(f"export MODELOPS_REGISTRY_SERVER={login_server}")
            if registry_name:
                result.append(f"export MODELOPS_REGISTRY_NAME={registry_name}")
            if provider:
                result.append(f"export MODELOPS_REGISTRY_PROVIDER={provider}")
            return "\n".join(result)

        elif format == "make":
            # Output as Makefile variables
            result = []
            if login_server:
                result.append(f"MODELOPS_REGISTRY_SERVER={login_server}")
            if registry_name:
                result.append(f"MODELOPS_REGISTRY_NAME={registry_name}")
            if provider:
                result.append(f"MODELOPS_REGISTRY_PROVIDER={provider}")
            return "\n".join(result)

        elif format == "json":
            # Output as JSON
            import json

            data = {}
            if login_server:
                data["MODELOPS_REGISTRY_SERVER"] = login_server
            if registry_name:
                data["MODELOPS_REGISTRY_NAME"] = registry_name
            if provider:
                data["MODELOPS_REGISTRY_PROVIDER"] = provider
            return json.dumps(data, indent=2)

        else:
            raise ValueError(f"Unknown format: {format}")

    def wire_permissions(self, infra_stack: str | None = None) -> bool:
        """
        Wire ACR pull permissions for AKS cluster.

        Args:
            infra_stack: Infrastructure stack name

        Returns:
            True if successful

        Raises:
            Exception: If wiring fails
        """
        # This would update the registry stack to add role assignment
        # For now, just provide manual instructions
        registry_stack = StackNaming.get_stack_name("registry", self.env)
        infra_stack = infra_stack or StackNaming.get_stack_name("cluster", self.env)

        print("Wiring registry permissions...")
        print(f"  Registry stack: {registry_stack}")
        print(f"  Infrastructure stack: {infra_stack}")

        # TODO: Implement actual permission wiring
        print("\nManual steps for now:")
        print("1. Get the AKS cluster's kubelet identity:")
        print(
            "   az aks show -n <cluster> -g <rg> --query identityProfile.kubeletidentity.objectId"
        )
        print("2. Grant ACR pull permissions:")
        print("   az role assignment create --assignee <identity> --role acrpull --scope <acr-id>")

        return False  # Not implemented yet
