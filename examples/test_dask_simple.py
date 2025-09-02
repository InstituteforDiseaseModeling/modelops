#!/usr/bin/env python
"""Simple test of Dask without custom modules.

This test runs pure Python functions that don't require modelops imports.
"""

from dask.distributed import Client
import time
import numpy as np


def simple_pi_monte_carlo(n_samples: int, seed: int) -> float:
    """Estimate pi using Monte Carlo - pure function, no imports needed."""
    import random
    random.seed(seed)
    
    inside = 0
    for _ in range(n_samples):
        x = random.uniform(-1, 1)
        y = random.uniform(-1, 1)
        if x*x + y*y <= 1:
            inside += 1
    
    return 4 * inside / n_samples


def main():
    # Connect to Dask
    scheduler_address = "tcp://localhost:8786"
    print(f"Connecting to Dask at {scheduler_address}")
    
    client = Client(scheduler_address)
    print(f"Connected! Cluster info:")
    print(f"  Workers: {len(client.scheduler_info()['workers'])}")
    print(f"  Dashboard: http://localhost:8787")
    
    # Submit simple tasks
    print("\nSubmitting Monte Carlo Pi estimations...")
    futures = []
    n_tasks = 10
    samples_per_task = 100000
    
    start = time.time()
    for i in range(n_tasks):
        future = client.submit(simple_pi_monte_carlo, samples_per_task, i)
        futures.append(future)
    
    print(f"Submitted {n_tasks} tasks, gathering results...")
    results = client.gather(futures)
    elapsed = time.time() - start
    
    print(f"\nCompleted in {elapsed:.2f} seconds")
    print(f"Rate: {n_tasks/elapsed:.1f} tasks/second")
    
    # Show results
    print("\nPi estimates:")
    for i, estimate in enumerate(results):
        error = abs(estimate - np.pi)
        print(f"  Task {i:2d}: π ≈ {estimate:.6f}, error = {error:.6f}")
    
    # Statistics
    mean_estimate = np.mean(results)
    std_estimate = np.std(results)
    print(f"\nStatistics:")
    print(f"  Mean estimate: {mean_estimate:.6f}")
    print(f"  Std deviation: {std_estimate:.6f}")
    print(f"  Actual π:      {np.pi:.6f}")
    
    # Test more complex computation
    print("\n" + "="*60)
    print("Testing matrix multiplication...")
    
    def matrix_multiply(size: int, seed: int):
        """Matrix multiplication test."""
        import numpy as np
        np.random.seed(seed)
        A = np.random.randn(size, size)
        B = np.random.randn(size, size)
        return np.dot(A, B).sum()
    
    futures = []
    for i in range(5):
        future = client.submit(matrix_multiply, 500, i)
        futures.append(future)
    
    start = time.time()
    results = client.gather(futures)
    elapsed = time.time() - start
    
    print(f"Completed 5 matrix multiplications (500x500) in {elapsed:.2f} seconds")
    print(f"Results: {[f'{r:.2f}' for r in results]}")
    
    client.close()
    print("\n✅ All tests completed successfully!")


if __name__ == "__main__":
    main()