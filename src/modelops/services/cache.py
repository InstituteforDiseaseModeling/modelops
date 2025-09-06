"""Simulation cache for result deduplication and reuse."""

import logging
import pickle  # For legacy cache reading only
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Callable, Any
from modelops_contracts import SimReturn, make_param_id
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
    
    storage_prefix: str = "cache"
    """Key prefix for all cache entries."""
    
    save_metadata: bool = True
    """Whether to save metadata about cached results."""


class SimulationCache:
    """Cache simulation results using parameter IDs.
    
    Uses UniqueParameterSet's stable param_id for deduplication,
    enabling result reuse across runs and avoiding redundant computation.
    
    Storage structure:
        cache/{param_id}/metadata.json  # Parameter metadata
        cache/{param_id}/seed_{seed}    # Simulation result for specific seed
    
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
    
    def make_cache_key(self, param_id: str, seed: int) -> str:
        """Generate cache key from param_id and seed.
        
        Args:
            param_id: Stable parameter ID from make_param_id()
            seed: Random seed
            
        Returns:
            Cache key string
        """
        return f"{self.config.storage_prefix}/{param_id}/seed_{seed}"
    
    def make_metadata_key(self, param_id: str) -> str:
        """Generate metadata key for parameter set.
        
        Args:
            param_id: Stable parameter ID
            
        Returns:
            Metadata key string
        """
        return f"{self.config.storage_prefix}/{param_id}/metadata.json"
    
    def exists(self, params: dict, seed: int) -> bool:
        """Check if result exists in cache.
        
        Args:
            params: Parameter dictionary
            seed: Random seed
            
        Returns:
            True if cached result exists
        """
        if not self.config.enabled:
            return False
        
        try:
            param_id = make_param_id(params)
            cache_key = self.make_cache_key(param_id, seed)
            exists = self.backend.exists(cache_key)
            
            if exists:
                logger.debug(f"Cache hit for param_id={param_id[:8]}..., seed={seed}")
            
            return exists
        except Exception as e:
            logger.error(f"Error checking cache: {e}")
            return False
    
    def get(self, params: dict, seed: int) -> Optional[SimReturn]:
        """Retrieve cached result if available.
        
        Args:
            params: Parameter dictionary
            seed: Random seed
            
        Returns:
            Cached SimReturn (dict[str, bytes]) or None if not found
        """
        if not self.config.enabled:
            return None
        
        try:
            param_id = make_param_id(params)
            cache_key = self.make_cache_key(param_id, seed)
            
            if not self.backend.exists(cache_key):
                return None
            
            blob = self.backend.load(cache_key)
            logger.info(f"Retrieved cached result for param_id={param_id[:8]}..., seed={seed}")
            
            # Try new ZIP format first
            try:
                return decode_zip(blob, validate=True)
            except Exception:
                # Fall back to legacy pickle format
                try:
                    result = pickle.loads(blob)
                    if isinstance(result, dict):
                        logger.debug("Loaded legacy pickle cache entry")
                        return result
                except Exception:
                    pass
                
                logger.warning(f"Unable to decode cache entry for {param_id[:8]}/{seed}")
                return None
            
        except Exception as e:
            logger.error(f"Error retrieving from cache: {e}")
            return None
    
    def put(self, params: dict, seed: int, result: SimReturn) -> None:
        """Store result in cache.
        
        Args:
            params: Parameter dictionary
            seed: Random seed  
            result: Simulation result (SimReturn dict[str, bytes])
        """
        if not self.config.enabled:
            return
        
        try:
            param_id = make_param_id(params)
            cache_key = self.make_cache_key(param_id, seed)
            
            # Update metadata with this seed
            if self.config.save_metadata:
                metadata_key = self.make_metadata_key(param_id)
                
                if self.backend.exists(metadata_key):
                    # Update existing metadata
                    metadata = self.backend.load_json(metadata_key)
                    seeds = metadata.get("seeds_computed", [])
                else:
                    # Create new metadata
                    metadata = {
                        "param_id": param_id,
                        "params": params,
                        "first_seen": datetime.now().isoformat(),
                        "seeds_computed": []
                    }
                    seeds = []
                
                # Add this seed if not already present
                if seed not in seeds:
                    seeds.append(seed)
                    seeds.sort()  # Keep sorted for readability
                
                metadata["seeds_computed"] = seeds
                metadata["last_updated"] = datetime.now().isoformat()
                metadata["total_seeds"] = len(seeds)
                
                self.backend.save_json(metadata_key, metadata)
                logger.debug(f"Updated metadata for param_id={param_id[:8]}... (seeds: {len(seeds)})")
            
            # Store result using new ZIP codec
            # This provides deterministic, safe, and portable serialization
            blob = encode_zip(result, params=params, seed=seed)
            self.backend.save(cache_key, blob)
            logger.info(f"Cached result for param_id={param_id[:8]}..., seed={seed}")
            
        except Exception as e:
            logger.error(f"Error storing in cache: {e}")
    
    def get_or_compute(self, params: dict, seed: int, 
                       compute_fn: Callable[[dict, int], SimReturn]) -> SimReturn:
        """Check cache first, compute if missing.
        
        This is the main pattern for using the cache - it handles
        both cache hits and misses transparently.
        
        Args:
            params: Parameter dictionary
            seed: Random seed
            compute_fn: Function to compute result if not cached
            
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
        cached = self.get(params, seed)
        if cached is not None:
            return cached
        
        # Compute if not cached
        logger.debug(f"Cache miss, computing for param_id={make_param_id(params)[:8]}..., seed={seed}")
        result = compute_fn(params, seed)
        
        # Store in cache for next time
        self.put(params, seed, result)
        
        return result
    
    def clear_param_id(self, param_id: str) -> int:
        """Clear all cached results for a parameter ID.
        
        Args:
            param_id: Parameter ID to clear
            
        Returns:
            Number of entries cleared
        """
        if not self.config.enabled:
            return 0
        
        try:
            prefix = f"{self.config.storage_prefix}/{param_id}/"
            keys = self.backend.list_keys(prefix)
            
            for key in keys:
                self.backend.delete(key)
            
            logger.info(f"Cleared {len(keys)} cache entries for param_id={param_id[:8]}...")
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
            
            # Count unique param_ids
            param_ids = set()
            for key in keys:
                parts = key.split("/")
                if len(parts) >= 2:
                    param_ids.add(parts[1])
            
            return {
                "enabled": self.config.enabled,
                "total_entries": len(keys),
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