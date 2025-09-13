"""Simple target evaluation functions for testing."""

def compute_loss(sim_returns, target_data=None):
    """Simple loss computation for testing aggregation.
    
    Args:
        sim_returns: List of simulation results (as dicts)
        target_data: Optional empirical data
        
    Returns:
        Dict with loss and diagnostics
    """
    # Simple mock loss calculation
    n_returns = len(sim_returns) if sim_returns else 0
    loss = 1.0 / (1.0 + n_returns)
    
    return {
        "loss": loss,
        "diagnostics": {
            "n_replicates": n_returns,
            "method": "simple_test"
        }
    }