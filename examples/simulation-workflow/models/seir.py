"""Minimal stochastic SEIR model for K8s job submission testing."""

import numpy as np
import polars as pl
from typing import Dict, Any, Mapping
from dataclasses import dataclass

from modelops_calabaria import (
    BaseModel, ParameterSpace, ParameterSpec, ParameterSet,
    model_output
)


@dataclass(frozen=True)
class SEIRConfig:
    """Fixed configuration for SEIR simulation.

    These are simulation settings that remain constant across all parameter
    samples during calibration. Only the disease dynamics parameters
    (beta, sigma, gamma) are varied by the sampling algorithm.
    """
    population: int = 50000
    initial_infected: int = 5
    simulation_days: int = 150
    dt: float = 0.1  # Time step for simulation


class StochasticSEIR(BaseModel):
    """Minimal stochastic SEIR model for testing job submission."""

    @classmethod
    def parameter_space(cls):
        """Define parameter space for calibration.

        Only disease dynamics parameters are included here.
        Fixed simulation settings are handled via SEIRConfig.
        """
        return ParameterSpace([
            ParameterSpec("beta", 0.1, 2.0, "float", doc="Transmission rate"),
            ParameterSpec("sigma", 0.05, 0.5, "float", doc="Incubation rate"),
            ParameterSpec("gamma", 0.05, 0.5, "float", doc="Recovery rate"),
        ])

    def __init__(self, space=None):
        """Initialize with parameter space.

        Args:
            space: Parameter space for calibration
        """
        if space is None:
            space = self.parameter_space()

        # Initialize with default config
        self.config = SEIRConfig()
        # Pass empty base_config
        super().__init__(space, base_config={})

    def build_sim(self, params: ParameterSet, config: Mapping[str, Any]) -> Dict:
        """Build simulation state from parameters and config.

        Uses fixed configuration from self.config for simulation settings,
        and params only for disease dynamics.
        """
        N = self.config.population
        I0 = self.config.initial_infected
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
                "days": self.config.simulation_days,
                "dt": config.get("dt", self.config.dt)  # Use passed config dt if available, else default
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