"""
Incidence targets for SIR model calibration.

This module provides two targets with different evaluation strategies:
1. replicate_mean_mse: Averages replicates first, then computes MSE
2. mean_of_per_replicate_mse: Computes MSE per replicate, then averages
"""

import polars as pl
import modelops_calabaria as cb
from modelops_calabaria.core.target import Target
from modelops_calabaria.core.alignment import JoinAlignment
from modelops_calabaria.core.evaluation import replicate_mean_mse, mean_of_per_replicate_mse


@cb.calibration_target(
    model_output="incidence",
    data={
        'observed': "data/observed_incidence.csv"  # Use clean or noisy version
    }
)
def incidence_replicate_mean_target(data_paths):
    """
    Target comparing simulated vs observed daily incidence using replicate-mean MSE.

    This evaluation strategy first averages the simulation results across all
    replicates, then computes MSE against the observed data. This approach
    reduces noise from individual replicates.

    Args:
        data_paths: Dict with paths to data files from decorator

    Returns:
        Target: Configured target for incidence evaluation with replicate-mean MSE
    """
    # Load the observation data
    observed_data = pl.read_csv(data_paths['observed'])

    # Create and return the target
    return Target(
        model_output="incidence",
        data=observed_data,
        alignment=JoinAlignment(
            on_cols="day",
            mode="exact"
        ),
        evaluation=replicate_mean_mse(col="infected"),
        weight=1.0
    )


@cb.calibration_target(
    model_output="incidence",
    data={
        'observed': "data/observed_incidence.csv"  # Use clean or noisy version
    }
)
def incidence_per_replicate_target(data_paths):
    """
    Target comparing simulated vs observed daily incidence using per-replicate MSE.

    This evaluation strategy computes MSE for each simulation replicate separately,
    then takes the mean of these MSE values. This gives equal weight to each
    replicate regardless of the number of data points.

    Args:
        data_paths: Dict with paths to data files from decorator

    Returns:
        Target: Configured target for incidence evaluation with per-replicate MSE
    """
    # Load the observation data
    observed_data = pl.read_csv(data_paths['observed'])

    # Create and return the target
    return Target(
        model_output="incidence",
        data=observed_data,
        alignment=JoinAlignment(
            on_cols="day",
            mode="exact"
        ),
        evaluation=mean_of_per_replicate_mse(col="infected"),
        weight=1.0
    )
