"""Service for blob storage management."""

import os
from typing import Any

from ..components.specs.storage import StorageConfig
from ..core import StackNaming, automation
from ..core.automation import get_output_value
from ..core.paths import ensure_work_dir
from ..core.state_manager import PulumiStateManager
from .base import BaseService, ComponentState, ComponentStatus, OutputCapture
from .utils import stack_exists


class StorageService(BaseService):
    """Service for blob storage management."""

    def __init__(self, env: str):
        """Initialize storage service."""
        super().__init__(env)

    def provision(
        self, config: StorageConfig, standalone: bool = False, verbose: bool = False
    ) -> dict[str, Any]:
        """
        Provision blob storage.

        Args:
            config: Storage configuration
            standalone: If True, don't reference infrastructure stack
            verbose: Show detailed output

        Returns:
            Stack outputs

        Raises:
            Exception: If provisioning fails
        """

        def pulumi_program():
            """Create BlobStorage in standalone or integrated mode."""
            import pulumi

            from ..infra.components.storage import BlobStorage

            # Convert config to dict
            storage_config = config.to_pulumi_config()
            storage_config["environment"] = self.env

            if standalone:
                # Standalone deployment
                storage = BlobStorage("storage", storage_config)
            else:
                # Integrated with infrastructure - reference cluster stack if it exists
                infra_ref = None
                if stack_exists("infra", self.env):
                    infra_ref = StackNaming.ref("infra", self.env)

                if infra_ref:
                    storage = BlobStorage("storage", storage_config, infra_stack_ref=infra_ref)
                else:
                    storage = BlobStorage("storage", storage_config)

            # Export outputs at stack level for visibility
            pulumi.export("account_name", storage.account_name)
            pulumi.export("resource_group", storage.resource_group)
            pulumi.export("connection_string", storage.connection_string)
            pulumi.export("primary_endpoint", storage.primary_endpoint)
            pulumi.export("containers", storage_config.get("containers", []))
            pulumi.export("location", storage_config.get("location", "eastus2"))
            pulumi.export("environment", self.env)

            return storage

        # Use PulumiStateManager for automatic lock recovery and state management
        state_manager = PulumiStateManager("storage", self.env)
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
        Destroy storage.

        Args:
            verbose: Show detailed output

        Raises:
            Exception: If destruction fails
        """
        import os

        # Allow deletion of K8s resources even if cluster is unreachable
        os.environ["PULUMI_K8S_DELETE_UNREACHABLE"] = "true"

        # Use PulumiStateManager for automatic lock recovery and cleanup
        state_manager = PulumiStateManager("storage", self.env)
        capture = OutputCapture(verbose)

        # State manager handles:
        # - Stale lock detection and clearing
        # - Environment YAML cleanup
        state_manager.execute_with_recovery("destroy", on_output=capture)

    def status(self) -> ComponentStatus:
        """
        Get storage status with unified contract.

        Returns:
            ComponentStatus with storage details
        """
        try:
            work_dir = ensure_work_dir("storage")
            outputs = automation.outputs("storage", self.env, refresh=False, work_dir=str(work_dir))

            if outputs:
                containers = get_output_value(outputs, "containers", [])
                # Extract container names from list of dicts
                if containers and isinstance(containers[0], dict):
                    container_names = [c.get("name", "unnamed") for c in containers]
                else:
                    container_names = containers

                return ComponentStatus(
                    deployed=True,
                    phase=ComponentState.READY,
                    details={
                        "account_name": get_output_value(outputs, "account_name", "unknown"),
                        "resource_group": get_output_value(outputs, "resource_group", "unknown"),
                        "location": get_output_value(outputs, "location", "unknown"),
                        "endpoint": get_output_value(outputs, "primary_endpoint"),
                        "containers": container_names,
                        "container_count": len(containers),
                    },
                )
            else:
                return ComponentStatus(
                    deployed=False, phase=ComponentState.NOT_DEPLOYED, details={}
                )
        except Exception as e:
            return ComponentStatus(
                deployed=False, phase=ComponentState.UNKNOWN, details={"error": str(e)}
            )

    def get_connection_string(self, show_secrets: bool = False) -> str | None:
        """
        Get storage connection string.

        Args:
            show_secrets: If True, show actual connection string

        Returns:
            Connection string or None
        """
        try:
            work_dir = ensure_work_dir("storage")
            outputs = automation.outputs("storage", self.env, refresh=False, work_dir=str(work_dir))

            if not outputs:
                return None

            conn_str = get_output_value(outputs, "connection_string", "")
            if conn_str:
                return conn_str if show_secrets else "****"
            return None
        except:
            return None

    def get_info(self) -> dict[str, Any]:
        """
        Get storage account information.

        Returns:
            Storage info dictionary
        """
        try:
            work_dir = ensure_work_dir("storage")
            outputs = automation.outputs("storage", self.env, refresh=False, work_dir=str(work_dir))

            if not outputs:
                return {}

            containers = get_output_value(outputs, "containers", [])
            # Extract container names
            if containers and isinstance(containers[0], dict):
                container_names = [c.get("name", "unnamed") for c in containers]
            else:
                container_names = containers

            return {
                "account_name": get_output_value(outputs, "account_name", "unknown"),
                "resource_group": get_output_value(outputs, "resource_group", "unknown"),
                "location": get_output_value(outputs, "location", "unknown"),
                "endpoint": get_output_value(outputs, "primary_endpoint"),
                "containers": container_names,
                "environment": self.env,
            }
        except:
            return {}

    def export_env_vars(self, output_file: str | None = None) -> str:
        """
        Export storage configuration as environment variables.

        Args:
            output_file: Optional file to write to

        Returns:
            Shell export commands
        """
        outputs = self.get_info()
        conn_str = self.get_connection_string(show_secrets=True)

        if not outputs:
            return ""

        export_content = f"""# ModelOps Storage Configuration
# Generated for environment: {self.env}
export AZURE_STORAGE_CONNECTION_STRING="{conn_str or ""}"
export AZURE_STORAGE_ACCOUNT="{outputs.get("account_name", "")}"
export MODELOPS_STORAGE_ENV="{self.env}"
"""

        if output_file:
            from pathlib import Path

            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(export_content)
            os.chmod(output_path, 0o600)  # User read/write only

        return export_content
