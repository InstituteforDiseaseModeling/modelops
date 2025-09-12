"""Simulation models for the test bundle.

In a real Calabaria bundle, these would be the user's scientific models.
"""

import numpy as np
from typing import Dict, Any


class MonteCarloModel:
    """Example Monte Carlo simulation model."""
    
    def __init__(self, alpha: float = 1.0, beta: float = 2.0):
        self.alpha = alpha
        self.beta = beta
    
    def simulate(self, n_samples: int, seed: int) -> np.ndarray:
        """Run the simulation.
        
        Args:
            n_samples: Number of samples to generate
            seed: Random seed
            
        Returns:
            Array of simulated values
        """
        np.random.seed(seed)
        return np.random.beta(self.alpha, self.beta, n_samples)


class RegressionModel:
    """Example regression model."""
    
    def __init__(self, true_slope: float = 2.0, true_intercept: float = 3.0):
        self.true_slope = true_slope
        self.true_intercept = true_intercept
    
    def generate_data(self, n_points: int, noise_level: float, seed: int) -> Dict[str, np.ndarray]:
        """Generate synthetic regression data.
        
        Args:
            n_points: Number of data points
            noise_level: Standard deviation of noise
            seed: Random seed
            
        Returns:
            Dictionary with x, y_true, and y_observed
        """
        np.random.seed(seed)
        
        x = np.linspace(0, 10, n_points)
        y_true = self.true_slope * x + self.true_intercept
        noise = np.random.normal(0, noise_level, n_points)
        y_observed = y_true + noise
        
        return {
            "x": x,
            "y_true": y_true,
            "y_observed": y_observed,
            "noise": noise
        }
    
    def fit(self, x: np.ndarray, y: np.ndarray) -> Dict[str, float]:
        """Fit a linear model to the data.
        
        Args:
            x: Input values
            y: Output values
            
        Returns:
            Fitted coefficients
        """
        coeffs = np.polyfit(x, y, 1)
        return {
            "slope": coeffs[0],
            "intercept": coeffs[1]
        }