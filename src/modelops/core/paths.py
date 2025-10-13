"""Centralized path management for ModelOps.

This module defines all paths used by ModelOps to ensure consistency
across all components. All path definitions should come from here.
"""

from pathlib import Path

# Configuration file
CONFIG_FILE = Path.home() / ".modelops" / "config.yaml"

# Infrastructure configuration file (unified spec)
INFRASTRUCTURE_FILE = Path.home() / ".modelops" / "infrastructure.yaml"

# Base directories
MODELOPS_HOME = Path.home() / ".modelops"
PULUMI_HOME = MODELOPS_HOME / "pulumi"

# Unified backend for all stacks (required for StackReferences to work)
BACKEND_DIR = PULUMI_HOME / "backend"

# Clean component structure
WORK_DIRS = {
    "resource-group": PULUMI_HOME / "resource-group",
    "resource_group": PULUMI_HOME / "resource-group",  # Alias for consistency
    "infra": PULUMI_HOME / "infra",
    "cluster": PULUMI_HOME / "infra",  # Alias for infra (cluster = infra)
    "workspace": PULUMI_HOME / "workspace",
    "adaptive": PULUMI_HOME / "adaptive",
    "registry": PULUMI_HOME / "registry",
    "storage": PULUMI_HOME / "storage",
}

# Provider configurations
PROVIDER_DIR = MODELOPS_HOME / "providers"


def get_backend_url() -> str:
    """Get backend URL from config or use default local path.
    
    Returns:
        Backend URL string, either from config or default file:// path
    """
    from .config import ModelOpsConfig
    config = ModelOpsConfig.get_instance()
    if config.pulumi.backend_url:
        return config.pulumi.backend_url
    ensure_backend()
    return f"file://{BACKEND_DIR}"


def ensure_work_dir(component: str) -> Path:
    """Ensure working directory exists for a component.
    
    Args:
        component: Component name (infra, workspace, adaptive, registry)
    
    Returns:
        Path to the working directory
    
    Raises:
        ValueError: If component is not recognized
    """
    if component not in WORK_DIRS:
        raise ValueError(f"Unknown component: {component}. Must be one of: {list(WORK_DIRS.keys())}")
    
    work_dir = WORK_DIRS[component]
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def ensure_backend() -> Path:
    """Ensure backend directory exists.
    
    Returns:
        Path to the backend directory
    """
    BACKEND_DIR.mkdir(parents=True, exist_ok=True)
    return BACKEND_DIR