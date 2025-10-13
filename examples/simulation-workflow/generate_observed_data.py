#!/usr/bin/env python
"""
Generate synthetic observed data for target evaluation.

This creates realistic-looking epidemiological data that can be used
as observation data for model calibration targets.
"""

import numpy as np
import polars as pl
from pathlib import Path


def generate_epidemic_curve(days=155, peak_day=90, peak_infected=9000, seed=42):
    """
    Generate a synthetic epidemic curve using a gamma-like distribution.

    Parameters
    ----------
    days : int
        Total number of days to simulate
    peak_day : int
        Day when infections peak
    peak_infected : int
        Number of infected at peak
    seed : int
        Random seed for reproducibility
    """
    np.random.seed(seed)

    # Create time points
    t = np.arange(days)

    # Use a gamma-like curve for realistic epidemic shape
    # Rising phase: exponential growth
    # Falling phase: exponential decay
    infected = np.zeros(days)

    # Parameters for the curve
    growth_rate = 0.15  # Daily growth rate during exponential phase
    decay_rate = 0.08   # Daily decay rate after peak

    for i, day in enumerate(t):
        if day <= peak_day:
            # Exponential growth phase with some noise
            infected[i] = np.exp(growth_rate * day) * (1 + np.random.normal(0, 0.1))
        else:
            # Exponential decay from peak
            days_after_peak = day - peak_day
            infected[i] = peak_infected * np.exp(-decay_rate * days_after_peak) * (1 + np.random.normal(0, 0.1))

    # Normalize to get desired peak
    infected = infected * (peak_infected / np.max(infected))

    # Add some realistic noise
    infected = np.maximum(1, infected + np.random.normal(0, infected * 0.05))

    # Round to integers
    infected = np.round(infected).astype(int)

    return t, infected


def main():
    """Generate and save observed prevalence data."""

    # Create data directory if it doesn't exist
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    # Generate daily observations
    days, infected = generate_epidemic_curve(
        days=155,
        peak_day=85,
        peak_infected=8500,
        seed=42
    )

    # Create DataFrame with daily data
    daily_df = pl.DataFrame({
        "day": days.tolist(),
        "infected": infected.tolist()
    })

    # Save daily observations
    daily_file = data_dir / "observed_prevalence_daily.csv"
    daily_df.write_csv(daily_file)
    print(f"Generated daily observations: {daily_file}")
    print(f"  Days: {len(daily_df)}")
    print(f"  Peak: {daily_df['infected'].max()} on day {daily_df.filter(pl.col('infected') == daily_df['infected'].max())['day'][0]}")

    # Also create weekly sampled data (more realistic for real observations)
    weekly_df = daily_df.filter(pl.col("day") % 7 == 0)
    weekly_file = data_dir / "observed_prevalence_weekly.csv"
    weekly_df.write_csv(weekly_file)
    print(f"\nGenerated weekly observations: {weekly_file}")
    print(f"  Observations: {len(weekly_df)}")

    # Create sparse observations (every 5 days, more typical of real data)
    sparse_df = daily_df.filter(pl.col("day") % 5 == 0)
    sparse_file = data_dir / "observed_prevalence.csv"
    sparse_df.write_csv(sparse_file)
    print(f"\nGenerated sparse observations: {sparse_file}")
    print(f"  Observations: {len(sparse_df)}")

    # Show sample of the data
    print("\nSample of sparse observations:")
    print(sparse_df.head(10))


if __name__ == "__main__":
    main()