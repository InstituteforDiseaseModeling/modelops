#!/usr/bin/env python
"""User simulation runner for isolated execution.

This module runs inside isolated virtual environments and handles:
1. Reading parameters from stdin (JSON)
2. Importing and executing user functions
3. Returning results via stdout (JSON with base64-encoded tables)

This is invoked as: python -m modelops_user_runner module:function
"""

import sys
import json
import base64
import importlib
import traceback
from typing import Dict, Any


def encode_output(data: Any) -> str:
    """Encode data as base64 string.
    
    For now using base64, but could use hex or other encoding.
    """
    if isinstance(data, bytes):
        return base64.b64encode(data).decode('ascii')
    elif isinstance(data, str):
        return base64.b64encode(data.encode('utf-8')).decode('ascii')
    else:
        # Try to serialize as JSON then encode
        json_str = json.dumps(data)
        return base64.b64encode(json_str.encode('utf-8')).decode('ascii')


def main():
    """Main entry point for isolated user runner."""
    
    if len(sys.argv) < 2:
        print(json.dumps({
            "error": "Usage: python -m modelops_user_runner module:function"
        }), file=sys.stderr)
        sys.exit(1)
    
    fn_ref = sys.argv[1]
    
    try:
        # Read input from stdin
        input_data = json.loads(sys.stdin.read())
        params = input_data.get("params", {})
        seed = input_data.get("seed", 0)
        
        # Parse function reference
        module_name, func_name = fn_ref.split(":")
        
        # Import and get function
        mod = importlib.import_module(module_name)
        func = getattr(mod, func_name)
        
        # Execute simulation
        result = func(params, seed)
        
        # Convert result to output format
        # Result should be a dict of named tables (bytes)
        if isinstance(result, dict):
            # Encode each table as base64
            output = {}
            for name, table_bytes in result.items():
                if isinstance(table_bytes, bytes):
                    output[name] = base64.b64encode(table_bytes).decode('ascii')
                else:
                    # Handle non-bytes data by converting to JSON then base64
                    output[name] = encode_output(table_bytes)
        else:
            # Wrap non-dict results
            output = {"result": encode_output(result)}
        
        # Write to stdout
        print(json.dumps(output))
        
    except Exception as e:
        # Report errors via stderr and exit with error code
        error_info = {
            "error": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc()
        }
        print(json.dumps(error_info), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()