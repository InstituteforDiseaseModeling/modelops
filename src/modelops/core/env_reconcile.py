"""Bundle environment reconciliation - the ONLY writer of bundle-env files.

This module ensures bundle environment files always reflect the truth from
Pulumi stacks. It's the single source of writes/deletes for these files.
"""

import os
import tempfile
from typing import Optional
from pathlib import Path

from ..core import automation
from ..core.env_config import get_environments_dir


def reconcile_bundle_env(env: str, dry_run: bool = False, verbose: bool = False) -> Optional[Path]:
    """Reconcile bundle env file with Pulumi stack truth.

    This is the ONLY function that writes/deletes bundle environment files.
    - If both registry AND storage have outputs → write file
    - If either is missing → delete file if exists
    - Always atomic (temp file + rename)

    Args:
        env: Environment name
        dry_run: If True, skip actual file operations
        verbose: If True, print detailed output

    Returns:
        Path to env file if written, None otherwise
    """
    if dry_run:
        return None

    # Get current stack outputs
    try:
        reg_outputs = automation.outputs("registry", env, refresh=False) or {}
    except Exception as e:
        if verbose:
            print(f"  Could not get registry outputs: {e}")
        reg_outputs = {}

    try:
        sto_outputs = automation.outputs("storage", env, refresh=False) or {}
    except Exception as e:
        if verbose:
            print(f"  Could not get storage outputs: {e}")
        sto_outputs = {}

    # Check minimum required outputs
    has_registry = bool(
        automation.get_output_value(reg_outputs, "login_server") or
        automation.get_output_value(reg_outputs, "registry_name")
    )
    has_storage = bool(
        automation.get_output_value(sto_outputs, "connection_string") or
        automation.get_output_value(sto_outputs, "primary_endpoint")
    )

    env_dir = get_environments_dir()
    env_file = env_dir / f"{env}.yaml"

    if has_registry and has_storage:
        # Both present - write atomically
        from ..core.env_config import save_environment_config

        env_dir.mkdir(parents=True, exist_ok=True)

        # Write to temp file first for atomicity
        with tempfile.NamedTemporaryFile(
            mode='w',
            dir=str(env_dir),
            prefix=f".{env}.",
            suffix=".tmp",
            delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Write the actual config to temp file
            # Note: We need to modify save_environment_config to support this
            # For now, we'll write directly and then rename
            from modelops_contracts.bundle_environment import (
                BundleEnvironment,
                RegistryConfig,
                StorageConfig,
            )
            from datetime import datetime
            import yaml

            # Build registry config
            registry = RegistryConfig(
                provider="acr",  # Azure Container Registry
                login_server=automation.get_output_value(reg_outputs, "login_server", ""),
                requires_auth=automation.get_output_value(reg_outputs, "requires_auth", True)
            )

            # Build storage config
            containers = automation.get_output_value(sto_outputs, "containers", [])

            # Handle different serialization formats from Pulumi
            container_names = []
            if containers:
                if isinstance(containers[0], dict):
                    # Normal dict format
                    container_names = [c.get("name", "unnamed") for c in containers]
                elif isinstance(containers[0], list):
                    # Nested list format [[['name', 'value'], ...], ...]
                    for container_data in containers:
                        container_dict = dict(container_data) if container_data else {}
                        if "name" in container_dict:
                            container_names.append(container_dict["name"])
                elif isinstance(containers[0], str):
                    # Already a list of strings
                    container_names = containers

            if not container_names:
                container_names = ["bundle-blobs"]

            primary_container = "bundle-blobs" if "bundle-blobs" in container_names else (
                container_names[0] if container_names else "bundle-blobs"
            )

            storage = StorageConfig(
                provider="azure",
                container=primary_container,
                connection_string=automation.get_output_value(sto_outputs, "connection_string"),
                endpoint=automation.get_output_value(sto_outputs, "primary_endpoint")
            )

            # Create bundle environment
            bundle_env = BundleEnvironment(
                environment=env,
                registry=registry,
                storage=storage,
                timestamp=datetime.utcnow().isoformat()
            )

            # Write to temp file
            with open(tmp_path, 'w') as f:
                yaml.safe_dump(
                    bundle_env.model_dump(exclude_none=True),
                    f,
                    default_flow_style=False,
                    sort_keys=False
                )

            # Atomic replace
            tmp_path.replace(env_file)

            # Set permissions (skip on Windows)
            if os.name != 'nt':
                env_file.chmod(0o600)

            if verbose:
                print(f"  ✓ Bundle environment written: {env_file}")

            return env_file

        except Exception as e:
            # Clean up temp file on error
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    # Either missing - remove stale file if exists
    if env_file.exists():
        try:
            env_file.unlink()
            missing = []
            if not has_registry:
                missing.append("registry")
            if not has_storage:
                missing.append("storage")
            print(f"  ✓ Removed stale bundle env file (missing {' and '.join(missing)})")
        except Exception:
            pass  # Best effort

    return None