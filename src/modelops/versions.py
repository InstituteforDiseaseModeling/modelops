"""Centralized version management for ModelOps dependencies.

This module ensures client-server compatibility by maintaining
consistent versions across the Dask client and container images.
"""

# Pin versions for compatibility
DASK_VERSION = "2024.8.0"
PYTHON_VERSION = "3.11"
POSTGRES_VERSION = "14"

# Container images (ISSUE-11, ISSUE-20 fix: pinned versions, no 'latest')
# Use centralized image configuration for modelops images
from .images import get_image_config
_img_config = get_image_config()

DASK_IMAGE = f"ghcr.io/dask/dask:{DASK_VERSION}-py{PYTHON_VERSION}"
POSTGRES_IMAGE = f"postgres:{POSTGRES_VERSION}-alpine"
PYTHON_IMAGE = f"python:{PYTHON_VERSION}-slim"
# Use adaptive worker from centralized config
ADAPTIVE_WORKER_IMAGE = _img_config.adaptive_worker_image()

# For pyproject.toml validation
DASK_REQUIREMENT = f"dask[distributed]=={DASK_VERSION}"

def check_compatibility():
    """Check if local environment matches expected versions.
    
    Returns:
        tuple: (is_compatible, messages) where messages contains any warnings
    """
    import sys
    messages = []
    is_compatible = True
    
    # Check Python version
    local_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if local_python != PYTHON_VERSION:
        messages.append(f"Python {local_python} != expected {PYTHON_VERSION}")
        # Python mismatch is a warning, not an error for MVP
    
    # Check Dask version
    try:
        import dask
        if dask.__version__ != DASK_VERSION:
            messages.append(
                f"Dask {dask.__version__} != required {DASK_VERSION}. "
                f"Run 'uv sync' or build images with Makefile to fix version mismatch."
            )
            is_compatible = False
    except ImportError:
        messages.append(
            f"Dask not installed. Run 'uv sync' to install required version {DASK_VERSION}"
        )
        is_compatible = False
    
    return is_compatible, messages

def extract_versions_from_image(image_tag: str) -> dict:
    """Extract version info from a Dask image tag.
    
    Args:
        image_tag: Docker image tag like "ghcr.io/dask/dask:2024.8.0-py3.11"
    
    Returns:
        dict with 'dask' and 'python' version strings, or empty dict
    """
    import re
    
    # Match patterns like "2024.8.0-py3.11" or "2024.8.0"
    pattern = r':(\d+\.\d+\.\d+)(?:-py(\d+\.\d+))?'
    match = re.search(pattern, image_tag)
    
    if match:
        result = {'dask': match.group(1)}
        if match.group(2):
            result['python'] = match.group(2)
        return result
    
    return {}
