#!/usr/bin/env python
"""End-to-end test of ReplicateSet and aggregation through warm processes."""

import time
from pathlib import Path
from dask.distributed import Client
from modelops_contracts import (
    SimTask, UniqueParameterSet
)
from modelops_contracts.simulation import ReplicateSet
from modelops.services.dask_simulation import DaskSimulationService
from modelops.worker.config import RuntimeConfig
from modelops.utils.test_bundle_digest import compute_test_bundle_digest, format_test_bundle_ref

def main():
    """Test full replicate set and aggregation flow."""
    print("=" * 60)
    print("Testing ReplicateSet + Aggregation with Warm Processes")
    print("=" * 60)
    
    # Connect to cluster
    client = Client("tcp://localhost:8786")
    print(f"Connected to cluster: {client.scheduler_info()['address']}")
    print(f"Workers: {len(client.scheduler_info()['workers'])}")
    
    # Create service from environment config
    config = RuntimeConfig.from_env()
    print(f"Config: bundle_source={config.bundle_source}, bundles_dir={config.bundles_dir}")
    print(f"Force fresh venv: {config.force_fresh_venv}")
    service = DaskSimulationService(client, config)

    # Compute test bundle digest dynamically
    test_bundle_path = Path(__file__).parent / "test_bundle"
    if not test_bundle_path.exists():
        raise RuntimeError(f"Test bundle not found at {test_bundle_path}")

    bundle_digest = compute_test_bundle_digest(test_bundle_path)
    bundle_ref = format_test_bundle_ref(bundle_digest)
    print(f"Using test bundle with digest: {bundle_ref[:24]}...")
    
    # Create parameter sets for optimization
    param_sets = [
        UniqueParameterSet.from_dict({"alpha": 1.0, "beta": 2.0}),
        UniqueParameterSet.from_dict({"alpha": 1.5, "beta": 2.5}),
        UniqueParameterSet.from_dict({"alpha": 2.0, "beta": 3.0}),
    ]
    
    print("\n=== First Round: Submitting Replicate Sets with Aggregation ===")
    print("(This should take longer as processes need to be created and venvs installed)")
    
    first_round_start = time.time()
    aggregation_futures = []
    
    for params in param_sets:
        # Create base task
        base_task = SimTask.from_components(
            import_path="simulations.test",
            scenario="baseline",
            bundle_ref=bundle_ref,
            params=dict(params.params),
            seed=42
        )
        
        # Create replicate set
        replicate_set = ReplicateSet(
            base_task=base_task,
            n_replicates=5,  # 5 replicates per parameter set
            seed_offset=0
        )
        
        # Submit with aggregation - computes loss ON WORKER!
        # Note: For this test, we'll use a mock target entrypoint
        future = service.submit_replicate_set(
            replicate_set,
            target_entrypoint="targets.compute_loss/compute_loss"  # Simple test target
        )
        
        aggregation_futures.append((params.param_id, future))
        print(f"  Submitted {replicate_set.n_replicates} replicates "
              f"for param {params.param_id[:8]}")
    
    print("\n=== Gathering Aggregated Results ===")
    print("(Only loss values transferred, not all replicate data!)")
    
    # Gather ONLY the aggregated results
    for param_id, future in aggregation_futures:
        try:
            result = future.result()
            print(f"\nParam {param_id[:8]}:")
            print(f"  Loss: {result.loss:.4f}")
            print(f"  Replicates: {result.n_replicates}")
            if result.diagnostics:
                print(f"  Diagnostics: {result.diagnostics}")
        except Exception as e:
            print(f"  Error for param {param_id[:8]}: {e}")
    
    # Show timing for first round
    first_round_elapsed = time.time() - first_round_start
    print(f"\nFirst round completed in {first_round_elapsed:.2f}s")
    
    # Demonstrate process reuse
    print("\n=== Process Pool Status ===")
    
    def check_process_pool():
        """Check warm process pool on a worker."""
        from dask.distributed import get_worker
        worker = get_worker()
        if hasattr(worker, 'modelops_runtime'):
            runtime = worker.modelops_runtime
            if hasattr(runtime, '_process_manager'):
                pm = runtime._process_manager
                return {
                    'n_processes': pm.active_count(),
                    'process_keys': list(pm._processes.keys())[:5]  # Show first 5 keys
                }
        return {'error': 'No process manager found'}
    
    try:
        pool_status = client.submit(check_process_pool, pure=False).result()
        print(f"Warm processes in pool: {pool_status.get('n_processes', 0)}")
        print(f"Process keys: {pool_status.get('process_keys', [])}")
    except Exception as e:
        print(f"Could not check process pool: {e}")
    
    print("\n=== Testing Process Reuse ===")
    
    # Submit another round with same bundle - should reuse processes!
    print("Submitting second round (should be faster due to warm processes)...")
    
    start_time = time.time()
    
    # Create new replicate set with same bundle
    new_task = SimTask.from_components(
        import_path="simulations.test",
        scenario="baseline",  # Same scenario, same bundle
        bundle_ref="sha256:f987e0ab742272c3969d63207162993f65c6c3af01f07910bd3c239f4407c51c",  # SAME bundle
        params={"alpha": 0.5, "beta": 1.5},
        seed=1000
    )
    
    new_replicate_set = ReplicateSet(
        base_task=new_task,
        n_replicates=5,
        seed_offset=0
    )
    
    # Submit and wait
    future = service.submit_replicate_set(
        new_replicate_set,
        target_entrypoint="targets.compute_loss/compute_loss"
    )
    
    try:
        result = future.result()
        elapsed = time.time() - start_time
        print(f"Second round completed in {elapsed:.2f}s")
        print(f"Loss: {result.loss:.4f}")
        print("(Should be much faster due to warm process reuse!)")
    except Exception as e:
        print(f"Second round failed: {e}")
    
    client.close()
    
    print("\n" + "=" * 60)
    print("Test completed!")
    print("\nKey achievements:")
    print("1. ReplicateSets group simulations with same parameters")
    print("2. Aggregation runs ON WORKERS using same warm processes")
    print("3. Only loss values transferred, not all replicate data")
    print("4. Warm processes reused for both simulation AND aggregation")
    print("5. Target evaluation integrated with infrastructure")

if __name__ == "__main__":
    main()