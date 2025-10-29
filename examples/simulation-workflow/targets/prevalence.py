"""
Prevalence target for SEIR model calibration.

This target compares simulated infection prevalence against observed data
using mean squared error as the loss function.
"""

import polars as pl
import modelops_calabaria as cb
from modelops_calabaria.core.target import Target
from modelops_calabaria.core.alignment import JoinAlignment
from modelops_calabaria.core.evaluation import mean_of_per_replicate_mse, replicate_mean_mse


@cb.calibration_target(
    model_output="prevalence",
    data={
        'observed': "data/observed_prevalence.csv"
    }
)
def prevalence_target(data_paths):
    """
    Target comparing simulated vs observed prevalence.

    Args:
        data_paths: Dict with paths to data files from decorator

    Returns:
        Target: Configured target for prevalence evaluation
    """
    # Load the observation data
    observed_data = pl.read_csv(data_paths['observed'])

    # Create and return the target
    return Target(
        model_output="prevalence",  # Must match @model_output name in model
        data=observed_data,
        alignment=JoinAlignment(
            on_cols="day",  # Join simulation and observation on day column
            mode="exact"    # Require exact day matches (no interpolation)
        ),
        evaluation=replicate_mean_mse(col="infected"),
        weight=1.0  # Weight for multi-target optimization
    )
