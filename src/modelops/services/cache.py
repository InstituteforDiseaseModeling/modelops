"""Simulation cache for result deduplication and reuse."""

import logging
import pickle  # For legacy cache reading only
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Callable, Any
from modelops_contracts import SimReturn, make_param_id, SimTask, shard
from .storage import StorageBackend, get_default_backend
from .cache_codec import encode_zip, decode_zip

logger = logging.getLogger(__name__)


@dataclass
class CacheConfig:
    """Configuration for simulation cache."""
    
    enabled: bool = True
    """Whether caching is enabled."""
    
    ttl_seconds: Optional[int] = None
    """Optional TTL for cache entries (not enforced in this implementation)."""
    
    storage_prefix: str = "cache/v2"
    """Key prefix for all cache entries (includes version)."""
    
    save_metadata: bool = True
    """Whether to save metadata about cached results."""


class SimulationCache:
    """Cache simulation results using parameter IDs.
    
    Uses UniqueParameterSet's stable param_id for deduplication,
    enabling result reuse across runs and avoiding redundant computation.
    
    Storage structure:
        cache/v1/<ctx_hash>/<param_id>/<seed>.mops     # Simulation result
        cache/v1/<ctx_hash>/<param_id>/metadata.json   # Parameter metadata
    
    Examples:
        >>> from modelops.services.storage import LocalFileBackend
        >>> backend = LocalFileBackend("/tmp/test_cache")
        >>> cache = SimulationCache(backend=backend)
        
        >>> # Check if result exists
        >>> params = {"beta": 0.5, "gamma": 0.1}
        >>> if cache.exists(params, seed=42):
        ...     result = cache.get(params, seed=42)
        
        >>> # Store result
        >>> cache.put(params, seed=42, result=sim_output)
        
        >>> # Get or compute pattern
        >>> result = cache.get_or_compute(
        ...     params, seed=42,
        ...     compute_fn=lambda p, s: run_simulation(p, s)
        ... )
    """
    
    def __init__(self, backend: Optional[StorageBackend] = None,
                 config: CacheConfig = CacheConfig()):
        """Initialize cache with pluggable backend.
        
        Args:
            backend: Any StorageBackend implementation (optional).
                    If None, uses default backend based on environment.
            config: Cache configuration
        """
        self.backend = backend or get_default_backend()
        self.config = config
        
        if not config.enabled:
            logger.info("Simulation cache disabled")
        else:
            logger.info(f"Simulation cache initialized with prefix: {config.storage_prefix}")
    
    def make_cache_key(self, task: SimTask) -> str:
        """Generate cache key from SimTask.
        
        Args:
            task: SimTask to generate key for
            
        Returns:
            Cache key string in format: cache/v2/<sim_root_shard>/<param_id>/<seed>.mops
        """
        from ..utils.shard import shard
        
        sim_root = task.sim_root()
        sim_root_shard = shard(sim_root, depth=2, width=2)
        param_id = task.params.param_id
        seed = task.seed
        
        return f"{self.config.storage_prefix}/{sim_root_shard}/{param_id}/{seed}.mops"
    
    def make_metadata_key(self, task: SimTask) -> str:
        """Generate metadata key for parameter set.
        
        Args:
            task: SimTask to generate metadata key for
            
        Returns:
            Metadata key string
        """
        from ..utils.shard import shard
        
        sim_root = task.sim_root()
        sim_root_shard = shard(sim_root, depth=2, width=2)
        param_id = task.params.param_id
        
        return f"{self.config.storage_prefix}/{sim_root_shard}/{param_id}/metadata.json"
    
    def exists(self, task: SimTask) -> bool:
        """Check if result exists in cache.
        
        Args:
            task: SimTask to check
            
        Returns:
            True if cached result exists
        """
        if not self.config.enabled:
            return False
        
        try:
            cache_key = self.make_cache_key(task)
            exists = self.backend.exists(cache_key)
            
            if exists:
                logger.debug(f"Cache hit for task_id={task.task_id()[:8]}...")
            
            return exists
        except Exception as e:
            logger.error(f"Error checking cache: {e}")
            return False
    
    def get(self, task: SimTask) -> Optional[SimReturn]:
        """Retrieve cached result if available.
        
        Args:
            task: SimTask to retrieve result for
            
        Returns:
            Cached SimReturn or None if not found
        """
        if not self.config.enabled:
            return None
        
        try:
            cache_key = self.make_cache_key(task)
            
            if not self.backend.exists(cache_key):
                return None
            
            blob = self.backend.load(cache_key)
            logger.info(f"Retrieved cached result for task_id={task.task_id()[:8]}...")
            
            # Try new ZIP format first
            try:
                return decode_zip(blob, validate=True)
            except Exception:
                # Fall back to legacy pickle format for old cache entries
                try:
                    result = pickle.loads(blob)
                    if isinstance(result, dict):
                        logger.debug("Loaded legacy pickle cache entry")
                        return result
                except Exception:
                    pass
                
                logger.warning(f"Unable to decode cache entry for {task.task_id()[:8]}")
                return None
            
        except Exception as e:
            logger.error(f"Error retrieving from cache: {e}")
            return None
    
    def put(self, task: SimTask, result: SimReturn) -> None:
        """Store result in cache.
        
        Args:
            task: SimTask that produced the result
            result: Simulation result to cache
        """
        if not self.config.enabled:
            return
        
        try:
            cache_key = self.make_cache_key(task)
            
            # Update metadata with this seed
            if self.config.save_metadata:
                metadata_key = self.make_metadata_key(task)
                
                if self.backend.exists(metadata_key):
                    # Update existing metadata
                    metadata = self.backend.load_json(metadata_key)
                    seeds = metadata.get("seeds_computed", [])
                else:
                    # Create new metadata
                    metadata = {
                        "param_id": task.params.param_id,
                        "params": dict(task.params.params),
                        "first_seen": datetime.now().isoformat(),
                        "seeds_computed": [],
                        "sim_root": task.sim_root(),
                    }
                    seeds = []
                
                # Add this seed if not already present
                if task.seed not in seeds:
                    seeds.append(task.seed)
                    seeds.sort()  # Keep sorted for readability
                
                metadata["seeds_computed"] = seeds
                metadata["last_updated"] = datetime.now().isoformat()
                metadata["total_seeds"] = len(seeds)
                
                self.backend.save_json(metadata_key, metadata)
                logger.debug(f"Updated metadata for param_id={task.params.param_id[:8]}... (seeds: {len(seeds)})")
            
            # Store result using new ZIP codec
            # This provides deterministic, safe, and portable serialization
            blob = encode_zip(result, params=dict(task.params.params), seed=task.seed, fn_ref=str(task.entrypoint))
            self.backend.save(cache_key, blob)
            logger.info(f"Cached result for param_id={task.params.param_id[:8]}..., seed={task.seed}")
            
        except ValueError as e:
            # No context available
            logger.debug(f"Cache store skipped: {e}")
        except Exception as e:
            logger.error(f"Error storing in cache: {e}")
    
    def get_or_compute(self, task: SimTask,
                       compute_fn: Callable[[SimTask], SimReturn]) -> SimReturn:
        """Check cache first, compute if missing.
        
        This is the main pattern for using the cache - it handles
        both cache hits and misses transparently.
        
        Args:
            params: Parameter dictionary
            seed: Random seed
            compute_fn: Function to compute result if not cached
            context: Execution context (uses self.context if not provided)
            
        Returns:
            Simulation result (from cache or freshly computed)
            
        Examples:
            >>> result = cache.get_or_compute(
            ...     {"beta": 0.5}, 
            ...     seed=42,
            ...     compute_fn=lambda p, s: simulation_service.submit_and_gather(
            ...         "model:simulate", p, s, bundle_ref="v1"
            ...     )
            ... )
        """
        # Check cache first
        cached = self.get(params, seed, context)
        if cached is not None:
            return cached
        
        # Compute if not cached
        logger.debug(f"Cache miss, computing for param_id={make_param_id(params)[:8]}..., seed={seed}")
        result = compute_fn(params, seed)
        
        # Store in cache for next time
        self.put(params, seed, result, context)
        
        return result
    
    def clear_sim_root(self, sim_root: str) -> int:
        """Clear all cached results for a simulation root.
        
        Args:
            sim_root: Simulation root hash to clear
            
        Returns:
            Number of entries cleared
        """
        if not self.config.enabled:
            return 0
        
        try:
            # Use sharded path for the sim_root
            sim_root_shard = shard(sim_root, depth=2, width=2)
            prefix = f"{self.config.storage_prefix}/{sim_root_shard}/"
            keys = self.backend.list_keys(prefix)
            
            for key in keys:
                self.backend.delete(key)
            
            logger.info(f"Cleared {len(keys)} cache entries for sim_root={sim_root[:8]}...")
            return len(keys)
            
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")
            return 0
    
    def clear_all(self) -> int:
        """Clear entire cache.
        
        Returns:
            Number of entries cleared
        """
        if not self.config.enabled:
            return 0
        
        try:
            keys = self.backend.list_keys(self.config.storage_prefix)
            
            for key in keys:
                self.backend.delete(key)
            
            logger.info(f"Cleared {len(keys)} cache entries")
            return len(keys)
            
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")
            return 0
    
    def stats(self) -> dict:
        """Get cache statistics.
        
        Returns:
            Dictionary with cache stats
        """
        try:
            keys = self.backend.list_keys(self.config.storage_prefix)
            
            # Count unique sim_roots and param_ids
            sim_roots = set()
            param_ids = set()
            for key in keys:
                parts = key.split("/")
                # Format: cache/v2/ab/cd/<sim_root>/<param_id>/<seed>.mops
                if len(parts) >= 6:
                    # Reconstruct sim_root from sharded path
                    sim_root = parts[4]  # The full hash after shards
                    param_id = parts[5]
                    sim_roots.add(sim_root[:8])  # Store prefix for stats
                    param_ids.add(param_id[:8])
            
            return {
                "enabled": self.config.enabled,
                "total_entries": len(keys),
                "unique_sim_roots": len(sim_roots),
                "unique_param_ids": len(param_ids),
                "storage_prefix": self.config.storage_prefix,
                "backend_type": type(self.backend).__name__
            }
            
        except Exception as e:
            logger.error(f"Error getting cache stats: {e}")
            return {
                "enabled": self.config.enabled,
                "error": str(e)
            }