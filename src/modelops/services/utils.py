"""Utility functions for simulation services."""

from typing import Union, Callable, TypeVar, Tuple
import importlib

T = TypeVar('T', bound=Callable)

def resolve_function(func: Union[str, T]) -> Tuple[bool, Union[str, T]]:
    """Resolve a function reference to either string or callable.
    
    This helper is used throughout ModelOps for functions that need to
    run on workers (simulations, aggregators, etc.)
    
    Args:
        func: Either a "module:function" string or a callable
    
    Returns:
        (is_distributed, resolved) where:
        - is_distributed=True: use string ref for worker execution
        - is_distributed=False: use callable directly (local only)
    
    Raises:
        ValueError: If string ref doesn't contain ':'
        TypeError: If func is neither string nor callable
    
    Examples:
        >>> is_dist, resolved = resolve_function("numpy:mean")
        >>> assert is_dist == True
        >>> assert resolved == "numpy:mean"
        
        >>> is_dist, resolved = resolve_function(lambda x: x)
        >>> assert is_dist == False
        >>> assert callable(resolved)
    """
    if callable(func):
        # Direct callable - local execution only
        return (False, func)
    elif isinstance(func, str):
        # String reference - can be used for distributed execution
        if ":" not in func:
            raise ValueError(f"Invalid function ref '{func}'. Must be 'module:function'")
        return (True, func)
    else:
        raise TypeError(f"Expected string ref or callable, got {type(func).__name__}")

def import_function(ref: str) -> Callable:
    """Import a function from a module:function reference.
    
    Args:
        ref: String like "module.submodule:function_name"
    
    Returns:
        The imported function
        
    Raises:
        ValueError: If ref doesn't contain ':'
        ImportError: If module can't be imported
        AttributeError: If function doesn't exist in module
    
    Examples:
        >>> mean_func = import_function("numpy:mean")
        >>> import numpy as np
        >>> assert mean_func is np.mean
    """
    if ":" not in ref:
        raise ValueError(f"Invalid function ref '{ref}'. Must be 'module:function'")
    
    module_name, func_name = ref.split(":", 1)
    
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ImportError(f"Failed to import module '{module_name}': {e}")
    
    try:
        return getattr(module, func_name)
    except AttributeError:
        raise AttributeError(f"Module '{module_name}' has no function '{func_name}'")