"""Minimal stochastic SEIR model for K8s job submission testing."""

import numpy as np
import polars as pl
from typing import Dict, Any, Mapping

from modelops_calabaria import (
    BaseModel, ParameterSpace, ParameterSpec, ParameterSet,
    model_output
)


class StochasticSEIR(BaseModel):
    """Minimal stochastic SEIR model for testing job submission."""

    @classmethod
    def parameter_space(cls):
        """Define parameter space for Sobol sampling."""
        return ParameterSpace([
            ParameterSpec("beta", 0.1, 2.0, "float", doc="Transmission rate"),
            ParameterSpec("sigma", 0.05, 0.5, "float", doc="Incubation rate"),
            ParameterSpec("gamma", 0.05, 0.5, "float", doc="Recovery rate"),
            ParameterSpec("population", 10000, 100000, "int", doc="Population size"),
            ParameterSpec("initial_infected", 1, 10, "int", doc="Initial infected"),
            ParameterSpec("simulation_days", 100, 200, "int", doc="Days to simulate"),
        ])

    def __init__(self, space=None):
        """Initialize with parameter space."""
        if space is None:
            space = self.parameter_space()
        super().__init__(space, base_config={"dt": 0.1})

    def build_sim(self, params: ParameterSet, config: Mapping[str, Any]) -> Dict:
        """Build simulation state from parameters."""
        N = int(params["population"])
        I0 = int(params["initial_infected"])
        E0 = 0  # Start with no exposed
        S0 = N - I0
        R0 = 0

        return {
            "state": {"S": S0, "E": E0, "I": I0, "R": R0},
            "params": {
                "beta": float(params["beta"]),
                "sigma": float(params["sigma"]),
                "gamma": float(params["gamma"]),
                "N": N,
                "days": int(params["simulation_days"]),
                "dt": config.get("dt", 0.1)
            }
        }

    def run_sim(self, state: Dict, seed: int) -> Dict:
        """Run stochastic SEIR simulation."""
        rng = np.random.RandomState(seed)

        # Extract state and parameters
        S, E, I, R = state["state"]["S"], state["state"]["E"], state["state"]["I"], state["state"]["R"]
        p = state["params"]
        N, beta, sigma, gamma = p["N"], p["beta"], p["sigma"], p["gamma"]
        dt, days = p["dt"], p["days"]

        # Time arrays
        times = np.arange(0, days + dt, dt)

        # Storage
        result_times = []
        result_I = []

        # Simulation loop
        for t in times:
            if int(t) == t:  # Record daily
                result_times.append(int(t))  # Store as int for day column
                result_I.append(I)

            # Rates
            infection_rate = beta * S * I / N
            progression_rate = sigma * E
            recovery_rate = gamma * I

            # Stochastic transitions
            new_infections = rng.poisson(infection_rate * dt)
            new_progressions = rng.poisson(progression_rate * dt)
            new_recoveries = rng.poisson(recovery_rate * dt)

            # Bounds checking
            new_infections = min(new_infections, S)
            new_progressions = min(new_progressions, E)
            new_recoveries = min(new_recoveries, I)

            # Update
            S -= new_infections
            E += new_infections - new_progressions
            I += new_progressions - new_recoveries
            R += new_recoveries

        return {
            "times": result_times,
            "infected": result_I,
            "peak": max(result_I),
            "final_size": R
        }

    @model_output("prevalence")
    def extract_prevalence(self, raw: Dict, seed: int) -> pl.DataFrame:
        """Extract infection prevalence time series."""
        return pl.DataFrame({
            "day": raw["times"],
            "infected": raw["infected"]
        })

    @model_output("summary")
    def extract_summary(self, raw: Dict, seed: int) -> pl.DataFrame:
        """Extract summary statistics."""
        return pl.DataFrame({
            "metric": ["peak_infections", "final_size"],
            "value": [float(raw["peak"]), float(raw["final_size"])]
        })