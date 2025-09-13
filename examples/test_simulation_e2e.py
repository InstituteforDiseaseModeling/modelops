#!/usr/bin/env python
"""End-to-end test of the new simulation architecture.

This example demonstrates:
1. Starting a local Dask cluster
2. Registering the ModelOps WorkerPlugin
3. Creating simulation tasks with bundle references
4. Submitting tasks via DaskSimulationService
5. Gathering and verifying results
"""

import logging
import os
from pathlib import Path
from typing import List

from dask.distributed import Client
from modelops_contracts import SimTask, UniqueParameterSet, EntryPointId, make_param_id

from modelops.services.dask_simulation import DaskSimulationService
from modelops.worker.config import RuntimeConfig

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_test_tasks(bundle_ref: str, n_tasks: int = 5) -> List[SimTask]:
    """Create test simulation tasks.
    
    Args:
        bundle_ref: Reference to the simulation bundle
        n_tasks: Number of tasks to create
        
    Returns:
        List of simulation tasks
    """
    tasks = []
    
    for i in range(n_tasks):
        # Create parameters
        param_dict = {
            "alpha": 0.1 * (i + 1),
            "beta": 2.0,
            "n_samples": 100  # Reduced to avoid large messages
        }
        params = UniqueParameterSet(
            params=param_dict,
            param_id=make_param_id(param_dict)
        )
        
        # Create task
        task = SimTask(
            entrypoint=EntryPointId("test_bundle.models/monte_carlo"),
            params=params,
            seed=42 + i,
            bundle_ref=bundle_ref
        )
        
        tasks.append(task)
    
    return tasks


def main():
    """Run the end-to-end test."""
    
    # Configuration
    scheduler_address = "tcp://localhost:8786"
    
    # For testing, use file bundle repository
    bundle_ref = "file:///Users/vsb/projects/work/modelops/examples/test_bundle"
    
    # Set up runtime configuration
    # os.environ["MODELOPS_EXECUTOR_TYPE"] = "direct"  # Use direct execution for testing
    os.environ["MODELOPS_EXECUTOR_TYPE"] = "isolated_warm"
    os.environ["MODELOPS_BUNDLE_SOURCE"] = "file"
    os.environ["MODELOPS_BUNDLES_DIR"] = "/Users/vsb/projects/work/modelops/examples"
    os.environ["MODELOPS_CAS_BACKEND"] = "memory"
    
    print("=" * 60)
    print("ModelOps Simulation End-to-End Test")
    print("=" * 60)
    
    # Connect to Dask cluster
    print(f"\n1. Connecting to Dask cluster at {scheduler_address}")
    client = Client(scheduler_address)
    print(f"   Connected! Workers: {len(client.scheduler_info()['workers'])}")
    print(f"   Dashboard: http://localhost:8787")
    
    # Create simulation service (this installs the WorkerPlugin)
    print("\n2. Creating DaskSimulationService and installing WorkerPlugin")
    config = RuntimeConfig.from_env()
    service = DaskSimulationService(client, config)
    print("   Service ready!")
    
    # Create test tasks
    print(f"\n3. Creating test tasks with bundle: {bundle_ref}")
    tasks = create_test_tasks(bundle_ref, n_tasks=5)
    print(f"   Created {len(tasks)} tasks")
    
    # Submit tasks
    print("\n4. Submitting tasks to cluster")
    futures = []
    for task in tasks:
        future = service.submit(task)
        futures.append(future)
        print(f"   Submitted task with seed={task.seed}, params={dict(task.params.params)}")
    
    # Gather results
    print("\n5. Gathering results")
    results = service.gather(futures)
    
    # Display results
    print("\n6. Results:")
    for i, (task, result) in enumerate(zip(tasks, results)):
        print(f"\n   Task {i}:")
        print(f"     Seed: {task.seed}")
        print(f"     Params: {dict(task.params.params)}")
        print(f"     Task ID: {result.task_id[:16]}...")
        
        # Check for errors using the new error field
        if result.error:
            print(f"     Status: FAILED")
            print(f"     Error: {result.error.message}")
            print(f"     Error Type: {result.error.error_type}")
            print(f"     Retryable: {result.error.retryable}")
            
            # Optionally decode full error details if needed
            if result.error_details and result.error_details.inline:
                import json
                try:
                    details = json.loads(result.error_details.inline)
                    if 'entrypoint' in details:
                        print(f"     Entrypoint: {details['entrypoint']}")
                except Exception:
                    pass  # Ignore decode errors for details
        else:
            print(f"     Status: SUCCESS")
            print(f"     Outputs: {list(result.outputs.keys())}")
    
    # Check success
    successful = sum(1 for r in results if not r.error and "table" in r.outputs)
    print(f"\n7. Summary: {successful}/{len(results)} tasks completed successfully")
    
    # Clean up
    client.close()
    
    # Show appropriate status emoji
    if successful == len(results):
        print("\n✅ Test complete!")
    else:
        print("\n❌ Test failed!")
    
    return successful == len(results)


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
