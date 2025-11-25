"""
Starsim-based SIR model for epidemiological simulations.

This model uses the Starsim agent-based modeling framework to simulate
SIR (Susceptible-Infected-Recovered) dynamics with proper contact networks.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any
import numpy as np
import polars as pl
import starsim as ss
import modelops_calabaria as cb
from modelops_calabaria import BaseModel, ParameterSpace, ParameterSet, ParameterSpec, Scalar, model_output
from typing import Mapping


@dataclass(frozen=True)
class SIRConfig:
    """Fixed configuration for SIR simulations."""
    population: int = 1000
    initial_infected: int = 2
    simulation_days: int = 60
    network_contacts: int = 4  # Average degree in contact network


class StarsimSIR(BaseModel):
    """
    Starsim-based SIR model with fixed contact network.

    This model creates a single fixed contact network that is reused across
    all simulation runs to ensure consistency. Only the disease transmission
    dynamics vary between runs based on the random seed.
    """

    def __init__(self, space: Optional[ParameterSpace] = None):
        """Initialize the SIR model with parameter space."""
        if space is None:
            space = self.parameter_space()
        self.config = SIRConfig()
        # Pass empty base_config to BaseModel
        super().__init__(space, base_config={})

        # Network will be created in build_sim to avoid memory issues
        self.network = None
        # Store initial infected agent IDs (deterministic)
        self.initial_infected_ids = list(range(self.config.initial_infected))

    @staticmethod
    def parameter_space() -> ParameterSpace:
        """
        Define the parameter space for SIR model calibration.

        Only disease dynamics parameters are included:
        - beta: Transmission probability per contact
        - dur_inf: Duration of infection (days)
        """
        return ParameterSpace([
            ParameterSpec("beta", 0.01, 0.2, "float", doc="Transmission probability per contact"),
            ParameterSpec("dur_inf", 3.0, 10.0, "float", doc="Duration of infection (days)"),
        ])

    def build_sim(self, params: ParameterSet, config: Mapping[str, Any]) -> Dict[str, Any]:
        """
        Build simulation state from parameters.

        Args:
            params: Parameter set with beta and dur_inf
            config: Additional configuration (unused for compatibility)

        Returns:
            Dictionary containing simulation state
        """
        # Create network here to avoid memory issues with thousands of instances
        if self.network is None:
            self.network = ss.RandomNet(n_contacts=self.config.network_contacts)

        return {
            'beta': params['beta'],
            'dur_inf': params['dur_inf'],
            'config': self.config,
            'network': self.network,
            'initial_infected_ids': self.initial_infected_ids,
        }

    def run_sim(self, state: Dict[str, Any], seed: int = 42) -> Dict[str, Any]:
        """
        Run a single SIR simulation with given parameters.

        Args:
            state: Simulation state from build_sim
            seed: Random seed for stochastic simulation

        Returns:
            Dictionary containing simulation results
        """
        # Extract parameters
        beta = state['beta']
        dur_inf = state['dur_inf']
        config = state['config']

        # Create SIR disease model
        sir = ss.SIR(
            beta=beta,
            dur_inf=dur_inf,
            init_prev=0.0,  # We'll manually set initial infections
            p_death=0.0     # No deaths in basic SIR
        )

        # Create simulation
        sim = ss.Sim(
            diseases=sir,
            networks=state['network'],  # Use the fixed network
            n_agents=config.population,
            start=0,
            stop=config.simulation_days,
            rand_seed=seed,
            verbose=0
        )

        # Initialize simulation
        sim.init()

        # Set initial infections deterministically
        uids = ss.uids(state['initial_infected_ids'])
        sim.people.sir.susceptible[uids] = False
        sim.people.sir.infected[uids] = True
        sim.people.sir.ti_infected[uids] = 0

        # Run simulation
        sim.run()

        # Extract results
        results = {
            'incidence': sim.results.sir.new_infections,  # Daily new infections
            'prevalence': sim.results.sir.n_infected,     # Daily infected count
            'cumulative': np.cumsum(sim.results.sir.new_infections),
            'recovered': sim.results.sir.n_recovered,
            'susceptible': sim.results.sir.n_susceptible,
        }

        return results

    @model_output("incidence")
    def extract_incidence(self, raw_output: Dict[str, Any], seed: int) -> pl.DataFrame:
        """
        Extract daily incidence (new infections) time series.

        Args:
            raw_output: Raw simulation output from run_sim
            seed: Random seed used for the simulation

        Returns:
            DataFrame with columns: day, incidence (new infections), seed
        """
        incidence = raw_output['incidence']
        days = np.arange(len(incidence))

        return pl.DataFrame({
            'day': days.tolist(),
            'incidence': incidence.tolist(),
        })

    @model_output("prevalence")
    def extract_prevalence(self, raw_output: Dict[str, Any], seed: int) -> pl.DataFrame:
        """
        Extract daily prevalence (total infected) time series.

        Args:
            raw_output: Raw simulation output from run_sim
            seed: Random seed used for the simulation

        Returns:
            DataFrame with columns: day, prevalence (total infected), seed
        """
        prevalence = raw_output['prevalence']
        days = np.arange(len(prevalence))

        return pl.DataFrame({
            'day': days.tolist(),
            'prevalence': prevalence.tolist(),
        })

    @model_output("cumulative")
    def extract_cumulative(self, raw_output: Dict[str, Any], seed: int) -> pl.DataFrame:
        """
        Extract cumulative infections time series.

        Args:
            raw_output: Raw simulation output from run_sim
            seed: Random seed used for the simulation

        Returns:
            DataFrame with columns: day, cumulative (cumulative), seed
        """
        cumulative = raw_output['cumulative']
        days = np.arange(len(cumulative))

        return pl.DataFrame({
            'day': days.tolist(),
            'cumulative': cumulative.tolist(),
        })
