"""Mock Calabaria wire implementation for testing.

This simulates what Calabaria would provide via its entry point.
In a real setup, this would be part of the Calabaria package.
"""

import json
import logging
import random
from typing import Dict, Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def wire_function(entrypoint: str, params: Dict[str, Any], seed: int) -> Dict[str, bytes]:
    """Execute a simulation and return serialized artifacts.
    
    This is the wire protocol implementation that ModelOps discovers
    via Python entry points. It simulates what Calabaria would do.
    
    Args:
        entrypoint: Entrypoint identifier (e.g., "model:scenario")
        params: Simulation parameters
        seed: Random seed for reproducibility
        
    Returns:
        Dictionary mapping artifact names to serialized bytes
    """
    logger.info(f"Wire function called: entrypoint={entrypoint}, seed={seed}, params={params}")
    
    # Set random seeds for reproducibility
    random.seed(seed)
    np.random.seed(seed)
    
    # Simulate different models based on entrypoint
    # The entrypoint comes in format "module.path/scenario"
    if entrypoint == "test_bundle.models/monte_carlo":
        result = run_monte_carlo_simulation(params, seed)
    elif entrypoint == "test_bundle.models/regression":
        result = run_regression_simulation(params, seed)
    else:
        # Default simulation
        result = run_default_simulation(params, seed)
    
    # Convert results to artifacts (serialized bytes)
    artifacts = {}
    
    # Main table artifact (required)
    table_df = pd.DataFrame(result)
    artifacts["table"] = table_df.to_parquet()
    
    # Optional metadata artifact
    metadata = {
        "entrypoint": entrypoint,
        "seed": seed,
        "params": params,
        "n_rows": len(table_df),
        "columns": list(table_df.columns)
    }
    artifacts["metadata"] = json.dumps(metadata, indent=2).encode()
    
    logger.info(f"Wire function returning {len(artifacts)} artifacts")
    return artifacts


def run_monte_carlo_simulation(params: Dict[str, Any], seed: int) -> Dict[str, Any]:
    """Run a Monte Carlo simulation.
    
    Args:
        params: Simulation parameters (alpha, beta, n_samples)
        seed: Random seed
        
    Returns:
        Simulation results as a dictionary
    """
    alpha = params.get("alpha", 1.0)
    beta = params.get("beta", 2.0)
    n_samples = params.get("n_samples", 1000)
    
    # Simulate some data
    samples = np.random.beta(alpha, beta, n_samples)
    
    # Calculate statistics
    results = {
        "sample_id": list(range(n_samples)),
        "value": samples.tolist(),
        "cumulative_mean": np.cumsum(samples) / np.arange(1, n_samples + 1)
    }
    
    return results


def run_regression_simulation(params: Dict[str, Any], seed: int) -> Dict[str, Any]:
    """Run a regression simulation.
    
    Args:
        params: Simulation parameters
        seed: Random seed
        
    Returns:
        Simulation results
    """
    n_points = params.get("n_points", 100)
    noise_level = params.get("noise", 0.1)
    
    # Generate synthetic data
    x = np.linspace(0, 10, n_points)
    true_y = 2 * x + 3
    noise = np.random.normal(0, noise_level, n_points)
    observed_y = true_y + noise
    
    # Simple linear regression
    coeffs = np.polyfit(x, observed_y, 1)
    fitted_y = np.polyval(coeffs, x)
    
    results = {
        "x": x.tolist(),
        "observed_y": observed_y.tolist(),
        "fitted_y": fitted_y.tolist(),
        "residuals": (observed_y - fitted_y).tolist(),
        "slope": [coeffs[0]] * n_points,
        "intercept": [coeffs[1]] * n_points
    }
    
    return results


def run_default_simulation(params: Dict[str, Any], seed: int) -> Dict[str, Any]:
    """Run a default simulation for unknown entrypoints.
    
    Args:
        params: Simulation parameters
        seed: Random seed
        
    Returns:
        Basic simulation results
    """
    n_rows = params.get("n_rows", 10)
    
    results = {
        "index": list(range(n_rows)),
        "random_values": np.random.randn(n_rows).tolist(),
        "param_echo": [str(params)] * n_rows,
        "seed_used": [seed] * n_rows
    }
    
    return results