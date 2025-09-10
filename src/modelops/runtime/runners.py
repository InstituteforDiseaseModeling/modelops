"""Simulation runner implementations for different execution strategies."""

import os
import importlib
from typing import Protocol, Optional, Dict
from pathlib import Path
from modelops_contracts import SimReturn
from .environment import ensure_bundle, ensure_venv, run_in_env
from ..services.ipc import validate_sim_return


class SimulationRunner(Protocol):
    """Base protocol for simulation runners.
    
    This is an internal ModelOps protocol for how simulations are executed.
    External systems use SimulationService, not this runner protocol.
    """
    
    def run(self, fn_ref: str, params: dict, seed: int, bundle_ref: str) -> SimReturn:
        """Execute a single simulation.
        
        Args:
            fn_ref: Function reference as "module.function" or "module:function"
            params: Parameter dictionary with scalar values
            seed: Random seed for reproducibility
            bundle_ref: Bundle reference for code/data dependencies
            
        Returns:
            SimReturn with named tables as IPC bytes
        """
        ...


class DirectRunner:
    """Direct execution - just import and run (for simple tests).
    
    This runner assumes the simulation code is already installed in the
    current Python environment. Used for development and simple testing.
    """
    
    def run(self, fn_ref: str, params: dict, seed: int, bundle_ref: str) -> SimReturn:
        """Execute simulation by direct import.
        
        Ignores bundle_ref and assumes code is pre-installed.
        """
        # Parse function reference - handle both dot and colon notation
        if ":" in fn_ref:
            # Traditional colon notation: "module:function"
            module_name, func_name = fn_ref.split(":")
        else:
            # Dot notation from EntryPointId: "module.function"
            # Assume last component is the function/class name
            parts = fn_ref.rsplit(".", 1)
            if len(parts) == 2:
                module_name, func_name = parts
            else:
                # Single component - treat as module with same-named function
                module_name = fn_ref
                func_name = fn_ref.split(".")[-1]
        
        mod = importlib.import_module(module_name)
        func = getattr(mod, func_name)
        
        # Call the simulation function
        result = func(params, seed)
        
        # Validate and return
        return validate_sim_return(result)


class BundleRunner:
    """Full bundle-aware execution with isolated environments.
    
    This runner fetches bundles, creates isolated virtual environments,
    and executes simulations in complete isolation.
    """
    
    def run(self, fn_ref: str, params: dict, seed: int, bundle_ref: str) -> SimReturn:
        """Execute simulation in isolated bundle environment.
        
        Steps:
        1. Ensure bundle is available locally
        2. Create or reuse virtual environment
        3. Execute in isolated environment
        """
        if not bundle_ref:
            raise ValueError("BundleRunner requires a bundle_ref")
        
        # Extract digest from bundle_ref using proper parsing
        try:
            from modelops_bundle.digest_utils import extract_digest_from_ref
            digest = extract_digest_from_ref(bundle_ref)
            if not digest:
                # No digest in ref, use the ref as-is (might be a tag)
                digest = bundle_ref
        except ImportError:
            # Fallback if modelops-bundle not installed
            # For now, assume bundle_ref is the digest directly
            digest = bundle_ref.split(":")[-2] + ":" + bundle_ref.split(":")[-1] if ":" in bundle_ref else bundle_ref
        
        # Ensure bundle is available
        bundle_dir = ensure_bundle(digest)
        
        # Ensure venv exists
        venv = ensure_venv(digest, bundle_dir)
        
        # Run in isolated environment
        output_bytes = run_in_env(venv, fn_ref, params, seed, bundle_dir)
        
        # Convert output format if needed
        # run_in_env returns dict[str, bytes], which matches SimReturn
        return output_bytes


class CachedBundleRunner:
    """Bundle runner with caching for repeated runs.
    
    This runner maintains a cache of virtual environments to avoid
    recreation overhead when running multiple simulations from the
    same bundle.
    """
    
    def __init__(self, max_cache_size: int = 10):
        """Initialize with cache settings.
        
        Args:
            max_cache_size: Maximum number of venvs to keep in cache
        """
        self._venv_cache: Dict[str, Path] = {}
        self._bundle_cache: Dict[str, Path] = {}
        self.max_cache_size = max_cache_size
    
    def run(self, fn_ref: str, params: dict, seed: int, bundle_ref: str) -> SimReturn:
        """Execute simulation with caching.
        
        Reuses virtual environments and bundle directories across runs
        to improve performance for repeated simulations.
        """
        if not bundle_ref:
            raise ValueError("CachedBundleRunner requires a bundle_ref")
        
        # Extract digest
        digest = bundle_ref.split(":")[-2] + ":" + bundle_ref.split(":")[-1] if ":" in bundle_ref else bundle_ref
        
        # Check bundle cache
        if digest not in self._bundle_cache:
            if len(self._bundle_cache) >= self.max_cache_size:
                # Evict oldest entry (simple FIFO for now)
                oldest = next(iter(self._bundle_cache))
                del self._bundle_cache[oldest]
                if oldest in self._venv_cache:
                    del self._venv_cache[oldest]
            
            self._bundle_cache[digest] = ensure_bundle(digest)
        
        bundle_dir = self._bundle_cache[digest]
        
        # Check venv cache
        if digest not in self._venv_cache:
            self._venv_cache[digest] = ensure_venv(digest, bundle_dir)
        
        venv = self._venv_cache[digest]
        
        # Run in cached environment
        output_bytes = run_in_env(venv, fn_ref, params, seed, bundle_dir)
        return output_bytes
    
    def clear_cache(self):
        """Clear all cached environments."""
        self._venv_cache.clear()
        self._bundle_cache.clear()


def get_runner(runner_type: Optional[str] = None) -> SimulationRunner:
    """Factory function to get appropriate runner.
    
    Args:
        runner_type: Type of runner ("direct", "bundle", "cached")
                    If None, uses MODELOPS_RUNNER_TYPE env var or defaults to "direct"
    
    Returns:
        SimulationRunner instance
    """
    if runner_type is None:
        runner_type = os.getenv("MODELOPS_RUNNER_TYPE", "direct")
    
    if runner_type == "direct":
        return DirectRunner()
    elif runner_type == "bundle":
        return BundleRunner()
    elif runner_type == "cached":
        return CachedBundleRunner()
    else:
        raise ValueError(f"Unknown runner type: {runner_type}")
