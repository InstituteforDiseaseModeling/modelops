"""Direct execution environment for testing."""

import hashlib
import importlib.metadata
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modelops_contracts import (
    AggregationReturn,
    AggregationTask,
    SimReturn,
    SimTask,
    TableArtifact,
)
from modelops_contracts.ports import BundleRepository, ExecutionEnvironment

logger = logging.getLogger(__name__)


class DirectExecEnv(ExecutionEnvironment):
    """Direct execution environment for testing.

    This environment executes tasks directly in the current process.
    Useful for testing and debugging, but provides no isolation.
    """

    def __init__(
        self,
        bundle_repo: BundleRepository,
        storage_dir: Path = None,
        azure_backend: dict[str, Any] | None = None,
    ):
        """Initialize the execution environment.

        Args:
            bundle_repo: Repository for fetching bundles
            storage_dir: Directory for provenance-based storage (optional)
            azure_backend: Azure backend configuration (ignored in direct mode)
        """
        self.bundle_repo = bundle_repo
        self.storage_dir = storage_dir or Path("/tmp/modelops/provenance")
        self._wire_fn_cache: dict[str, Callable] = {}  # Cache wire functions by bundle digest
        # Direct execution doesn't use remote storage
        if azure_backend:
            logger.info("DirectExecEnv ignores azure_backend (local-only execution)")

    def _discover_wire_function(self, bundle_path: Path) -> Callable:
        """Discover wire function via Python entry points.

        Similar to subprocess_runner but runs in-process.

        Args:
            bundle_path: Path to the bundle

        Returns:
            The wire function callable

        Raises:
            RuntimeError: If no wire function found
        """
        # Add bundle path to sys.path temporarily
        if str(bundle_path) not in sys.path:
            sys.path.insert(0, str(bundle_path))

        try:
            # Check if this is the dev bundle (current working directory)
            import os

            if str(bundle_path) == os.getcwd():
                # Use development wire function
                try:
                    from examples.dev_wire import wire_function

                    return wire_function
                except ImportError:
                    pass

            # Look for modelops.wire entry point
            eps = importlib.metadata.entry_points(group="modelops.wire")

            if not eps:
                # For testing, try to import directly from test bundle
                # This is a fallback for bundles without proper entry points
                try:
                    import test_bundle.wire

                    return test_bundle.wire.wire_function
                except ImportError:
                    raise RuntimeError(
                        "No modelops.wire entry point found. "
                        "Bundle should register: [project.entry-points.'modelops.wire'] "
                        "execute = 'module.wire:wire_function'"
                    )

            eps_list = list(eps)
            if len(eps_list) > 1:
                names = [ep.name for ep in eps_list]
                raise RuntimeError(f"Multiple modelops.wire entry points found: {names}")

            # Load the entry point
            ep = eps_list[0]
            logger.info(f"Using wire function from entry point: {ep.name} = {ep.value}")
            return ep.load()
        finally:
            # Clean up sys.path
            if str(bundle_path) in sys.path:
                sys.path.remove(str(bundle_path))

    def run(self, task: SimTask) -> SimReturn:
        """Execute a simulation task directly.

        Args:
            task: Simulation task to execute

        Returns:
            Simulation result with status and artifacts
        """
        try:
            # Ensure bundle is available locally
            digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)

            # Get or discover wire function for this bundle
            if digest not in self._wire_fn_cache:
                self._wire_fn_cache[digest] = self._discover_wire_function(bundle_path)
            wire_fn = self._wire_fn_cache[digest]

            # Add bundle path to sys.path for execution
            sys.path.insert(0, str(bundle_path))

            try:
                # Execute the wire function
                logger.info(f"Executing {task.entrypoint} with seed {task.seed}")

                # Call wire function - it returns Dict[str, bytes]
                result_bytes = wire_fn(
                    str(task.entrypoint) if task.entrypoint else "main",
                    dict(task.params.params),
                    task.seed,
                )

                # Process artifacts (always inline for direct execution)
                outputs = {}
                for name, data in result_bytes.items():
                    if not isinstance(data, bytes):
                        logger.warning(f"Wire function returned non-bytes for {name}, converting")
                        if isinstance(data, str):
                            data = data.encode()
                        else:
                            data = json.dumps(data).encode()

                    checksum = hashlib.blake2b(data, digest_size=32).hexdigest()
                    outputs[name] = TableArtifact(size=len(data), inline=data, checksum=checksum)

                # Create simple task_id from params and seed
                param_id = task.params.param_id
                seed_str = str(task.seed)
                output_names = tuple(outputs.keys())
                tid_components = f"{param_id[:16]}-{seed_str}-{','.join(sorted(output_names))}"
                tid = hashlib.blake2b(tid_components.encode(), digest_size=32).hexdigest()

                return SimReturn(task_id=tid, outputs=outputs)

            finally:
                # Clean up sys.path
                if str(bundle_path) in sys.path:
                    sys.path.remove(str(bundle_path))

        except Exception as e:
            logger.exception(f"Direct execution failed for bundle {task.bundle_ref}")
            # Create simple task_id for error case
            param_id = task.params.param_id
            tid_components = f"{param_id[:16]}-{task.seed}-error"
            tid = hashlib.blake2b(tid_components.encode(), digest_size=32).hexdigest()

            # Store error inline
            error_data = json.dumps(
                {
                    "error": str(e),
                    "type": type(e).__name__,
                    "params": dict(task.params.params),
                    "seed": task.seed,
                }
            ).encode()
            checksum = hashlib.sha256(error_data).hexdigest()

            return SimReturn(
                task_id=tid,
                outputs={
                    "error": TableArtifact(
                        size=len(error_data),
                        inline=error_data,  # Store inline instead of using CAS
                        checksum=checksum,
                    )
                },
            )

    def health_check(self) -> dict[str, Any]:
        """Check health of execution environment."""
        return {
            "type": "direct",
            "status": "healthy",
            "cached_wire_functions": len(self._wire_fn_cache),
        }

    def shutdown(self):
        """Shutdown the execution environment."""
        logger.info("Shutting down DirectExecEnv")
        self._wire_fn_cache.clear()

    def run_aggregation(self, task: AggregationTask) -> AggregationReturn:
        """Direct execution environment does not support aggregation."""
        raise NotImplementedError("DirectExecEnv does not support run_aggregation")
