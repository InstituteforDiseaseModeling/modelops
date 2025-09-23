#!/usr/bin/env python
"""Debug test for aggregation."""

from dask.distributed import Client
from modelops_contracts import SimTask, SimReturn, UniqueParameterSet, TableArtifact
from modelops_contracts.simulation import ReplicateSet, AggregationTask, AggregationReturn
import hashlib

def main():
    client = Client("tcp://localhost:8786")
    print(f"Connected to {client.scheduler_info()['address']}")
    
    # Test simple aggregation on worker
    def test_aggregation():
        """Test function that runs on worker."""
        # Create mock SimReturns with valid outputs
        sim_returns = [
            SimReturn(
                task_id="a" * 64,  # Valid 64-char hex
                outputs={
                    "result": TableArtifact(
                        ref=None,
                        checksum="e" * 64,
                        size=11,  # Match actual data size
                        inline=b"test data 1"
                    )
                }
            ),
            SimReturn(
                task_id="c" * 64,  # Valid 64-char hex
                outputs={
                    "result": TableArtifact(
                        ref=None,
                        checksum="f" * 64,
                        size=11,  # Match actual data size
                        inline=b"test data 2"
                    )
                }
            )
        ]
        
        # Create aggregation task
        # Note: entrypoint validation requires module.something format
        # We'll use dummy.targets to satisfy validation
        agg_task = AggregationTask(
            bundle_ref="local://examples/test_bundle",
            target_entrypoint="dummy.targets/compute_loss",  # Satisfies validation
            sim_returns=sim_returns
        )
        
        # Try to run aggregation
        from dask.distributed import get_worker
        worker = get_worker()
        
        if hasattr(worker, 'modelops_exec_env'):
            try:
                result = worker.modelops_exec_env.run_aggregation(agg_task)
                return f"Success! Loss: {result.loss}, N: {result.n_replicates}"
            except Exception as e:
                return f"Error in run_aggregation: {e}"
        else:
            return "No modelops_exec_env on worker"
    
    # Submit to worker
    future = client.submit(test_aggregation, pure=False)
    result = future.result()
    print(f"Result: {result}")
    
    client.close()

if __name__ == "__main__":
    main()