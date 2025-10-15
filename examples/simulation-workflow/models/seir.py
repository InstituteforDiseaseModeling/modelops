"""Minimal stochastic SEIR model for K8s job submission testing."""

import numpy as np
import polars as pl
from typing import Dict, Any, Mapping

from modelops_calabaria import (
    BaseModel, ParameterSpace, ParameterSpec, ParameterSet,
    model_output
)


# --- Core SEIR simulation functions (pure, no framework dependency) ---

def seir_dynamics(state: Dict, params: Dict, dt: float, rng: np.random.RandomState) -> Dict:
    """
    Single timestep of SEIR dynamics.

    Pure function that computes one timestep of stochastic SEIR model.

    Args:
        state: Current compartment sizes {'S': int, 'E': int, 'I': int, 'R': int}
        params: Model parameters {'beta': float, 'sigma': float, 'gamma': float, 'N': int}
        dt: Time step size
        rng: Random number generator for stochastic transitions

    Returns:
        New state after one timestep
    """
    S, E, I, R = state['S'], state['E'], state['I'], state['R']
    N = params['N']

    # Calculate rates
    infection_rate = params['beta'] * S * I / N
    progression_rate = params['sigma'] * E
    recovery_rate = params['gamma'] * I

    # Stochastic transitions using Poisson processes
    new_infections = rng.poisson(infection_rate * dt)
    new_progressions = rng.poisson(progression_rate * dt)
    new_recoveries = rng.poisson(recovery_rate * dt)

    # Ensure transitions don't exceed compartment sizes
    new_infections = min(new_infections, S)
    new_progressions = min(new_progressions, E)
    new_recoveries = min(new_recoveries, I)

    # Return updated state
    return {
        'S': S - new_infections,
        'E': E + new_infections - new_progressions,
        'I': I + new_progressions - new_recoveries,
        'R': R + new_recoveries
    }


def run_seir_simulation(initial_state: Dict, params: Dict, seed: int) -> Dict:
    """
    Run complete SEIR simulation.

    Pure function that runs a full stochastic SEIR simulation from initial
    conditions to completion.

    Args:
        initial_state: Initial compartment sizes {'S': int, 'E': int, 'I': int, 'R': int}
        params: Model parameters including:
            - 'beta': transmission rate
            - 'sigma': incubation rate
            - 'gamma': recovery rate
            - 'N': total population
            - 'days': simulation duration
            - 'dt': timestep size
        seed: Random seed for reproducibility

    Returns:
        Dictionary with simulation results:
            - 'times': list of time points (days)
            - 'infected': list of infected counts at each day
            - 'peak': maximum infected count
            - 'final_size': final recovered count
    """
    rng = np.random.RandomState(seed)
    state = initial_state.copy()

    # Generate time points
    times = np.arange(0, params['days'] + params['dt'], params['dt'])

    # Storage for results (recording daily)
    result_times = []
    result_I = []

    # Main simulation loop
    for t in times:
        # Record state at integer days
        if int(t) == t:
            result_times.append(int(t))
            result_I.append(state['I'])

        # Update state
        state = seir_dynamics(state, params, params['dt'], rng)

    return {
        'times': result_times,
        'infected': result_I,
        'peak': max(result_I),
        'final_size': state['R']
    }


# --- BaseModel wrapper (thin adapter for the framework) ---

class StochasticSEIR(BaseModel):
    """
    Minimal stochastic SEIR model for testing job submission.

    This is a thin wrapper around the core SEIR simulation functions,
    adapting them to the BaseModel interface required by the framework.
    """

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
        """
        Build simulation state from parameters.

        Prepares initial conditions and parameters for the core simulation.
        """
        N = int(params["population"])
        I0 = int(params["initial_infected"])

        # Set up initial state
        initial_state = {
            'S': N - I0,
            'E': 0,  # Start with no exposed
            'I': I0,
            'R': 0
        }

        # Prepare simulation parameters
        sim_params = {
            'beta': float(params["beta"]),
            'sigma': float(params["sigma"]),
            'gamma': float(params["gamma"]),
            'N': N,
            'days': int(params["simulation_days"]),
            'dt': config.get("dt", 0.1)
        }

        return {
            'initial_state': initial_state,
            'params': sim_params
        }

    def run_sim(self, state: Dict, seed: int) -> Dict:
        """
        Run stochastic SEIR simulation.

        Delegates to the core simulation function.
        """
        return run_seir_simulation(
            state['initial_state'],
            state['params'],
            seed
        )

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
