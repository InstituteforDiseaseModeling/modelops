#!/usr/bin/env python
"""Test running simulations on Dask cluster.

This script demonstrates how to:
1. Connect to a Dask scheduler
2. Submit multiple simulations in parallel
3. Gather and process results

Usage:
    # First, port-forward the Dask scheduler:
    kubectl port-forward -n modelops-default svc/dask-scheduler 8786:8786
    
    # Then run this script:
    python examples/run_dask_simulation.py
"""

import sys
import time
import argparse
from typing import List
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from modelops.services.simulation import DaskSimulationService, LocalSimulationService
from modelops.services.ipc import from_ipc_tables


def test_monte_carlo_pi(service, n_simulations: int = 10):
    """Test Monte Carlo pi estimation."""
    print(f"\n{'='*60}")
    print(f"Testing Monte Carlo Pi Estimation with {n_simulations} simulations")
    print(f"{'='*60}")
    
    # Submit simulations with different seeds
    futures = []
    for i in range(n_simulations):
        params = {"n_samples": 100000}  # 100k samples per simulation
        future = service.submit(
            "examples.simulations:monte_carlo_pi",
            params,
            seed=i,
            bundle_ref=""
        )
        futures.append(future)
    
    print(f"Submitted {n_simulations} simulations...")
    
    # Gather results
    start = time.time()
    results = service.gather(futures)
    elapsed = time.time() - start
    
    print(f"Completed in {elapsed:.4f} seconds")
    if elapsed > 0:
        print(f"Rate: {n_simulations/elapsed:.1f} simulations/second")
    else:
        print(f"Rate: >10000 simulations/second (too fast to measure)")
    
    # Process and display results
    estimates = []
    errors = []
    
    for i, result in enumerate(results):
        decoded = from_ipc_tables(result)
        # Handle different data formats (list, pandas DataFrame, polars DataFrame)
        est = decoded["estimate"]
        err = decoded["error"]
        
        if hasattr(est, 'item'):  # Polars DataFrame
            pi_estimate = float(est.item())
            error = float(err.item())
        elif hasattr(est, 'iloc'):  # Pandas DataFrame
            pi_estimate = float(est.iloc[0])
            error = float(err.iloc[0])
        else:  # List
            pi_estimate = float(est[0])
            error = float(err[0])
        estimates.append(pi_estimate)
        errors.append(error)
        
        if i < 5:  # Show first 5 results
            print(f"  Sim {i:2d}: π ≈ {pi_estimate:.6f}, error = {error:.6f}")
    
    if n_simulations > 5:
        print(f"  ... ({n_simulations - 5} more results)")
    
    # Statistics across all simulations
    import numpy as np
    mean_estimate = np.mean(estimates)
    std_estimate = np.std(estimates)
    mean_error = np.mean(errors)
    
    print(f"\nAggregate Statistics:")
    print(f"  Mean π estimate: {mean_estimate:.6f}")
    print(f"  Std deviation:   {std_estimate:.6f}")
    print(f"  Mean error:      {mean_error:.6f}")
    print(f"  Actual π:        {np.pi:.6f}")


