"""Example simulation functions for testing ModelOps with Dask.

These simulations follow the ModelOps contract:
- Take params (dict) and seed (int) as arguments
- Return a dict of named tables/arrays
"""


def simple_sim(params: dict, seed: int) -> dict:
    """Simple simulation for testing basic functionality.
    
    Args:
        params: Dictionary with 'x' (scaling factor) and optionally 'n' (number of samples)
        seed: Random seed for reproducibility
        
    Returns:
        Dict with 'output' array and 'metrics' dict
    """
    import numpy as np
    np.random.seed(seed)
    
    # Get parameters with defaults
    x = params.get("x", 1.0)
    n = params.get("n", 100)
    
    # Simulate some computation
    result = x * np.random.randn(n)
    
    return {
        "output": result.tolist(),
        "metrics": {
            "mean": [float(result.mean())],
            "std": [float(result.std())],
            "min": [float(result.min())],
            "max": [float(result.max())]
        }
    }


def monte_carlo_pi(params: dict, seed: int) -> dict:
    """Estimate pi using Monte Carlo simulation.
    
    Args:
        params: Dictionary with 'n_samples' (number of random points)
        seed: Random seed for reproducibility
        
    Returns:
        Dict with pi estimate and convergence metrics
    """
    import numpy as np
    np.random.seed(seed)
    
    n_samples = params.get("n_samples", 10000)
    
    # Generate random points in [-1, 1] x [-1, 1]
    points = np.random.uniform(-1, 1, (n_samples, 2))
    
    # Count points inside unit circle
    distances = np.sqrt(points[:, 0]**2 + points[:, 1]**2)
    inside_circle = np.sum(distances <= 1)
    
    # Estimate pi: Area of circle / Area of square = pi/4
    pi_estimate = 4 * inside_circle / n_samples
    
    # Calculate error
    error = abs(pi_estimate - np.pi)
    
    return {
        "estimate": [float(pi_estimate)],
        "error": [float(error)],
        "n_samples": [int(n_samples)],
        "seed": [int(seed)],
        "inside_ratio": [float(inside_circle / n_samples)]
    }


def black_scholes_option(params: dict, seed: int) -> dict:
    """Monte Carlo simulation for Black-Scholes option pricing.
    
    Args:
        params: Dictionary with:
            - S0: Initial stock price (default 100)
            - K: Strike price (default 100)
            - T: Time to maturity in years (default 1.0)
            - r: Risk-free rate (default 0.05)
            - sigma: Volatility (default 0.2)
            - n_paths: Number of simulation paths (default 10000)
            - option_type: 'call' or 'put' (default 'call')
        seed: Random seed for reproducibility
        
    Returns:
        Dict with option price and Greeks
    """
    import numpy as np
    np.random.seed(seed)
    
    # Extract parameters
    S0 = params.get("S0", 100.0)
    K = params.get("K", 100.0)
    T = params.get("T", 1.0)
    r = params.get("r", 0.05)
    sigma = params.get("sigma", 0.2)
    n_paths = params.get("n_paths", 10000)
    option_type = params.get("option_type", "call")
    
    # Generate random paths
    dt = T
    Z = np.random.standard_normal(n_paths)
    
    # Calculate terminal stock prices using GBM
    ST = S0 * np.exp((r - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * Z)
    
    # Calculate payoffs
    if option_type == "call":
        payoffs = np.maximum(ST - K, 0)
    else:  # put
        payoffs = np.maximum(K - ST, 0)
    
    # Discount to present value
    option_price = np.exp(-r * T) * np.mean(payoffs)
    
    # Calculate standard error
    std_error = np.std(payoffs) / np.sqrt(n_paths)
    
    # Confidence interval (95%)
    ci_lower = option_price - 1.96 * std_error
    ci_upper = option_price + 1.96 * std_error
    
    return {
        "price": [float(option_price)],
        "std_error": [float(std_error)],
        "ci_lower": [float(ci_lower)],
        "ci_upper": [float(ci_upper)],
        "parameters": {
            "S0": [float(S0)],
            "K": [float(K)],
            "T": [float(T)],
            "r": [float(r)],
            "sigma": [float(sigma)],
            "n_paths": [int(n_paths)]
        }
    }


def stochastic_growth_model(params: dict, seed: int) -> dict:
    """Simulate a stochastic growth model with random shocks.
    
    Args:
        params: Dictionary with:
            - initial_value: Starting value (default 100)
            - growth_rate: Mean growth rate (default 0.05)
            - volatility: Standard deviation of growth (default 0.15)
            - n_periods: Number of time periods (default 252, like trading days)
            - dt: Time step size (default 1/252)
        seed: Random seed for reproducibility
        
    Returns:
        Dict with time series and statistics
    """
    import numpy as np
    np.random.seed(seed)
    
    # Extract parameters
    initial_value = params.get("initial_value", 100.0)
    growth_rate = params.get("growth_rate", 0.05)
    volatility = params.get("volatility", 0.15)
    n_periods = params.get("n_periods", 252)
    dt = params.get("dt", 1/252)
    
    # Generate time series using geometric Brownian motion
    values = np.zeros(n_periods + 1)
    values[0] = initial_value
    
    # Random shocks
    shocks = np.random.standard_normal(n_periods)
    
    for t in range(n_periods):
        values[t + 1] = values[t] * np.exp(
            (growth_rate - 0.5 * volatility**2) * dt + 
            volatility * np.sqrt(dt) * shocks[t]
        )
    
    # Calculate returns
    returns = np.diff(np.log(values))
    
    # Calculate statistics
    final_value = values[-1]
    total_return = (final_value - initial_value) / initial_value
    annualized_return = (final_value / initial_value) ** (1 / (n_periods * dt)) - 1
    max_drawdown = np.min(values / np.maximum.accumulate(values)) - 1
    
    return {
        "time_series": values.tolist(),
        "returns": returns.tolist(),
        "statistics": {
            "final_value": [float(final_value)],
            "total_return": [float(total_return)],
            "annualized_return": [float(annualized_return)],
            "max_drawdown": [float(max_drawdown)],
            "volatility_realized": [float(np.std(returns) * np.sqrt(1/dt))]
        }
    }