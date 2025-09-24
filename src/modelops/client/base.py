"""Base classes for service layer with unified contracts."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Dict, Any, Optional, List, Callable
import time
import random
import json


class ComponentState(str, Enum):
    """Unified state for infrastructure components."""
    NOT_DEPLOYED = "NotDeployed"
    DEPLOYING = "Deploying"
    READY = "Ready"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


@dataclass
class ComponentStatus:
    """Unified status contract for all services."""
    deployed: bool
    phase: ComponentState
    details: Dict[str, Any]

    def to_json(self) -> dict:
        """JSON-serializable representation."""
        return {
            "deployed": self.deployed,
            "phase": self.phase.value,
            "details": self.details
        }

    def to_dict(self) -> dict:
        """Dictionary representation."""
        return asdict(self)


@dataclass
class InfraResult:
    """Result of infrastructure operations."""
    success: bool
    components: Dict[str, ComponentState]
    outputs: Dict[str, Dict[str, Any]]
    errors: Dict[str, str]
    logs_path: Optional[str] = None

    def to_json(self) -> str:
        """JSON representation for CLI output."""
        data = {
            "success": self.success,
            "components": {k: v.value for k, v in self.components.items()},
            "outputs": self.outputs,
            "errors": self.errors,
            "logs_path": self.logs_path
        }
        return json.dumps(data, indent=2, default=str)


class BaseService(ABC):
    """Base class for all component services."""

    def __init__(self, env: str):
        """Initialize service with environment."""
        self.env = env

    @abstractmethod
    def provision(self, config: Any, verbose: bool = False) -> Dict[str, Any]:
        """Provision the component."""
        pass

    @abstractmethod
    def destroy(self, verbose: bool = False) -> None:
        """Destroy the component."""
        pass

    @abstractmethod
    def status(self) -> ComponentStatus:
        """Get component status with unified contract."""
        pass

    def with_retry(
        self,
        func: Callable,
        max_retries: int = 3,
        base_delay: float = 1.0
    ) -> Any:
        """
        Retry wrapper for transient failures.

        Args:
            func: Function to retry
            max_retries: Maximum number of retry attempts
            base_delay: Base delay in seconds (exponential backoff)

        Returns:
            Function result

        Raises:
            Last exception if all retries fail
        """
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                return func()
            except Exception as e:
                last_error = e
                error_str = str(e)

                # Check if it's a retryable error
                retryable_errors = [
                    "429",  # Too Many Requests
                    "503",  # Service Unavailable
                    "504",  # Gateway Timeout
                    "RequestTimeout",
                    "Throttling",
                    "TooManyRequests",
                    "ServiceUnavailable",
                    "lock",  # Resource lock
                    "Conflict",  # ARM conflicts
                ]

                if not any(err in error_str for err in retryable_errors):
                    # Not retryable, raise immediately
                    raise

                if attempt < max_retries:
                    # Exponential backoff with jitter
                    wait_time = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    print(f"  Retrying after {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                else:
                    # Final attempt failed
                    raise

        # Should never reach here, but for safety
        if last_error:
            raise last_error


class OutputCapture:
    """Captures output for clean display management."""

    def __init__(
        self,
        verbose: bool = False,
        progress_callback: Optional[Callable[[str], None]] = None
    ):
        """
        Initialize output capture.

        Args:
            verbose: If True, show all output
            progress_callback: Optional callback for progress updates
        """
        self.verbose = verbose
        self.buffer = []
        self.progress_callback = progress_callback or (lambda x: None)
        self._progress_count = 0
        self._last_progress_time = time.time()

    def __call__(self, message: str):
        """Capture output from Pulumi or other sources."""
        self.buffer.append(message)

        if self.verbose:
            # Show everything in verbose mode
            print(message, end="")
        else:
            # In quiet mode, show progress indicators
            if self._is_progress_message(message):
                self._show_progress()
            elif self._is_important_message(message):
                # Show important messages even in quiet mode
                print(f"\n  {message.strip()}")

    def _is_progress_message(self, msg: str) -> bool:
        """Check if message indicates progress."""
        progress_indicators = [
            "Creating",
            "Updating",
            "Reading",
            "Deleting",
            "Refreshing",
            "Provisioning",
            "Configuring",
            "Installing",
            "Deploying"
        ]
        return any(indicator in msg for indicator in progress_indicators)

    def _is_important_message(self, msg: str) -> bool:
        """Check if message is important enough to show."""
        important = [
            "error:",
            "Error:",
            "ERROR",
            "failed",
            "Failed",
            "FAILED",
            "warning:",
            "Warning:",
            "WARNING"
        ]
        return any(imp in msg for imp in important)

    def _show_progress(self):
        """Show progress indicator."""
        self._progress_count += 1
        current_time = time.time()

        # Show dot every 10 operations or every 2 seconds
        if (self._progress_count % 10 == 0 or
            current_time - self._last_progress_time > 2):
            print(".", end="", flush=True)
            self._last_progress_time = current_time

    def get_output(self) -> str:
        """Get captured output as string."""
        return "".join(self.buffer)

    def get_lines(self) -> List[str]:
        """Get captured output as lines."""
        return self.buffer.copy()