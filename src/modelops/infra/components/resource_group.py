"""Azure Resource Group component - the root dependency for all Azure resources."""

import pulumi
import pulumi_azure_native as azure
from typing import Dict, Any, Optional

from ...core.naming import StackNaming


class ResourceGroup(pulumi.ComponentResource):
    """
    Resource Group component - creates and manages Azure resource groups.

    This is the root dependency for all other Azure components. It ensures
    idempotent resource group creation, handling both new deployments and
    existing resource groups gracefully.
    """

    def __init__(
        self,
        name: str,
        config: Dict[str, Any],
        opts: Optional[pulumi.ResourceOptions] = None
    ):
        """
        Create or reference an Azure resource group.

        Args:
            name: Component name
            config: Configuration dict containing:
                - environment: Environment name (dev, staging, prod)
                - location: Azure region (default: eastus2)
                - username: Optional username for per-user isolation
                - subscription_id: Azure subscription ID
            opts: Pulumi resource options
        """
        super().__init__("modelops:azure:ResourceGroup", name, {}, opts)

        # Store config for later use
        self.config = config

        # Extract configuration
        env = config["environment"]
        location = config.get("location", "eastus2")
        username = config.get("username")
        subscription_id = config.get("subscription_id")

        # Generate resource group name using centralized naming
        rg_name = StackNaming.get_resource_group_name(env, username)

        # Create or import resource group (idempotent)
        self.resource_group = self._ensure_resource_group(
            name, rg_name, location, subscription_id, username
        )

        # Export outputs for other components to reference
        self.resource_group_name = self.resource_group.name
        self.resource_group_id = self.resource_group.id
        self.location = pulumi.Output.from_input(location)

        # Register outputs
        self.register_outputs({
            "resource_group_name": self.resource_group_name,
            "resource_group_id": self.resource_group_id,
            "location": self.location,
            "environment": env,
            "username": username
        })

    def _ensure_resource_group(
        self,
        name: str,
        rg_name: str,
        location: str,
        subscription_id: str,
        username: Optional[str]
    ) -> azure.resources.ResourceGroup:
        """
        Create or get existing resource group (idempotent).

        This method handles the case where a resource group already exists in Azure
        but may not be in the Pulumi state. It attempts to use an existing RG if found,
        otherwise creates a new one.

        Args:
            name: Component name for Pulumi resource
            rg_name: Azure resource group name
            location: Azure region
            subscription_id: Azure subscription ID
            username: Optional username for tagging

        Returns:
            ResourceGroup resource (new or imported)
        """
        rg_id = f"/subscriptions/{subscription_id}/resourceGroups/{rg_name}"

        # Try to check if resource group exists in Azure
        try:
            # Attempt to get the existing resource group
            existing_rg_result = azure.resources.get_resource_group(
                resource_group_name=rg_name,
                opts=pulumi.InvokeOptions(parent=self)
            )

            # If we get here, the RG exists in Azure
            # Use ResourceGroup.get to import it into our state
            pulumi.log.info(f"Resource group '{rg_name}' already exists, importing into state")

            rg = azure.resources.ResourceGroup.get(
                f"{name}-rg",
                id=rg_id,
                opts=pulumi.ResourceOptions(
                    parent=self
                    # Note: NO protect or retain_on_delete - we use --delete-rg flag for safety
                )
            )

            return rg

        except Exception as e:
            # Resource group doesn't exist or we can't access it
            # Create a new one
            pulumi.log.info(f"Creating new resource group: {rg_name}")

            tags = {
                "managed-by": "modelops",
                "project": "modelops",
                "component": "resource-group",
                "environment": self.config.get("environment", "unknown")
            }

            if username:
                tags["user"] = username

            rg = azure.resources.ResourceGroup(
                f"{name}-rg",
                resource_group_name=rg_name,
                location=location,
                tags=tags,
                opts=pulumi.ResourceOptions(
                    parent=self
                    # Note: NO protect or retain_on_delete - we use --delete-rg flag for safety
                )
            )

            return rg