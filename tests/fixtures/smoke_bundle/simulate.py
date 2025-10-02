"""Simple simulation function for OCI bundle smoke testing.

This is a minimal simulation that validates the complete flow:
1. Bundle push to registry
2. Worker fetches bundle from registry
3. Execution environment runs the simulation
4. Results returned through Dask
"""

def simulate(params, seed):
    """Minimal simulation for smoke testing OCI bundle integration.

    Args:
        params: Dictionary of simulation parameters
        seed: Random seed for reproducibility

    Returns:
        Dictionary with simulation results
    """
    return {
        "status": "completed",
        "message": "OCI bundle successfully fetched and executed!",
        "params": params,
        "seed": seed,
        "test": True,
        "bundle_integration": "working",
        # Include some computation to verify execution
        "computed_value": params.get("value", 1.0) * seed,
        "model_type": params.get("model", "smoke_test")
    }