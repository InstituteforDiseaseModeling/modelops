"""Test module for bundle testing."""

import numpy as np


def bundle_simulation(params: dict, seed: int) -> dict:
    """Test simulation that runs in bundle environment.
    
    Args:
        params: Simulation parameters
        seed: Random seed
        
    Returns:
        Dict with simulation results as bytes
    """
    np.random.seed(seed)
    
    # Get parameters
    size = params.get("size", 10)
    mean = params.get("mean", 0.0)
    std = params.get("std", 1.0)
    
    # Generate data
    data = np.random.normal(mean, std, size)
    
    # Create results
    results = {
        "data": data.tobytes(),
        "stats": np.array([data.mean(), data.std()]).tobytes(),
        "info": f"Generated {size} samples with seed {seed}".encode(),
    }
    
    return results


def simple_test(params: dict, seed: int) -> dict:
    """Simple test without numpy dependency."""
    return {
        "output": f"params={params},seed={seed}".encode(),
        "status": b"OK",
    }