"""
Wire function for smoke test bundle.

This provides the wire protocol implementation that bridges between
ModelOps infrastructure and the simulation function.
"""

import json
import sys
from pathlib import Path
from typing import Dict, Any


def wire(entrypoint: str, params: Dict[str, Any], seed: int) -> Dict[str, bytes]:
    """Wire function that executes the simulation and returns serialized results.

    Args:
        entrypoint: The entry point to execute (e.g., "simulate:simulate")
        params: Dictionary of simulation parameters
        seed: Random seed for reproducibility

    Returns:
        Dictionary mapping artifact names to serialized bytes
    """
    # Ensure bundle directory is in Python path
    bundle_dir = Path(__file__).parent
    if str(bundle_dir) not in sys.path:
        sys.path.insert(0, str(bundle_dir))

    # Import the simulate function
    from simulate import simulate

    # Run the simulation
    result = simulate(params, seed)

    # Serialize the result as JSON bytes
    # In a real Calabaria model, this would be Arrow IPC format
    result_bytes = json.dumps(result).encode("utf-8")

    # Return as a dictionary of named artifacts
    return {
        "result": result_bytes,
        "metadata": json.dumps(
            {"entrypoint": entrypoint, "status": "completed", "model_type": "smoke_test"}
        ).encode("utf-8"),
    }
