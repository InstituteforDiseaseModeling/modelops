"""Service for Azure Resource Group management."""

from typing import Dict, Any, Optional

from .base import BaseService, ComponentStatus, ComponentState, OutputCapture
from .utils import stack_exists
from ..core import StackNaming, automation
from ..core.automation import get_output_value
from ..core.paths import ensure_work_dir
from ..core.state_manager import PulumiStateManager


class ResourceGroupService(BaseService):
    """Service for Azure Resource Group management - the root dependency."""

    def __init__(self, env: str):
        """Initialize resource group service."""
        super().__init__(env)

    def provision(
        self,
        config: Dict[str, Any],
        verbose: bool = False
    ) -> Dict[str, Any]:
        """
        Provision resource group.

        Args:
            config: Resource group configuration containing:
                - location: Azure region (default: eastus2)
                - subscription_id: Azure subscription ID
                - username: Optional username for per-user isolation
            verbose: Show detailed output

        Returns:
            Stack outputs with resource group details

        Raises:
            Exception: If provisioning fails
        """
        def pulumi_program():
            """Create ResourceGroup component."""
            from ..infra.components.resource_group import ResourceGroup
            import pulumi
            # import os
            # import hashlib
            # from pathlib import Path

            # Add environment to config
            rg_config = config.copy()
            rg_config["environment"] = self.env

            # Create resource group
            rg = ResourceGroup("resource-group", rg_config)

            # Export outputs at stack level for visibility
            pulumi.export("resource_group_name", rg.resource_group_name)
            pulumi.export("resource_group_id", rg.resource_group_id)
            pulumi.export("location", rg.location)
            pulumi.export("environment", self.env)

            # INSTRUMENTATION: Export diagnostic info
            # passphrase_file = Path.home() / ".modelops" / "secrets" / "pulumi-passphrase"
            # if passphrase_file.exists():
            #     content = passphrase_file.read_text().strip()
            #     hash_val = hashlib.sha256(content.encode()).hexdigest()[:8]
            # else:
            #     hash_val = "NO_FILE"

            # pulumi.export("diag_pass_hash", hash_val)
            # pulumi.export("diag_pass_file", os.environ.get("PULUMI_CONFIG_PASSPHRASE_FILE", "NOT_SET"))
            # pulumi.export("diag_pass_env_set", "SET" if os.environ.get("PULUMI_CONFIG_PASSPHRASE") else "NOT_SET")
            # pulumi.export("diag_secrets_provider", os.environ.get("PULUMI_SECRETS_PROVIDER", "NOT_SET"))

            return rg

        # Use PulumiStateManager for automatic lock recovery and state management
        state_manager = PulumiStateManager("resource-group", self.env)
        capture = OutputCapture(verbose)

        # State manager handles:
        # - Stale lock detection and clearing
        # - State reconciliation with Azure
        # - Environment YAML updates (though RG doesn't need env YAML)
        result = state_manager.execute_with_recovery(
            "up",
            program=pulumi_program,
            on_output=capture
        )

        return result.outputs if result else {}

    def destroy(self, verbose: bool = False) -> None:
        """
        Destroy resource group.

        WARNING: This will destroy ALL resources in the resource group!
        Use with extreme caution.

        Args:
            verbose: Show detailed output

        Raises:
            Exception: If destruction fails
        """
        # Destroy using PulumiStateManager
        state_manager = PulumiStateManager("resource-group", self.env)
        capture = OutputCapture(verbose)

        # State manager handles:
        # - Stale lock detection and clearing
        # - Environment YAML cleanup
        state_manager.execute_with_recovery(
            "destroy",
            on_output=capture
        )

    def status(self) -> ComponentStatus:
        """
        Get resource group status.

        Returns:
            ComponentStatus with resource group details
        """
        try:
            work_dir = ensure_work_dir("resource-group")
            outputs = automation.outputs(
                "resource-group", self.env, refresh=False, work_dir=str(work_dir)
            )

            if outputs:
                return ComponentStatus(
                    deployed=True,
                    phase=ComponentState.READY,
                    details={
                        "resource_group_name": get_output_value(outputs, "resource_group_name", "unknown"),
                        "resource_group_id": get_output_value(outputs, "resource_group_id", "unknown"),
                        "location": get_output_value(outputs, "location", "unknown"),
                        "environment": get_output_value(outputs, "environment", self.env)
                    }
                )
            else:
                return ComponentStatus(
                    deployed=False,
                    phase=ComponentState.NOT_DEPLOYED,
                    details={}
                )
        except Exception as e:
            return ComponentStatus(
                deployed=False,
                phase=ComponentState.FAILED,
                details={"error": str(e)}
            )