def test_option_pricing(service, n_simulations: int = 20):
    """Test Black-Scholes option pricing."""
    print(f"\n{'='*60}")
    print(f"Testing Black-Scholes Option Pricing with {n_simulations} simulations")
    print(f"{'='*60}")
    
    # Submit simulations with different volatilities
    futures = []
    volatilities = []
    
    for i in range(n_simulations):
        # Vary volatility from 10% to 50%
        sigma = 0.1 + (0.4 * i / (n_simulations - 1))
        volatilities.append(sigma)
        
        params = {
            "S0": 100,      # Current price
            "K": 100,       # Strike (at-the-money)
            "T": 1.0,       # 1 year to expiry
            "r": 0.05,      # 5% risk-free rate
            "sigma": sigma, # Volatility
            "n_paths": 50000,
            "option_type": "call"
        }
        
        future = service.submit(
            "examples.simulations:black_scholes_option",
            params,
            seed=42,  # Same seed for comparison
            bundle_ref=""
        )
        futures.append(future)
    
    print(f"Submitted {n_simulations} option pricing simulations...")
    
    # Gather results
    start = time.time()
    results = service.gather(futures)
    elapsed = time.time() - start
    
    print(f"Completed in {elapsed:.2f} seconds")
    
    # Display volatility smile
    print("\nVolatility vs Option Price:")
    print(f"{'Volatility':>10} | {'Price':>10} | {'Std Error':>10}")
    print("-" * 35)
    
    prices = []
    for i, result in enumerate(results):
        decoded = from_ipc_tables(result)
        # Handle different data formats
        p = decoded["price"]
        se = decoded["std_error"]
        
        if hasattr(p, 'item'):  # Polars
            price = float(p.item())
            std_error = float(se.item())
        elif hasattr(p, 'iloc'):  # Pandas
            price = float(p.iloc[0])
            std_error = float(se.iloc[0])
        else:  # List
            price = float(p[0])
            std_error = float(se[0])
        prices.append(price)
        
        if i % (n_simulations // 5) == 0:  # Show every 5th result
            print(f"{volatilities[i]:10.1%} | ${price:9.2f} | ±${std_error:8.4f}")


def test_stochastic_growth(service, n_simulations: int = 5):
    """Test stochastic growth model."""
    print(f"\n{'='*60}")
    print(f"Testing Stochastic Growth Model with {n_simulations} paths")
    print(f"{'='*60}")
    
    # Submit simulations
    futures = []
    for i in range(n_simulations):
        params = {
            "initial_value": 100,
            "growth_rate": 0.08,    # 8% annual growth
            "volatility": 0.20,      # 20% annual volatility
            "n_periods": 252,        # 1 year of daily data
            "dt": 1/252
        }
        
        future = service.submit(
            "examples.simulations:stochastic_growth_model",
            params,
            seed=i,
            bundle_ref=""
        )
        futures.append(future)
    
    print(f"Submitted {n_simulations} growth simulations...")
    
    # Gather results
    start = time.time()
    results = service.gather(futures)
    elapsed = time.time() - start
    
    print(f"Completed in {elapsed:.2f} seconds")
    
    # Display path statistics
    print("\nGrowth Path Statistics:")
    print(f"{'Path':>6} | {'Final Value':>12} | {'Total Return':>12} | {'Max Drawdown':>12}")
    print("-" * 55)
    
    for i, result in enumerate(results):
        decoded = from_ipc_tables(result)
        stats = decoded["statistics"]
        
        # Handle different data formats
        fv = stats["final_value"]
        tr = stats["total_return"]
        md = stats["max_drawdown"]
        
        if hasattr(fv, 'item'):  # Polars
            final_value = float(fv.item())
            total_return = float(tr.item())
            max_drawdown = float(md.item())
        elif hasattr(fv, 'iloc'):  # Pandas
            final_value = float(fv.iloc[0])
            total_return = float(tr.iloc[0])
            max_drawdown = float(md.iloc[0])
        else:  # List
            final_value = float(fv[0])
            total_return = float(tr[0])
            max_drawdown = float(md[0])
        
        print(f"{i:6d} | ${final_value:11.2f} | {total_return:11.1%} | {max_drawdown:11.1%}")


def main():
    parser = argparse.ArgumentParser(description="Run simulations on Dask")
    parser.add_argument(
        "--scheduler",
        default="tcp://localhost:8786",
        help="Dask scheduler address (default: tcp://localhost:8786)"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Use local simulation service instead of Dask"
    )
    parser.add_argument(
        "--test",
        choices=["all", "pi", "option", "growth"],
        default="all",
        help="Which test to run"
    )
    parser.add_argument(
        "-n",
        "--num-sims",
        type=int,
        default=10,
        help="Number of simulations to run"
    )
    
    args = parser.parse_args()
    
    # Create simulation service
    if args.local:
        print(f"Using LocalSimulationService (in-process execution)")
        service = LocalSimulationService()
    else:
        print(f"Connecting to Dask scheduler at {args.scheduler}")
        print("(Make sure to run: kubectl port-forward -n modelops-default svc/dask-scheduler 8786:8786)")
        
        try:
            service = DaskSimulationService(args.scheduler)
            # Test connection by getting client info
            print(f"Connected! Cluster has {len(service.client.scheduler_info()['workers'])} workers")
        except Exception as e:
            print(f"\nError connecting to Dask: {e}")
            print("\nTips:")
            print("1. Check if Dask is running: kubectl get pods -n modelops-default")
            print("2. Port-forward the scheduler: kubectl port-forward -n modelops-default svc/dask-scheduler 8786:8786")
            print("3. Or use --local flag to test with local execution")
            return 1
    
    try:
        # Run tests
        if args.test in ["all", "pi"]:
            test_monte_carlo_pi(service, args.num_sims)
        
        if args.test in ["all", "option"]:
            test_option_pricing(service, min(args.num_sims * 2, 20))
        
        if args.test in ["all", "growth"]:
            test_stochastic_growth(service, min(args.num_sims // 2, 5))
        
        print(f"\n{'='*60}")
        print("All tests completed successfully!")
        print(f"{'='*60}")
        
    finally:
        if hasattr(service, 'close'):
            service.close()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
