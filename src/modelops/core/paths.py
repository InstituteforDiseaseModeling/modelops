"""Centralized path management for ModelOps Pulumi stacks.

This module defines all paths used by Pulumi stacks to ensure consistency
and prevent backend mismatches that break StackReferences.
"""

from pathlib import Path

# Single unified backend for ALL stacks (critical for StackReferences to work)
BACKEND_DIR = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
BACKEND_URL = f"file://{BACKEND_DIR}"

# Working directories for each component
WORK_DIRS = {
    "infra": Path.home() / ".modelops" / "pulumi" / "azure",
    "workspace": Path.home() / ".modelops" / "pulumi" / "workspace",
    "adaptive": Path.home() / ".modelops" / "pulumi" / "adaptive",
    "registry": Path.home() / ".modelops" / "pulumi" / "registry",
}

# Provider configuration directory
PROVIDER_DIR = Path.home() / ".modelops" / "providers"

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