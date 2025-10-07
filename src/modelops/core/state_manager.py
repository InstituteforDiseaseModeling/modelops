"""Pulumi state management with automatic lock recovery and state reconciliation."""

import json
import os
import subprocess
from .subprocess_utils import run_pulumi_command
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable
import psutil
import pulumi.automation as auto


class PulumiStateManager:
    """
    Manages Pulumi operations with automatic lock recovery and state
    sync.

    This class provides:
    - Automatic stale lock detection and clearing
    - State reconciliation with cloud reality
    - Environment YAML updates (single writer per component)
    - Robust error handling and recovery

    ## Developer Notes

    This emerged out of repeated Pulumi lock contention issues and BundleEnvironment
    type confusion bugs. The core problems were:

    1. Stale locks from interrupted operations blocking subsequent runs
    2. BundleEnvironment Pydantic models being treated as dicts (.get() calls)
    3. Registry/storage configs overwriting each other instead of merging

    This manager ensures each component can independently provision while preserving
    the shared environment YAML, and automatically recovers from common failure modes.
    """

    def __init__(self, component: str, env: str):
        """Initialize state manager for a component.

        Args:
            component: Component name (storage, registry, cluster, workspace)
            env: Environment name (dev, staging, prod)
        """
        from .paths import ensure_work_dir
        from .automation import _ensure_passphrase

        self.component = component
        self.env = env
        # Use ensure_work_dir to create the directory if it doesn't exist
        self.work_dir = ensure_work_dir(component)
        self.lock_timeout_seconds = 900  # 15 minutes

        # Ensure passphrase is configured for all subprocess calls
        _ensure_passphrase()

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
                # Note: Environment reconciliation happens at higher level (InfrastructureService)
                return result

            elif operation == "destroy":
                stack.destroy(on_output=on_output or (lambda x: None), **kwargs)
                # Note: Environment reconciliation happens at higher level (InfrastructureService)
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
            result = run_pulumi_command(
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
        """Reconcile Pulumi state with cloud reality using refresh.

        Handles pending CREATE operations that require interactive refresh.
        """
        try:
            print(f"  Refreshing {self.component} state...")

            # First check if we have pending operations
            stack_name = self._get_stack_name()
            pending_ops = self._check_pending_operations()

            if pending_ops:
                print(f"  ⚠ Found {len(pending_ops)} pending operations, running interactive refresh...")
                # For pending CREATE operations, we need to run refresh with --yes to clear them
                result = run_pulumi_command(
                    ["pulumi", "refresh", "--yes", "--skip-preview", "-s", stack_name],
                    cwd=self.work_dir,
                    capture_output=True,
                    text=True,
                    timeout=60
                )

                if result.returncode == 0:
                    print(f"  ✓ Cleared pending operations and refreshed state")
                else:
                    print(f"  ⚠ Refresh completed with warnings: {result.stderr}")
            else:
                # Normal refresh through automation API
                stack = self._get_stack()
                result = stack.refresh(on_output=lambda x: None)

                if result.summary.result == "succeeded":
                    print(f"  ✓ State refreshed successfully")
                else:
                    print(f"  ⚠ State refresh had issues but continuing")

        except subprocess.TimeoutExpired:
            print(f"  ⚠ Refresh timed out, continuing anyway")
        except Exception as e:
            # Don't fail the operation if refresh fails
            print(f"  ⚠ Could not refresh state: {e}")
            print(f"    Continuing with potentially stale state...")

    def _check_pending_operations(self) -> list:
        """Check for pending operations in the stack.

        Returns:
            List of pending operation URNs
        """
        import json  # Import at module level to avoid scope issues

        try:
            # Use pulumi stack export to check for pending operations
            result = run_pulumi_command(
                ["pulumi", "stack", "export", "-s", self._get_stack_name()],
                cwd=self.work_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                stack_data = json.loads(result.stdout)
                deployment = stack_data.get("deployment", {})
                pending = deployment.get("pending_operations", [])
                return pending if pending else []

        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
            pass

        return []

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
