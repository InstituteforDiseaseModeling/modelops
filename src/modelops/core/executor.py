"""Core domain executor for simulation tasks.

This is the application service layer - the seam between primary
adapters (Dask) and secondary adapters (ExecutionEnvironment).
"""

from dataclasses import replace

from modelops_contracts import SimReturn, SimTask
from modelops_contracts.ports import ExecutionEnvironment

from modelops.telemetry import TelemetryCollector


class SimulationExecutor:
    """Application service layer for simulation execution.

    This is the seam between:
    - Inbound: Primary adapters (DaskSimulationService)
    - Outbound: Secondary adapters (ExecutionEnvironment)

    Even though thin now, this is where domain logic belongs:
    - Validation & normalization (coming soon)
    - Fingerprinting & caching (future)
    - Policy & routing decisions (future)
    - Observability & metrics (future)

    Infrastructure concerns stay in ExecutionEnvironment.
    The executor used to have bundle_repo and cas dependencies too,
    but those are infrastructure concerns that belong in ExecutionEnvironment.
    """

    def __init__(self, exec_env: ExecutionEnvironment):
        """Initialize with single dependency.

        Args:
            exec_env: Execution environment for running simulations
        """
        self.exec_env = exec_env
        self.telemetry = TelemetryCollector()

    def execute(self, task: SimTask) -> SimReturn:
        """Execute a simulation task.

        Currently just delegates to ExecutionEnvironment, but this
        is where we'll add domain logic:
        - Parameter validation
        - Result caching
        - Routing logic
        - Metrics collection

        Args:
            task: Simulation task to execute

        Returns:
            SimReturn with outputs and metadata (includes telemetry in metrics field)
        """
        # Future: self._validate(task)
        # Future: if cached := self._check_cache(task): return cached

        with self.telemetry.span(
            "simulation.execute",
            param_id=task.params.param_id[:8],
            seed=str(task.seed),
        ) as span:
            # Execute via environment
            result = self.exec_env.run(task)

            # Collect metrics
            span.metrics["cached"] = 1.0 if result.cached else 0.0

            # Attach telemetry to SimReturn.metrics field
            result = replace(
                result,
                metrics={
                    "execution_duration": span.duration(),
                    "cached": 1.0 if result.cached else 0.0,
                    **span.metrics,
                },
            )

            return result

    def shutdown(self):
        """Clean shutdown of executor.

        Ensures all resources are properly released.
        """
        if hasattr(self.exec_env, "shutdown"):
            self.exec_env.shutdown()
