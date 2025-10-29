#!/usr/bin/env python
"""
Generate synthetic observed data using the Starsim SIR model.

This creates realistic epidemiological data by running the actual SIR model
with known "true" parameters, then optionally adding observation noise.
"""

import numpy as np
import polars as pl
from pathlib import Path
from models.sir import StarsimSIR, SIRConfig
from modelops_calabaria import ParameterSet
from scipy.special import gammaln


def add_negative_binomial_noise(mu: np.ndarray, dispersion: float, seed: int = 42) -> np.ndarray:
    """
    Add negative binomial observation noise to data.

    Args:
        mu: Expected values (mean of the distribution)
        dispersion: Dispersion parameter k (higher = less noise)
        seed: Random seed

    Returns:
        Noisy observations following NB(mu, k) distribution
    """
    rng = np.random.RandomState(seed)
    y = np.zeros_like(mu, dtype=float)

    for t, m in enumerate(mu):
        if m <= 0:
            y[t] = 0.0
        else:
            # Negative binomial with mean=m and dispersion=k
            # Variance = m + m^2/k
            r = dispersion
            p = r / (r + m)
            y[t] = rng.negative_binomial(r, p)

    return y.astype(int)


def main():
    """Generate and save observed data using known true parameters."""

    # Create data directory if it doesn't exist
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    # Define "true" parameters for data generation
    TRUE_BETA = 0.08      # Transmission probability
    TRUE_DUR_INF = 5.0    # Duration of infection in days

    print("Generating observed data with true parameters:")
    print(f"  Beta (transmission): {TRUE_BETA}")
    print(f"  Duration of infection: {TRUE_DUR_INF} days")
    print()

    # Initialize model
    model = StarsimSIR()
    config = model.config

    print(f"Model configuration:")
    print(f"  Population: {config.population}")
    print(f"  Initial infected: {config.initial_infected}")
    print(f"  Simulation days: {config.simulation_days}")
    print(f"  Network contacts: {config.network_contacts}")
    print()

    # Create parameter set with true values
    params = ParameterSet(model.parameter_space(), {
        'beta': TRUE_BETA,
        'dur_inf': TRUE_DUR_INF
    })

    # Build simulation state
    state = model.build_sim(params, {})

    # Run simulation with a fixed seed for reproducibility
    raw_output = model.run_sim(state, seed=42)

    # Extract incidence (new infections per day)
    incidence = raw_output['incidence']
    prevalence = raw_output['prevalence']

    # Option 1: Use clean data (no noise)
    observed_incidence_clean = incidence
    observed_prevalence_clean = prevalence

    # Option 2: Add realistic observation noise (negative binomial)
    dispersion_k = 2.0  # Lower k = more overdispersion
    observed_incidence_noisy = add_negative_binomial_noise(incidence, dispersion_k, seed=123)
    observed_prevalence_noisy = add_negative_binomial_noise(prevalence, dispersion_k, seed=456)

    # Create DataFrames
    days = np.arange(len(incidence))

    # Save clean incidence data
    df_incidence_clean = pl.DataFrame({
        'day': days.tolist(),
        'infected': observed_incidence_clean.tolist()
    })
    incidence_file = data_dir / "observed_incidence.csv"
    df_incidence_clean.write_csv(incidence_file)
    print(f"Generated clean incidence data: {incidence_file}")
    print(f"  Peak: {df_incidence_clean['infected'].max()} on day {df_incidence_clean.filter(pl.col('infected') == df_incidence_clean['infected'].max())['day'][0]}")

    # Save clean prevalence data
    df_prevalence_clean = pl.DataFrame({
        'day': days.tolist(),
        'infected': observed_prevalence_clean.tolist()
    })
    prevalence_file = data_dir / "observed_prevalence.csv"
    df_prevalence_clean.write_csv(prevalence_file)
    print(f"Generated clean prevalence data: {prevalence_file}")
    print(f"  Peak: {df_prevalence_clean['infected'].max()} on day {df_prevalence_clean.filter(pl.col('infected') == df_prevalence_clean['infected'].max())['day'][0]}")

    # Save noisy versions
    df_incidence_noisy = pl.DataFrame({
        'day': days.tolist(),
        'infected': observed_incidence_noisy.tolist()
    })
    incidence_noisy_file = data_dir / "observed_incidence_noisy.csv"
    df_incidence_noisy.write_csv(incidence_noisy_file)
    print(f"\nGenerated noisy incidence data: {incidence_noisy_file}")

    df_prevalence_noisy = pl.DataFrame({
        'day': days.tolist(),
        'infected': observed_prevalence_noisy.tolist()
    })
    prevalence_noisy_file = data_dir / "observed_prevalence_noisy.csv"
    df_prevalence_noisy.write_csv(prevalence_noisy_file)
    print(f"Generated noisy prevalence data: {prevalence_noisy_file}")

    # Save true parameters for reference
    true_params = pl.DataFrame({
        'parameter': ['beta', 'dur_inf'],
        'value': [TRUE_BETA, TRUE_DUR_INF],
        'description': ['Transmission probability', 'Duration of infection (days)']
    })
    params_file = data_dir / "true_parameters.csv"
    true_params.write_csv(params_file)
    print(f"\nSaved true parameters: {params_file}")

    # Show sample of the data
    print("\nSample of clean incidence data (first 10 days):")
    print(df_incidence_clean.head(10))

    print("\nSample of noisy incidence data (first 10 days):")
    print(df_incidence_noisy.head(10))

    print("\nData generation complete!")
    print(f"True parameters used:")
    print(f"  beta = {TRUE_BETA}")
    print(f"  dur_inf = {TRUE_DUR_INF}")


if __name__ == "__main__":
    main()