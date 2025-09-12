"""Development wire function for testing."""

import importlib
from modelops.services.ipc import to_ipc_tables


def wire_function(entrypoint: str, params: dict, seed: int) -> dict:
    """Simple wire function for development testing.
    
    Args:
        entrypoint: Module path and function name
        params: Parameters dict
        seed: Random seed
        
    Returns:
        Dict[str, bytes] of IPC tables
    """
    try:
        # Parse entrypoint
        if "/" in entrypoint:
            module_path, _ = entrypoint.rsplit("/", 1)
        else:
            module_path = entrypoint
            
        # Import and run the function
        if "." in module_path:
            module_name, func_name = module_path.rsplit(".", 1)
        else:
            module_name = module_path
            func_name = "main"
            
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
        
        # Execute simulation
        result = func(params, seed)
        
        # Convert to IPC
        return to_ipc_tables(result)
        
    except Exception as e:
        # Return error as IPC table
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
        return to_ipc_tables({"error": error_msg})