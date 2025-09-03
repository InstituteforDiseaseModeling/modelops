"""Container registry abstraction using Pulumi ComponentResource.

Provides provider-agnostic container registry management with support for
Azure ACR, AWS ECR, GCP GCR, and external registries like DockerHub.
"""

import base64
import pulumi
import pulumi_azure_native as azure
import uuid
from typing import Dict, Any, Optional
from pathlib import Path
from ...core import StackNaming


class ContainerRegistry(pulumi.ComponentResource):
    """Provider-agnostic container registry abstraction.
    
    This component can be deployed as an independent stack and referenced
    by other stacks (infrastructure, workspace) via StackReferences.
    
    Supports:
    - Azure Container Registry (ACR)
    - AWS Elastic Container Registry (ECR) - future
    - Google Container Registry (GCR) - future
    - External registries (DockerHub, GHCR)
    """
    
    def __init__(self, name: str, config: Dict[str, Any],
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize container registry component.
        
        Args:
            name: Component name (e.g., "modelops-registry")
            config: Registry configuration including provider type
            opts: Optional Pulumi resource options
        """
        super().__init__("modelops:infra:registry", name, None, opts)
        
        provider = config.get("provider", "azure")
        
        if provider == "azure":
            self._create_azure_registry(name, config)
        elif provider == "dockerhub":
            self._setup_external_registry(name, config)
        elif provider == "ghcr":
            self._setup_external_registry(name, config)
        else:
            raise ValueError(f"Unsupported registry provider: {provider}")
        
        # Register outputs for StackReference access
        self.register_outputs({
            "login_server": self.login_server,
            "registry_name": self.registry_name,
            "provider": pulumi.Output.from_input(provider),
            "requires_auth": self.requires_auth
        })
    
    def _create_azure_registry(self, name: str, config: Dict[str, Any]):
        """Create Azure Container Registry."""
        # Extract Azure-specific config
        subscription_id = config["subscription_id"]
        location = config.get("location", "eastus2")
        env = config.get("environment", "dev")
        
        # Get username for per-user resources
        username = self._get_username(config)
        rg_name = StackNaming.get_resource_group_name(env, username)
        
        # Generate unique ACR name using centralized naming
        # For dev environments, use username as suffix for isolation
        # For prod, use org-level with random suffix
        if config.get("registry_name"):
            # Allow explicit override
            acr_name = config["registry_name"]
            # Ensure it's valid for ACR (alphanumeric only)
            acr_name = ''.join(c for c in acr_name if c.isalnum()).lower()
        elif env in ["dev", "staging"] and config.get("per_user_registry", True):
            # Development: per-user registries for isolation
            acr_name = StackNaming.get_acr_name(env, suffix=username)
        else:
            # Production or shared: org-level with random suffix
            acr_name = StackNaming.get_acr_name(env)
        
        # Reference existing resource group or create new one
        if config.get("use_existing_rg", True):
            # Reference the RG created by infrastructure stack
            # get_resource_group only takes resource_group_name as positional arg
            rg = azure.resources.get_resource_group(
                resource_group_name=rg_name
            )
        else:
            # Create dedicated RG for registry
            rg = azure.resources.ResourceGroup(
                f"{name}-rg",
                resource_group_name=f"{rg_name}-registry",
                location=location,
                tags={
                    "managed-by": "modelops",
                    "component": "registry",
                    "user": username
                },
                opts=pulumi.ResourceOptions(parent=self)
            )
        
        # Create ACR
        acr = azure.containerregistry.Registry(
            f"{name}-acr",
            registry_name=acr_name,
            resource_group_name=rg.name if hasattr(rg, 'name') else rg_name,
            location=location,
            sku=azure.containerregistry.SkuArgs(
                name=config.get("sku", "Standard")
            ),
            admin_user_enabled=False,  # Use managed identity
            opts=pulumi.ResourceOptions(parent=self)
        )
        
        # Set outputs
        self.login_server = acr.login_server
        self.registry_name = acr.name
        self.requires_auth = pulumi.Output.from_input(True)
        self.registry_id = acr.id
        
        # Store for role assignment
        self.acr = acr
        self.subscription_id = subscription_id
    
    def _setup_external_registry(self, name: str, config: Dict[str, Any]):
        """Setup configuration for external registry (DockerHub, GHCR)."""
        provider = config["provider"]
        
        if provider == "dockerhub":
            login_server = "docker.io"
            registry_name = config.get("username", "library")
        elif provider == "ghcr":
            login_server = "ghcr.io"
            registry_name = config.get("org", "modelops")
        else:
            login_server = config.get("login_server", "")
            registry_name = config.get("registry_name", name)
        
        self.login_server = pulumi.Output.from_input(login_server)
        self.registry_name = pulumi.Output.from_input(registry_name)
        self.requires_auth = pulumi.Output.from_input(config.get("requires_auth", True))
        self.registry_id = pulumi.Output.from_input(f"external:{provider}:{registry_name}")
    
    def setup_cluster_pull_permissions(self, cluster_stack_ref: str):
        """Grant cluster pull permissions to registry.
        
        Args:
            cluster_stack_ref: Stack reference to infrastructure stack
            
        Returns:
            Role assignment resource (for Azure) or None
        """
        if not hasattr(self, 'acr'):
            # Not an Azure registry, no permissions needed
            return None
        
        # Get cluster info from stack reference
        infra = pulumi.StackReference(cluster_stack_ref)
        cluster_name = infra.require_output("cluster_name")
        resource_group = infra.require_output("resource_group")
        
        # We need the cluster's kubelet identity
        # This requires querying the cluster resource
        cluster = azure.containerservice.get_managed_cluster(
            resource_name=cluster_name,
            resource_group_name=resource_group
        )
        
        # Get kubelet identity
        principal_id = cluster.identity_profile["kubeletidentity"]["object_id"]
        
        # AcrPull role definition ID
        acr_pull_role = f"/subscriptions/{self.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
        
        # Create role assignment with deterministic GUID
        # Azure requires a GUID for RoleAssignment names
        # Generate deterministic UUID from scope and principal to ensure idempotency
        role_assignment_guid = str(uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"{self.registry_id}-{principal_id}-acrpull"
        ))
        
        return azure.authorization.RoleAssignment(
            f"{self.registry_name}-cluster-pull",
            role_assignment_name=role_assignment_guid,
            principal_id=principal_id,
            principal_type="ServicePrincipal",
            role_definition_id=acr_pull_role,
            scope=self.registry_id,
            opts=pulumi.ResourceOptions(parent=self)
        )
    
    def _get_username(self, config: Dict[str, Any]) -> str:
        """Get username for per-user resources.
        
        Priority: config > environment > error
        Sanitizes username for Azure naming requirements.
        """
        if config.get("username"):
            username = config["username"]
        else:
            import os
            username = os.environ.get("USER") or os.environ.get("USERNAME")
            if not username:
                raise ValueError(
                    "Username required for per-user resources. "
                    "Set 'username' in config or USER environment variable"
                )
        
        # Sanitize for Azure naming (alphanumeric only for ACR)
        import re
        username = re.sub(r'[^a-zA-Z0-9]', '', username).lower()
        
        # ACR names have max length, truncate if needed
        return username[:20]