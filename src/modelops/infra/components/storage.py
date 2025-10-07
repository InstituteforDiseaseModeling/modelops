"""Blob storage component using Pulumi ComponentResource.

Creates Azure storage account with containers for bundles, results,
workspace scratch, and task definitions. Provides dual access for
both workstation and cloud environments.
"""

import os
import pulumi
import pulumi_azure_native as azure
import pulumi_kubernetes as k8s
from typing import Dict, Any, Optional, List
from pathlib import Path
from ...core import StackNaming
from ...components.specs.storage import StorageConfig


class BlobStorage(pulumi.ComponentResource):
    """Azure blob storage for ModelOps artifacts and results.
    
    Creates storage account with containers and distributes
    credentials via Kubernetes secrets for cloud access and
    outputs for workstation access.
    """
    
    def __init__(self, name: str,
                 config: Dict[str, Any],
                 infra_stack_ref: Optional[str] = None,
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize storage component.
        
        Args:
            name: Component name (e.g., "storage")
            config: Storage configuration (validated via StorageConfig)
            infra_stack_ref: Optional reference to infra stack for K8s secret creation
            opts: Pulumi resource options
        """
        super().__init__("modelops:infra:storage", name, None, opts)
        
        # Store static name for resource naming
        self._static_name = name
        
        # Validate configuration using Pydantic model
        storage_config = StorageConfig(**config)
        validated_config = storage_config.to_pulumi_config()
        
        # Get environment and location
        env = config.get("environment", "dev")
        location = config.get("location", "eastus2")
        
        # Get username for per-user resources in dev
        username = self._get_username(config)

        # Always use naming convention for resource groups
        rg_name = StackNaming.get_resource_group_name(env, username)

        # Use explicit account name if provided, otherwise generate from naming
        desired_account_name = config.get("account_name")

        # Create storage account
        account = self._create_storage_account(name, env, location, rg_name, username, desired_account_name)
        
        # Create containers
        self._create_containers(account, storage_config.containers, rg_name)
        
        # Get connection string
        connection_string = self._get_connection_string(account, rg_name)

        # Create SAS-based connection string for bundles container
        sas_connection_string = self._create_sas_connection_string(account, rg_name, storage_config.containers)

        # Create K8s secret if infra reference provided
        if infra_stack_ref:
            self._create_k8s_secret(
                infra_stack_ref,
                account.name,
                connection_string,
                env
            )
        
        # Store outputs for reference
        self.account_name = account.name
        self.connection_string = connection_string
        self.sas_connection_string = sas_connection_string
        self.primary_endpoint = account.primary_endpoints.blob
        self.resource_group = pulumi.Output.from_input(rg_name)

        # Register outputs for both workstation and cloud access
        self.register_outputs({
            "account_name": account.name,
            "resource_group": pulumi.Output.from_input(rg_name),
            "connection_string": pulumi.Output.secret(connection_string),
            "sas_connection_string": pulumi.Output.secret(sas_connection_string),
            "primary_endpoint": account.primary_endpoints.blob,
            "containers": storage_config.get_container_names(),
            "location": pulumi.Output.from_input(location),
            "environment": pulumi.Output.from_input(env)
        })
    
    def _get_username(self, config: Dict[str, Any]) -> str:
        """Get username for per-user resources."""
        # Check config first, then use centralized username source
        if config.get("username"):
            username = config["username"]
        else:
            from ...core.config import get_username
            username = get_username()
        return StackNaming.sanitize_username(username)
    
    def _create_storage_account(self, name: str, env: str,
                                location: str, rg_name: str, username: str,
                                desired_account_name: Optional[str] = None):
        """Create storage account with unique name."""
        # Use provided name if set, otherwise generate deterministic one
        account_name = desired_account_name or StackNaming.get_storage_account_name(env, username)

        return azure.storage.StorageAccount(
            f"{self._static_name}-account",
            resource_group_name=rg_name,
            account_name=account_name,
            location=location,
            sku=azure.storage.SkuArgs(
                name="Standard_LRS"  # Locally redundant (cheapest)
            ),
            kind="StorageV2",
            access_tier="Hot",
            allow_blob_public_access=False,  # Security by default
            enable_https_traffic_only=True,
            minimum_tls_version="TLS1_2",
            tags={
                "managed-by": "modelops",
                "component": "storage",
                "environment": env
            },
            opts=pulumi.ResourceOptions(parent=self)
        )
    
    def _create_containers(self, account: azure.storage.StorageAccount,
                          containers: List, rg_name: str):
        """Create blob containers."""
        for container_config in containers:
            container = azure.storage.BlobContainer(
                f"{container_config.name}-container",
                account_name=account.name,
                resource_group_name=rg_name,
                container_name=container_config.name,
                public_access="None",  # Private by default
                opts=pulumi.ResourceOptions(
                    parent=self,
                    depends_on=[account]  # Ensure storage account is ready
                )
            )
            
            # Set up lifecycle management for workspace container
            if container_config.lifecycle_days:
                self._setup_lifecycle_policy(
                    account, 
                    container_config.name,
                    container_config.lifecycle_days,
                    rg_name
                )
    
    def _setup_lifecycle_policy(self, account: azure.storage.StorageAccount,
                                container_name: str, days: int, rg_name: str):
        """Set up lifecycle management for automatic cleanup."""
        azure.storage.ManagementPolicy(
            f"{self._static_name}-lifecycle-policy",
            account_name=account.name,
            resource_group_name=rg_name,
            management_policy_name="default",  # Azure requires this to be "default"
            policy=azure.storage.ManagementPolicySchemaArgs(
                rules=[
                    azure.storage.ManagementPolicyRuleArgs(
                        name=f"delete-old-{container_name}",
                        enabled=True,
                        type="Lifecycle",
                        definition=azure.storage.ManagementPolicyDefinitionArgs(
                            actions=azure.storage.ManagementPolicyActionArgs(
                                base_blob=azure.storage.ManagementPolicyBaseBlobArgs(
                                    delete=azure.storage.DateAfterModificationArgs(
                                        days_after_modification_greater_than=days
                                    )
                                )
                            ),
                            filters=azure.storage.ManagementPolicyFilterArgs(
                                blob_types=["blockBlob"],
                                prefix_match=[f"{container_name}/"]
                            )
                        )
                    )
                ]
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[account])
        )
    
    def _get_connection_string(self, account: azure.storage.StorageAccount, rg_name: str):
        """Get primary connection string."""
        keys = azure.storage.list_storage_account_keys_output(
            resource_group_name=rg_name,
            account_name=account.name
        )

        return pulumi.Output.all(
            account.name,
            keys.keys[0].value
        ).apply(
            lambda args: (
                f"DefaultEndpointsProtocol=https;"
                f"AccountName={args[0]};"
                f"AccountKey={args[1]};"
                f"EndpointSuffix=core.windows.net"
            )
        )

    def _create_sas_connection_string(self, account: azure.storage.StorageAccount, rg_name: str, containers: List):
        """Create SAS-based connection string for bundles container (read/list only).

        TODO: Future enhancement - add `mops auth rotate` command to regenerate
        SAS tokens before 2030 expiry. Not needed for MVP/MLP.
        """
        from datetime import datetime, timedelta

        # Compute start time with 5-minute buffer for clock skew
        start_time = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Generate SAS token for bundle-blobs container
        # Find the bundle-blobs container name from config
        bundles_container = "bundle-blobs"
        for container in containers:
            if container.name == "bundle-blobs" or container.name == "bundle-blob":
                bundles_container = container.name
                break

        sas = azure.storage.list_storage_account_service_sas_output(
            resource_group_name=rg_name,
            account_name=account.name,
            canonicalized_resource=pulumi.Output.concat("/blob/", account.name, "/", bundles_container),
            resource="c",  # Container level
            permissions="rl",  # Read + list only
            protocols="https",  # HTTPS only for security
            shared_access_start_time=start_time,
            shared_access_expiry_time="2030-01-01T00:00:00Z",  # 5-year expiry for dev
        )

        # Build SAS-based connection string
        return pulumi.Output.all(
            account.name,
            sas.service_sas_token
        ).apply(
            lambda args: (
                f"BlobEndpoint=https://{args[0]}.blob.core.windows.net;"
                f"SharedAccessSignature={args[1]}"
            )
        )
    
    def _create_k8s_secret(self, infra_ref: str, account_name: pulumi.Output[str],
                          conn_str: pulumi.Output[str], env: str):
        """Create K8s secret for pod access in default namespace only.
        
        Other components (workspace, adaptive) will create their own secrets
        in their namespaces when they deploy, pulling the connection string
        from this storage stack's outputs.
        """
        infra = pulumi.StackReference(infra_ref)
        kubeconfig = infra.require_output("kubeconfig")
        
        k8s_provider = k8s.Provider(
            f"{self._static_name}-k8s",
            kubeconfig=kubeconfig,
            opts=pulumi.ResourceOptions(parent=self)
        )
        
        # Only create in default namespace - other components handle their own
        k8s.core.v1.Secret(
            f"storage-secret-default",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name="modelops-storage",
                namespace="default"
            ),
            string_data={
                "AZURE_STORAGE_CONNECTION_STRING": conn_str,
                "AZURE_STORAGE_ACCOUNT": account_name
            },
            opts=pulumi.ResourceOptions(
                provider=k8s_provider,
                parent=self
            )
        )