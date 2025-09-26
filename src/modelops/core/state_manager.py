"""Pulumi state management with automatic lock recovery and state reconciliation."""

import json
import os
import subprocess
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable
import psutil
import pulumi.automation as auto

from .env_config import load_environment_config, save_environment_config


class PulumiStateManager:
    """
    Manages Pulumi operations with automatic lock recovery and state
    sync.

    This class provides:
    - Automatic stale lock detection and clearing
    - State reconciliation with cloud reality
    - Environment YAML updates (single writer per component)
    - Robust error handling and recovery
    """

    def __init__(self, component: str, env: str):
        """Initialize state manager for a component.

        Args:
            component: Component name (storage, registry, cluster, workspace)
            env: Environment name (dev, staging, prod)
        """
        self.component = component
        self.env = env
        self.work_dir = Path.home() / ".modelops" / "pulumi" / component
        self.lock_timeout_seconds = 900  # 15 minutes

    def execute_with_recovery(
        self,
        operation: str,
        program: Optional[Callable] = None,
        on_output: Optional[Callable[[str], None]] = None,
        **kwargs
    ) -> Optional[auto.UpResult]:
        """Execute Pulumi operation with automatic lock recovery.

        Args:
            operation: Operation to perform (up, destroy, refresh)
            program: Pulumi program to run (for up operation)
            on_output: Callback for output messages
            **kwargs: Additional arguments for the operation

        Returns:
            Result of the operation (UpResult for up, None for destroy/refresh)

        Raises:
            Exception: If operation fails after recovery attempts
        """
        # 1. Check and clear stale locks
        if self._has_stale_lock():
            print(f"  Found stale lock for {self.component}, clearing...")
            self._clear_stale_lock()

        # 2. For up operations, reconcile state first
        if operation == "up":
            self._reconcile_state()

        # 3. Execute the operation
        try:
            stack = self._get_stack(program)

            if operation == "up":
                result = stack.up(on_output=on_output or (lambda x: None), **kwargs)
                # Update environment YAML on success
                if result.summary.result != "failed":
                    self._update_environment_yaml(result.outputs)
                return result

            elif operation == "destroy":
                stack.destroy(on_output=on_output or (lambda x: None), **kwargs)
                # Remove from environment YAML on success
                self._remove_from_environment_yaml()
                return None

            elif operation == "refresh":
                return stack.refresh(on_output=on_output or (lambda x: None), **kwargs)

        except auto.ConcurrentUpdateError as e:
            # Another operation is in progress
            raise Exception(
                f"Stack is locked by another operation. Wait or run:\n"
                f"  cd {self.work_dir} && pulumi cancel"
            ) from e
        except Exception as e:
            # Check if it's a lock error
            error_msg = str(e).lower()
            if "lock" in error_msg and "pulumi cancel" in error_msg:
                self._handle_lock_error(e)
            raise

    def _has_stale_lock(self) -> bool:
        """Check if there's a stale lock file.

        Returns:
            True if a stale lock exists, False otherwise
        """
        lock_dir = self.work_dir.parent / "backend" / ".pulumi" / "locks"
        if not lock_dir.exists():
            return False

        # Find lock files for this stack
        stack_name = self._get_stack_name()

        # Look for locks in the organization structure
        org_lock_dir = lock_dir / "organization"
        if not org_lock_dir.exists():
            return False

        # Search for lock files
        for project_dir in org_lock_dir.iterdir():
            if not project_dir.is_dir():
                continue
            stack_dir = project_dir / stack_name
            if not stack_dir.exists():
                continue

            for lock_file in stack_dir.glob("*.json"):
                try:
                    with open(lock_file) as f:
                        lock_data = json.load(f)

                    if self._is_lock_stale(lock_data, lock_file):
                        return True

                except (json.JSONDecodeError, IOError):
                    # Corrupted lock file is considered stale
                    return True

        return False

    def _is_lock_stale(self, lock_data: dict, lock_file: Path) -> bool:
        """Check if a lock is stale based on PID and time.

        Args:
            lock_data: Lock file contents
            lock_file: Path to lock file

        Returns:
            True if lock is stale, False otherwise
        """
        pid = lock_data.get("pid")
        timestamp_str = lock_data.get("timestamp", "")

        # Check if process is dead
        if pid:
            try:
                if not psutil.pid_exists(pid):
                    print(f"    Lock held by dead process {pid}")
                    return True

                # Check if it's actually a Pulumi/Python process
                proc = psutil.Process(pid)
                cmdline = " ".join(proc.cmdline()).lower()
                if "pulumi" not in cmdline and "python" not in cmdline:
                    print(f"    Lock held by non-Pulumi process {pid}")
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return True

        # Check if lock is old
        try:
            # Parse timestamp (handle various formats)
            if timestamp_str:
                # Remove timezone suffix for parsing
                timestamp_clean = timestamp_str.replace("Z", "+00:00")
                lock_time = datetime.fromisoformat(timestamp_clean)

                # Calculate age
                now = datetime.now(lock_time.tzinfo) if lock_time.tzinfo else datetime.now()
                age_seconds = (now - lock_time).total_seconds()

                if age_seconds > self.lock_timeout_seconds:
                    print(f"    Lock is {int(age_seconds/60)} minutes old (timeout: {self.lock_timeout_seconds/60} min)")
                    return True
        except (ValueError, TypeError) as e:
            print(f"    Could not parse lock timestamp: {e}")
            # Can't parse timestamp, consider it stale
            return True

        return False

    def _clear_stale_lock(self):
        """Clear stale lock using pulumi cancel."""
        stack_name = self._get_stack_name()

        try:
            result = subprocess.run(
                ["pulumi", "cancel", "--yes", "-s", stack_name],
                cwd=self.work_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                print(f"  ✓ Cleared stale lock for {stack_name}")
            else:
                # Non-zero return code might mean no lock exists, which is fine
                if "no stack named" not in result.stderr.lower():
                    print(f"  ⚠ Could not clear lock: {result.stderr}")

        except subprocess.TimeoutExpired:
            print(f"  ⚠ Timeout clearing lock for {stack_name}")
        except Exception as e:
            print(f"  ⚠ Error clearing lock: {e}")

    def _reconcile_state(self):
        """Reconcile Pulumi state with cloud reality using refresh."""
        try:
            print(f"  Refreshing {self.component} state...")
            stack = self._get_stack()
            result = stack.refresh(on_output=lambda x: None)

            if result.summary.result == "succeeded":
                print(f"  ✓ State refreshed successfully")
            else:
                print(f"  ⚠ State refresh had issues but continuing")

        except Exception as e:
            # Don't fail the operation if refresh fails
            print(f"  ⚠ Could not refresh state: {e}")
            print(f"    Continuing with potentially stale state...")

    def _get_stack(self, program: Optional[Callable] = None) -> auto.Stack:
        """Get or create Pulumi stack.

        Args:
            program: Pulumi program to use (defaults to noop)

        Returns:
            Pulumi Stack object
        """
        from . import automation

        # Use the centralized select_stack function
        return automation.select_stack(
            self.component,
            self.env,
            program=program,
            work_dir=str(self.work_dir)
        )

    def _get_stack_name(self) -> str:
        """Get the stack name for this component and environment.

        Returns:
            Stack name in format modelops-{component}-{env}
        """
        from .naming import StackNaming
        return StackNaming.get_stack_name(self.component, self.env)

    def _update_environment_yaml(self, outputs: Optional[Dict[str, Any]]):
        """Update environment YAML with outputs - THIS IS THE ONLY WRITER.

        Args:
            outputs: Pulumi stack outputs to save
        """
        if not outputs:
            return

        # Extract plain values from Pulumi outputs
        plain_outputs = {}
        for key, value in outputs.items():
            if hasattr(value, 'value'):
                plain_outputs[key] = value.value
            elif isinstance(value, dict) and 'value' in value:
                plain_outputs[key] = value['value']
            else:
                plain_outputs[key] = value

        # Load existing config (returns EnvironmentConfig object or raises FileNotFoundError)
        try:
            existing_config = load_environment_config(self.env)
        except FileNotFoundError:
            existing_config = None

        # Update with new outputs for this component
        if self.component == "registry" and plain_outputs:
            # For registry, save the entire output dict
            # existing_config is an EnvironmentConfig object, not a dict
            existing_storage = existing_config.storage.model_dump() if existing_config and existing_config.storage else None
            save_environment_config(
                self.env,
                registry_outputs=plain_outputs,
                storage_outputs=existing_storage
            )
        elif self.component == "storage" and plain_outputs:
            # For storage, save the entire output dict
            # existing_config is an EnvironmentConfig object, not a dict
            existing_registry = existing_config.registry.model_dump() if existing_config and existing_config.registry else None
            save_environment_config(
                self.env,
                registry_outputs=existing_registry,
                storage_outputs=plain_outputs
            )
        elif self.component == "cluster":
            # Cluster outputs are not saved to environment YAML
            # They're accessed directly via Pulumi when needed
            pass
        elif self.component == "workspace":
            # Workspace outputs are not saved to environment YAML
            pass

    def _remove_from_environment_yaml(self):
        """Remove component from environment YAML."""
        try:
            config = load_environment_config(self.env)
            if not config:
                return

            # Remove this component's data
            # config is an EnvironmentConfig object, not a dict
            updated = False
            remaining_registry = None
            remaining_storage = None

            if self.component == "registry" and config.registry:
                # Keep storage, remove registry
                remaining_storage = config.storage.model_dump() if config.storage else None
                updated = True
            elif self.component == "storage" and config.storage:
                # Keep registry, remove storage
                remaining_registry = config.registry.model_dump() if config.registry else None
                updated = True
            else:
                # Component not in config, nothing to remove
                return

            if updated:
                # Save updated config or delete if empty
                if remaining_registry or remaining_storage:
                    # Still has data, update the file
                    save_environment_config(
                        self.env,
                        registry_outputs=remaining_registry,
                        storage_outputs=remaining_storage
                    )
                else:
                    # No data left, delete the file
                    config_path = Path.home() / ".modelops" / "environments" / f"{self.env}.yaml"
                    if config_path.exists():
                        config_path.unlink()
                        print(f"  ✓ Removed environment config for {self.env}")

        except Exception as e:
            # Don't fail the destroy operation if config removal fails
            print(f"  ⚠ Could not update environment config: {e}")

    def _handle_lock_error(self, error: Exception):
        """Handle lock errors with helpful messages.

        Args:
            error: The lock error exception
        """
        print(f"\n  Error: {error}")
        print(f"\n  The stack appears to be locked. Try:")
        print(f"    1. Wait for the current operation to complete")
        print(f"    2. If the operation is stuck, clear the lock:")
        print(f"       cd {self.work_dir} && pulumi cancel")
        print(f"    3. Then retry your operation")
