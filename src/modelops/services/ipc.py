"""IPC conversion utilities for SimulationService contract compliance.

This module provides functions to convert between Python objects and
Arrow IPC bytes as required by the modelops-contracts SimReturn type.

Uses Polars DataFrames exclusively for the MVP (no pandas).
"""

from typing import Any, Dict, Mapping
import io

import pyarrow as pa
import polars as pl


def to_ipc_tables(data: Dict[str, Any]) -> Mapping[str, bytes]:
    """Convert simulation outputs to IPC bytes format.
    
    Converts each value in the dictionary to Arrow IPC bytes.
    This ensures compliance with the SimReturn = Mapping[str, TableIPC] contract.
    
    Args:
        data: Dictionary of named outputs from simulation
        
    Returns:
        Dictionary mapping names to IPC bytes
        
    Raises:
        ValueError: If data cannot be converted
    """
    result = {}
    for name, value in data.items():
        # Convert different data types to Arrow tables
        if isinstance(value, pl.DataFrame):
            # Convert polars DataFrame to Arrow
            table = value.to_arrow()
        elif isinstance(value, pa.Table):
            # Already an Arrow table
            table = value
        elif isinstance(value, dict):
            # Convert dict to Arrow table via polars for consistency
            # This ensures proper type handling
            df = pl.DataFrame(value)
            table = df.to_arrow()
        elif isinstance(value, list):
            # Convert list to single-column table via polars
            df = pl.DataFrame({"values": value})
            table = df.to_arrow()
        else:
            # For scalar types, create a single-value table
            df = pl.DataFrame({"value": [value]})
            table = df.to_arrow()
        
        # Convert to IPC bytes
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        
        result[name] = sink.getvalue().to_pybytes()
    
    return result


def from_ipc_tables(data: Mapping[str, bytes]) -> Dict[str, Any]:
    """Convert IPC bytes back to Python objects.
    
    Converts Arrow IPC bytes back to Polars DataFrames.
    
    Args:
        data: Dictionary mapping names to IPC bytes
        
    Returns:
        Dictionary of Polars DataFrames
        
    Raises:
        ValueError: If bytes cannot be decoded
    """
    result = {}
    for name, ipc_bytes in data.items():
        try:
            # Read Arrow IPC bytes
            reader = pa.ipc.open_stream(io.BytesIO(ipc_bytes))
            table = reader.read_all()
            
            # Convert to Polars DataFrame
            result[name] = pl.from_arrow(table)
        except Exception as e:
            raise ValueError(f"Failed to decode {name}: {e}")
    
    return result


def validate_sim_return(data: Any) -> Mapping[str, bytes]:
    """Validate and convert simulation return to contract-compliant format.
    
    Ensures the return value conforms to SimReturn = Mapping[str, TableIPC].
    
    Args:
        data: Raw return value from simulation function
        
    Returns:
        Validated Mapping[str, bytes]
        
    Raises:
        TypeError: If data is not a dict or cannot be converted
    """
    if not isinstance(data, dict):
        raise TypeError(f"Simulation must return dict, got {type(data).__name__}")
    
    # If already bytes, assume it's valid IPC
    if all(isinstance(v, bytes) for v in data.values()):
        return data
    
    # Otherwise convert to IPC
    return to_ipc_tables(data)
