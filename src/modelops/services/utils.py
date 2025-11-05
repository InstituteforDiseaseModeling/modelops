"""Utility functions for simulation services."""

import importlib
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T", bound=Callable)


def resolve_function(func: str | T) -> tuple[bool, str | T]:
    """Resolve a function reference to either string or callable.

    This helper is used throughout ModelOps for functions that need to
    run on workers (simulations, aggregators, etc.). It determines the
    execution strategy based on how the function is provided.

    The key insight: String references can be safely sent to distributed
    workers (they import the function themselves), while Python callables
    would need to be pickled, which is unreliable for lambdas, closures,
    and local functions. Therefore, callables trigger a "local execution"
    strategy where data is gathered from workers first, then the function
    runs on the client/scheduler.

    Args:
        func: Either a "module:function" string or a callable

    Returns:
        (is_distributed, resolved) where:
        - is_distributed=True: String ref that workers can import themselves.
                               Enables distributed execution ON workers with
                               minimal data transfer (best performance).
        - is_distributed=False: Callable that will be executed locally after
                                gathering all results from workers. This avoids
                                serialization issues but requires transferring
                                all data from workers (performance penalty).

    Performance implications:
        - String refs: Aggregation runs ON workers, only aggregated result transferred
        - Callables: All raw results transferred to client, then aggregated locally
        - For large datasets, string refs can be 100x faster

    Raises:
        ValueError: If string ref doesn't contain ':'
        TypeError: If func is neither string nor callable

    Examples:
        >>> # String ref - will run distributed on workers
        >>> is_dist, resolved = resolve_function("numpy:mean")
        >>> assert is_dist == True
        >>> assert resolved == "numpy:mean"

        >>> # Callable - will gather data then run locally
        >>> is_dist, resolved = resolve_function(lambda x: x)
        >>> assert is_dist == False
        >>> assert callable(resolved)
    """
    if callable(func):
        # Direct callable - requires local execution strategy (gather then aggregate)
        # to avoid unreliable pickling of Python functions
        return (False, func)
    elif isinstance(func, str):
        # String reference - enables distributed execution on workers
        # Workers import the function themselves, no serialization needed
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
