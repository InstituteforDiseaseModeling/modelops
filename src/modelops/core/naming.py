"""Centralized naming conventions for ModelOps stacks and resources.

This module provides a single source of truth for all naming conventions
used throughout ModelOps, including Pulumi stacks, Azure resources, and
Kubernetes resources.
"""

import re
import random
import string
from typing import Optional


class StackNaming:
    """Centralized naming for all Pulumi stacks and cloud resources.
    
    All methods use the PROJECT_PREFIX to ensure consistency.
    The prefix can be changed to rebrand or for testing.
    """
    
    PROJECT_PREFIX = "modelops"
    
    @staticmethod
    def get_stack_name(component: str, env: str, run_id: Optional[str] = None) -> str:
        """Generate Pulumi stack name.
        
        Pattern: {PROJECT_PREFIX}-{component}-{env}[-{run_id}]
        
        Args:
            component: Component name (infra, workspace, adaptive)
            env: Environment name (dev, staging, prod)
            run_id: Optional run ID for adaptive stacks
            
        Returns:
            Stack name like 'modelops-infra-dev'
        """
        parts = [StackNaming.PROJECT_PREFIX, component, env]
        if run_id:
            parts.append(run_id)
        return "-".join(parts)
    
    @staticmethod
    def get_project_name(component: str) -> str:
        """Generate Pulumi project name.
        
        Pattern: {PROJECT_PREFIX}-{component}
        
        Args:
            component: Component name (infra, workspace, adaptive)
            
        Returns:
            Project name like 'modelops-infra'
        """
        return f"{StackNaming.PROJECT_PREFIX}-{component}"
    
    @staticmethod
    def get_resource_group_name(env: str, username: Optional[str] = None) -> str:
        """Generate Azure resource group name.
        
        Pattern: {PROJECT_PREFIX}-{env}-rg[-{username}]
        
        Args:
            env: Environment name (dev, staging, prod)
            username: Optional username for per-user resource groups
            
        Returns:
            Resource group name like 'modelops-dev-rg-alice'
        """
        base = f"{StackNaming.PROJECT_PREFIX}-{env}-rg"
        if username:
            sanitized = StackNaming.sanitize_username(username)
            return f"{base}-{sanitized}"
        return base
    
    @staticmethod
    def get_aks_cluster_name(env: str) -> str:
        """Generate AKS cluster name.
        
        Pattern: {PROJECT_PREFIX}-{env}-aks
        
        Args:
            env: Environment name (dev, staging, prod)
            
        Returns:
            AKS cluster name like 'modelops-dev-aks'
        """
        return f"{StackNaming.PROJECT_PREFIX}-{env}-aks"
    
    @staticmethod
    def get_acr_name(env: str, suffix: Optional[str] = None) -> str:
        """Generate Azure Container Registry name.
        
        ACR names must be globally unique and alphanumeric only (no hyphens).
        Pattern: {PROJECT_PREFIX}{env}acr{suffix}
        
        Args:
            env: Environment name (dev, staging, prod)
            suffix: Optional suffix, auto-generated if not provided
            
        Returns:
            ACR name like 'modelopsdevacr7x9k'
        """
        if not suffix:
            suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
        # Remove any non-alphanumeric characters from env
        clean_env = re.sub(r'[^a-zA-Z0-9]', '', env).lower()
        return f"{StackNaming.PROJECT_PREFIX}{clean_env}acr{suffix}"
    
    @staticmethod
    def get_storage_account_name(env: str, suffix: Optional[str] = None) -> str:
        """Generate Azure Storage Account name.
        
        Storage account names must be globally unique, lowercase, alphanumeric only.
        Pattern: {PROJECT_PREFIX}{env}st{suffix}
        
        Args:
            env: Environment name (dev, staging, prod)
            suffix: Optional suffix, auto-generated if not provided
            
        Returns:
            Storage account name like 'modelopsdevst8m2p'
        """
        if not suffix:
            suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
        # Remove any non-alphanumeric characters from env
        clean_env = re.sub(r'[^a-zA-Z0-9]', '', env).lower()
        # Ensure total length doesn't exceed Azure's 24 character limit
        base = f"{StackNaming.PROJECT_PREFIX}{clean_env}st"
        if len(base) + len(suffix) > 24:
            # Truncate base if needed
            max_base_len = 24 - len(suffix)
            base = base[:max_base_len]
        return f"{base}{suffix}"
    
    @staticmethod
    def get_namespace(component: str, env: str) -> str:
        """Generate Kubernetes namespace.
        
        Pattern: {PROJECT_PREFIX}-{component}-{env}
        
        Args:
            component: Component name (dask, adaptive, etc.)
            env: Environment name (dev, staging, prod)
            
        Returns:
            Namespace like 'modelops-dask-dev'
        """
        return f"{StackNaming.PROJECT_PREFIX}-{component}-{env}"
    
    @staticmethod
    def sanitize_username(username: str) -> str:
        """Sanitize username for use in resource names.
        
        Removes non-alphanumeric characters, converts to lowercase,
        and limits length to Azure's requirements.
        
        Args:
            username: Raw username to sanitize
            
        Returns:
            Sanitized username suitable for resource names
        """
        # Remove non-alphanumeric, convert to lowercase
        sanitized = re.sub(r'[^a-zA-Z0-9]', '', username).lower()
        # Limit to 20 characters for Azure resource name limits
        return sanitized[:20]
    
    @staticmethod
    def get_infra_stack_ref(env: str) -> str:
        """Get infrastructure stack reference name.
        
        Convenience method for getting the infrastructure stack name.
        
        Args:
            env: Environment name (dev, staging, prod)
            
        Returns:
            Infrastructure stack name like 'modelops-infra-dev'
        """
        return StackNaming.get_stack_name("infra", env)
    
    @staticmethod
    def get_workspace_stack_ref(env: str) -> str:
        """Get workspace stack reference name.
        
        Convenience method for getting the workspace stack name.
        
        Args:
            env: Environment name (dev, staging, prod)
            
        Returns:
            Workspace stack name like 'modelops-workspace-dev'
        """
        return StackNaming.get_stack_name("workspace", env)
    
    @staticmethod
    def parse_stack_name(stack_name: str) -> dict:
        """Parse a stack name into its components.
        
        Extracts component, environment, and optional run_id from a stack name.
        
        Args:
            stack_name: Full stack name to parse
            
        Returns:
            Dictionary with 'component', 'env', and optionally 'run_id'
        """
        parts = stack_name.split("-")
        if len(parts) < 3:
            raise ValueError(f"Invalid stack name format: {stack_name}")
        
        # Assuming format: {prefix}-{component}-{env}[-{run_id}...]
        result = {
            "prefix": parts[0],
            "component": parts[1],
            "env": parts[2]
        }
        
        # If there are more parts, they're the run_id
        if len(parts) > 3:
            result["run_id"] = "-".join(parts[3:])
        
        return result