"""Version information for ModelOps.

Provides version string and git commit hash when available.
"""

import json
import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _get_version_from_metadata() -> str:
    """Get version from package metadata."""
    try:
        return version("modelops")
    except PackageNotFoundError:
        return "0.0.0-unknown"


def _get_git_hash_from_direct_url() -> str | None:
    """Get git hash from direct_url.json (pip/uv git installs)."""
    try:
        # Find the package's dist-info directory
        import modelops

        package_dir = Path(modelops.__file__).parent
        # Look for dist-info in site-packages
        site_packages = package_dir.parent
        for dist_info in site_packages.glob("modelops-*.dist-info"):
            direct_url = dist_info / "direct_url.json"
            if direct_url.exists():
                data = json.loads(direct_url.read_text())
                vcs_info = data.get("vcs_info", {})
                commit = vcs_info.get("commit_id")
                if commit:
                    return commit[:8]  # Short hash
    except Exception:
        pass
    return None


def _get_git_hash_from_repo() -> str | None:
    """Get git hash from local repo (development mode)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).parent,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


@lru_cache(maxsize=1)
def get_version_info() -> dict:
    """Get full version information.

    Returns:
        Dict with 'version', 'git_hash', and 'full' keys.
    """
    ver = _get_version_from_metadata()
    git_hash = _get_git_hash_from_direct_url() or _get_git_hash_from_repo()

    full = ver
    if git_hash:
        full = f"{ver}+g{git_hash}"

    return {
        "version": ver,
        "git_hash": git_hash,
        "full": full,
    }


def get_version() -> str:
    """Get the full version string with git hash if available."""
    return get_version_info()["full"]


# For backwards compatibility
__version__ = _get_version_from_metadata()
