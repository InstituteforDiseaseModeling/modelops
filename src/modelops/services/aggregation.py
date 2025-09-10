"""Aggregation service for combining simulation results.

This module provides aggregation-specific functionality separate from
simulation caching, since aggregation has different identity requirements.
"""

import logging
from typing import List, Optional, Union, Callable
from modelops_contracts import SimReturn, AggregatorFunction
from .storage import StorageBackend, get_default_backend
from .cache_codec import encode_zip, decode_zip
from .execution_context import ExecContext, AggregationContext
from .utils import resolve_function, import_function

logger = logging.getLogger(__name__)


class AggregationService:
    """Service for storing and retrieving aggregated results.
    
    Aggregates have a different identity than simulations:
    - They depend on the aggregator function
    - They combine multiple simulation results
    - They use AggregationContext instead of ExecContext
    
    Storage structure:
        aggregates/v1/<agg_hash>/<param_id>/aggregate.mops
    """
    
    def __init__(self, backend: Optional[StorageBackend] = None):
        """Initialize aggregation service.
        
        Args:
            backend: Storage backend (uses default if not provided)
        """
        self.backend = backend or get_default_backend()
        self.storage_prefix = "aggregates/v1"
    
    def make_aggregate_key(self, agg_context: AggregationContext, param_id: str) -> str:
        """Generate storage key for aggregate result.
        
        Args:
            agg_context: Aggregation context
            param_id: Parameter ID (for batch aggregations)
            
        Returns:
            Storage key string
        """
        agg_hash = agg_context.compute_hash()
        return f"{self.storage_prefix}/{agg_hash}/{param_id}/aggregate.mops"
    
    def exists(self, agg_context: AggregationContext, param_id: str) -> bool:
        """Check if aggregate exists.
        
        Args:
            agg_context: Aggregation context
            param_id: Parameter ID
            
        Returns:
            True if aggregate exists
        """
        try:
            key = self.make_aggregate_key(agg_context, param_id)
            return self.backend.exists(key)
        except Exception as e:
            logger.error(f"Error checking aggregate: {e}")
            return False
    
    def get(self, agg_context: AggregationContext, param_id: str) -> Optional[SimReturn]:
        """Retrieve stored aggregate.
        
        Args:
            agg_context: Aggregation context
            param_id: Parameter ID
            
        Returns:
            Aggregated result or None
        """
        try:
            key = self.make_aggregate_key(agg_context, param_id)
            if not self.backend.exists(key):
                return None
            
            blob = self.backend.load(key)
            result = decode_zip(blob, validate=True)
            logger.info(f"Retrieved aggregate for param_id={param_id[:8]}...")
            return result
            
        except Exception as e:
            logger.error(f"Error retrieving aggregate: {e}")
            return None
    
    def put(self, agg_context: AggregationContext, param_id: str, 
            result: SimReturn) -> None:
        """Store aggregate result.
        
        Args:
            agg_context: Aggregation context
            param_id: Parameter ID
            result: Aggregated result
        """
        try:
            key = self.make_aggregate_key(agg_context, param_id)
            
            # Encode with aggregation metadata
            blob = encode_zip(
                result,
                params={"aggregator": agg_context.aggregator_ref},
                fn_ref=agg_context.aggregator_ref
            )
            
            self.backend.save(key, blob)
            logger.info(f"Stored aggregate for param_id={param_id[:8]}...")
            
        except Exception as e:
            logger.error(f"Error storing aggregate: {e}")
    
    def aggregate_with_cache(self, results: List[SimReturn],
                             aggregator: Union[str, AggregatorFunction],
                             input_context: ExecContext,
                             param_id: str) -> SimReturn:
        """Aggregate results with caching support.
        
        Args:
            results: List of simulation results to aggregate
            aggregator: Aggregator function (string ref or callable)
            input_context: Context of simulations being aggregated
            param_id: Parameter ID for the aggregation
            
        Returns:
            Aggregated result (from cache or computed)
        """
        is_distributed, resolved = resolve_function(aggregator)
        
        # Create aggregation context
        aggregator_ref = resolved if is_distributed else f"callable:{id(resolved)}"
        agg_context = AggregationContext(
            aggregator_ref=aggregator_ref,
            input_context=input_context
        )
        
        # Check cache first
        cached = self.get(agg_context, param_id)
        if cached is not None:
            return cached
        
        # Compute aggregation
        if is_distributed:
            aggregator_fn = import_function(resolved)
        else:
            aggregator_fn = resolved
        
        result = aggregator_fn(results)
        
        # Store in cache (only for string refs, not callables)
        if is_distributed:
            self.put(agg_context, param_id, result)
        
        return result