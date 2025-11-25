"""CLI commands for managing and viewing simulation results."""

import json
import os
from pathlib import Path

import typer

from ..services.provenance_schema import (
    BUNDLE_INVALIDATION_SCHEMA,
    TOKEN_INVALIDATION_SCHEMA,
)
from ..services.provenance_store import ProvenanceStore
from .display import console, error, info, info_dict, section, success, warning

app = typer.Typer(name="results", help="View and manage simulation results", no_args_is_help=True)


# Helper functions
def _safe_str(x) -> str:
    """Convert value to string, handling None safely."""
    return "" if x is None else str(x)


def _relpath_maybe(p: str, root: os.PathLike) -> str:
    """Get relative path safely, falling back to original on error."""
    try:
        return os.path.relpath(p, root)
    except (ValueError, TypeError):
        return p


def _ellipsize(s: str, maxlen: int) -> str:
    """Add ellipsis only if string exceeds maxlen."""
    s = s or ""
    return s if len(s) <= maxlen else s[: maxlen - 1] + "…"


@app.command("list")
def cmd_list(
    storage_dir: Path = typer.Option(
        Path("/tmp/modelops/provenance"),
        "--storage-dir",
        "-s",
        help="Storage directory for provenance store",
    ),
    result_type: str = typer.Option(
        "sim", "--type", "-t", help="Type of results to list: sim or agg"
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of results to show"),
    schema: str = typer.Option("bundle", "--schema", help="Schema to use: bundle or token"),
) -> None:
    """List stored simulation or aggregation results."""
    try:
        # Select schema
        if schema == "token":
            provenance_schema = TOKEN_INVALIDATION_SCHEMA
        else:
            provenance_schema = BUNDLE_INVALIDATION_SCHEMA

        # Create provenance store
        store = ProvenanceStore(storage_dir, schema=provenance_schema)

        # List results
        results = store.list_results(result_type=result_type, limit=limit)

        if not results:
            warning(f"No {result_type} results found in {storage_dir}")
            return

        section(f"Found {len(results)} {result_type} results")

        # Display results in a table
        from rich.table import Table

        table = Table(show_header=True, header_style="bold magenta")
        if result_type == "sim":
            table.add_column("Bundle Ref", style="dim")
            table.add_column("Entrypoint")
            table.add_column("Param ID", style="cyan")
            table.add_column("Seed")
            table.add_column("Path", style="dim")

            for r in results:
                bundle_ref = _safe_str(r.get("bundle_ref"))
                entrypoint = _safe_str(r.get("entrypoint"))
                param_id = _safe_str(r.get("param_id"))[:8]
                seed = _safe_str(r.get("seed"))
                path = _safe_str(r.get("path"))
                rel_path = _relpath_maybe(path, storage_dir)

                table.add_row(
                    _ellipsize(bundle_ref, 20),
                    entrypoint,
                    param_id,
                    seed,
                    _ellipsize(rel_path, 60),
                )
        else:  # agg
            table.add_column("Bundle Ref", style="dim")
            table.add_column("Target")
            table.add_column("Num Results")
            table.add_column("Path", style="dim")

            for r in results:
                bundle_ref = _safe_str(r.get("bundle_ref"))
                target = _safe_str(r.get("target_entrypoint"))
                n_results = _safe_str(r.get("n_sim_returns"))
                path = _safe_str(r.get("path"))
                rel_path = _relpath_maybe(path, storage_dir)

                table.add_row(
                    _ellipsize(bundle_ref, 20),
                    target,
                    n_results,
                    _ellipsize(rel_path, 60),
                )

        console.print(table)

    except Exception as e:
        error(f"Failed to list results: {e}")
        raise typer.Exit(code=1)


@app.command()
def show(
    path: Path = typer.Argument(..., help="Path to result directory (from list command)"),
    output_format: str = typer.Option(
        "summary", "--format", "-f", help="Output format: summary, json, or artifacts"
    ),
) -> None:
    """Show details of a specific result."""
    try:
        if not path.exists():
            error(f"Result path not found: {path}")
            raise typer.Exit(code=1)

        # Load metadata
        metadata_file = path / "metadata.json"
        if not metadata_file.exists():
            error(f"No metadata.json found in {path}")
            raise typer.Exit(code=1)

        with open(metadata_file) as f:
            metadata = json.load(f)

        # Load result
        result_file = path / "result.json"
        if result_file.exists():
            with open(result_file) as f:
                result = json.load(f)
        else:
            result = None

        # Load manifest if present
        manifest_file = path / "manifest.json"
        if manifest_file.exists():
            with open(manifest_file) as f:
                manifest = json.load(f)
        else:
            manifest = None

        # Display based on format
        if output_format == "json":
            # Full JSON output
            output = {"metadata": metadata, "result": result, "manifest": manifest}
            console.print_json(data=output)

        elif output_format == "artifacts":
            # List artifacts
            section("Artifacts")
            artifact_files = list(path.glob("artifact_*.arrow"))
            if artifact_files:
                for af in artifact_files:
                    name = af.stem.replace("artifact_", "")
                    size = af.stat().st_size
                    info(f"  {name}: {size:,} bytes")
            else:
                warning("No artifact files found")

        else:  # summary
            # Summary view
            section("Result Summary")

            info_dict(
                {
                    "Bundle": metadata.get("bundle_ref", "N/A"),
                    "Entrypoint": metadata.get(
                        "entrypoint", metadata.get("target_entrypoint", "N/A")
                    ),
                    "Params": metadata.get("param_id", "N/A")[:16]
                    if metadata.get("param_id")
                    else "N/A",
                    "Seed": metadata.get("seed", "N/A"),
                }
            )

            if result:
                section("Outputs")
                for name, output_info in result.get("outputs", {}).items():
                    info(f"  {name}: {output_info.get('size', 0):,} bytes")

            if manifest:
                section("Manifest")
                info(f"  Bundle digest: {manifest.get('bundle_digest', 'N/A')[:16]}")
                info(f"  Models: {', '.join(manifest.get('models', {}).keys())}")

    except Exception as e:
        error(f"Failed to show result: {e}")
        raise typer.Exit(code=1)


@app.command()
def clear(
    storage_dir: Path = typer.Option(
        Path("/tmp/modelops/provenance"),
        "--storage-dir",
        "-s",
        help="Storage directory for provenance store",
    ),
    schema_name: str | None = typer.Option(
        None, "--schema", help="Schema to clear (default: current schema)"
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
) -> None:
    """Clear cached results for a schema."""
    try:
        # Confirm if not forced
        if not force:
            if not typer.confirm(f"Clear all results for schema '{schema_name or 'current'}'?"):
                warning("Cancelled")
                return

        # Create provenance store
        store = ProvenanceStore(storage_dir)

        # Clear schema
        store.clear_schema(schema_name)

        success(f"Cleared results for schema '{schema_name or store.schema.name}'")

    except Exception as e:
        error(f"Failed to clear results: {e}")
        raise typer.Exit(code=1)


@app.command()
def stats(
    storage_dir: Path = typer.Option(
        Path("/tmp/modelops/provenance"),
        "--storage-dir",
        "-s",
        help="Storage directory for provenance store",
    ),
) -> None:
    """Show storage statistics."""
    try:
        if not storage_dir.exists():
            warning(f"Storage directory not found: {storage_dir}")
            return

        # Calculate statistics
        total_size = 0
        num_sims = 0
        num_aggs = 0
        num_artifacts = 0

        for path in storage_dir.rglob("*"):
            if path.is_file():
                total_size += path.stat().st_size
                if path.name == "metadata.json":
                    if "sims" in path.parts:
                        num_sims += 1
                    elif "aggs" in path.parts:
                        num_aggs += 1
                elif path.name.startswith("artifact_"):
                    num_artifacts += 1

        section("Storage Statistics")
        info_dict(
            {
                "Storage directory": str(storage_dir),
                "Total size": f"{total_size / (1024 * 1024):.2f} MB",
                "Simulation results": str(num_sims),
                "Aggregation results": str(num_aggs),
                "Artifact files": str(num_artifacts),
            }
        )

        # Show schema breakdown
        schema_dirs = [d for d in storage_dir.iterdir() if d.is_dir()]
        if schema_dirs:
            section("Schemas")
            for schema_dir in schema_dirs:
                schema_size = sum(f.stat().st_size for f in schema_dir.rglob("*") if f.is_file())
                info(f"  {schema_dir.name}: {schema_size / (1024 * 1024):.2f} MB")

    except Exception as e:
        error(f"Failed to get statistics: {e}")
        raise typer.Exit(code=1)


@app.command()
def download(
    job_id: str | None = typer.Argument(
        None, help="Job ID to download results for (or latest if not specified)"
    ),
    output_dir: Path = typer.Option(
        Path("./results"),
        "--output",
        "-o",
        help="Output directory for downloaded files",
    ),
    targets: str | None = typer.Option(
        None,
        "--targets",
        "-t",
        help="Comma-separated list of targets to download (default: all)",
    ),
    fmt: str = typer.Option(
        "parquet", "--format", "-f", help="Download format: parquet, manifest, or all"
    ),
    csv: bool = typer.Option(
        False, "--csv", help="Convert Parquet files to CSV after download"
    ),
    env: str | None = typer.Option(
        None, "--env", "-e", help="Environment name (dev, staging, prod)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed progress"),
) -> None:
    """Download job results from Azure blob storage.

    Downloads Parquet files and manifest for completed jobs from Azure.
    If no job ID is specified, uses the latest completed job.

    Examples:
        mops results download                     # Download latest job results
        mops results download job-abc123          # Download specific job
        mops results download -o ./my-results     # Custom output directory
        mops results download --targets prevalence,incidence  # Specific targets
        mops results download --csv               # Download and convert to CSV
    """
    import os

    from azure.storage.blob import BlobServiceClient

    try:
        # Get connection string from environment or Pulumi stack
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if not conn_str:
            # Try to get from Pulumi stack (canonical source)
            from ..core.automation import get_stack_output
            from ..core.config import ModelOpsConfig

            # Load config to get environment
            try:
                config = ModelOpsConfig.load()
                actual_env = env or config.environment or "dev"
            except:
                actual_env = env or "dev"

            # Try to get from storage component
            if verbose:
                info(
                    f"Getting connection string from Pulumi stack (storage component, env={actual_env})..."
                )

            conn_str = get_stack_output("storage", "connection_string", actual_env)

            if not conn_str:
                # Fallback: try to get from infra component (some deployments might have it there)
                conn_str = get_stack_output("infra", "storageConnectionString", actual_env)

            if not conn_str:
                error("Could not get Azure storage connection string from Pulumi stacks.")
                info(f"Tried stacks: modelops-storage-{actual_env}, modelops-infra-{actual_env}")
                info("\nOptions:")
                info("1. Ensure storage is deployed: mops infra up")
                info("2. Set AZURE_STORAGE_CONNECTION_STRING environment variable")
                info(
                    f"3. Get it manually: pulumi stack output connectionString --stack modelops-storage-{actual_env}"
                )
                raise typer.Exit(code=1)

        # Create blob service client
        blob_service = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service.get_container_client("results")

        # If no job_id, try to find the latest
        if not job_id:
            if verbose:
                info("Looking for latest completed job (by manifest)...")
            else:
                info("Looking for latest job...")
            # List all job manifest files to find latest completed job
            prefix = "views/jobs/"
            blobs = container_client.list_blobs(name_starts_with=prefix)

            # Track latest job by manifest.json or calibration/summary.json modification time
            latest_job = None
            latest_time = None

            for blob in blobs:
                # Look at manifest.json (simulation jobs) or calibration/summary.json (calibration jobs)
                if blob.name.endswith("/manifest.json") or blob.name.endswith("/calibration/summary.json"):
                    parts = blob.name.split("/")
                    if len(parts) >= 3 and (
                        parts[2].startswith("job-") or parts[2].startswith("calib-")
                    ):
                        job_id_candidate = parts[2]
                        blob_time = blob.last_modified

                        # Track the latest by modification time
                        if latest_time is None or blob_time > latest_time:
                            latest_time = blob_time
                            latest_job = job_id_candidate

            if not latest_job:
                error("No completed jobs found in Azure storage (looked for manifest.json or calibration/summary.json)")
                raise typer.Exit(code=1)

            job_id = latest_job
            info(
                f"Using latest job: {job_id} (modified: {latest_time.strftime('%Y-%m-%d %H:%M:%S UTC')})"
            )

        # Create output directory
        job_output_dir = output_dir / job_id
        job_output_dir.mkdir(parents=True, exist_ok=True)

        section(f"Downloading results for job {job_id}")

        # Download manifest if requested
        if fmt in ["manifest", "all"]:
            manifest_blob = f"views/jobs/{job_id}/manifest.json"
            manifest_path = job_output_dir / "manifest.json"
            try:
                blob_client = container_client.get_blob_client(manifest_blob)
                with open(manifest_path, "wb") as f:
                    download_stream = blob_client.download_blob()
                    f.write(download_stream.readall())
                success(f"Downloaded manifest to {manifest_path}")

                # Parse manifest to show summary
                with open(manifest_path) as f:
                    manifest = json.load(f)
                    if "targets" in manifest:
                        info(f"Available targets: {', '.join(manifest['targets'].keys())}")
            except Exception as e:
                if verbose:
                    warning(f"Could not download manifest: {e}")

        # Download Parquet files
        if fmt in ["parquet", "all"]:
            # List all parquet files for this job
            prefix = f"views/jobs/{job_id}/targets/"
            blobs = container_client.list_blobs(name_starts_with=prefix)

            downloaded = 0
            for blob in blobs:
                if blob.name.endswith(".parquet"):
                    # Extract target name from path
                    parts = blob.name.split("/")
                    if len(parts) >= 5:
                        target_name = parts[4]

                        # Check if we should download this target
                        if targets:
                            target_list = [t.strip() for t in targets.split(",")]
                            if target_name not in target_list:
                                continue

                        # Create target directory
                        target_dir = job_output_dir / "targets" / target_name
                        target_dir.mkdir(parents=True, exist_ok=True)

                        # Download the Parquet file
                        output_path = target_dir / "data.parquet"
                        blob_client = container_client.get_blob_client(blob.name)

                        with open(output_path, "wb") as f:
                            download_stream = blob_client.download_blob()
                            f.write(download_stream.readall())

                        info(f"Downloaded {target_name} ({blob.size:,} bytes)")
                        downloaded += 1

            if downloaded > 0:
                success(f"Downloaded {downloaded} Parquet file(s) to {job_output_dir}")
            else:
                warning("No Parquet files found for this job")

        # Download model outputs
        if fmt in ["parquet", "all"]:
            prefix = f"views/jobs/{job_id}/model_outputs/"
            blobs = container_client.list_blobs(name_starts_with=prefix)

            outputs_downloaded = 0
            for blob in blobs:
                if blob.name.endswith(".parquet"):
                    # Extract output name from path
                    output_name = Path(blob.name).stem

                    # Create model_outputs directory
                    outputs_dir = job_output_dir / "model_outputs"
                    outputs_dir.mkdir(parents=True, exist_ok=True)

                    # Download the Parquet file
                    output_path = outputs_dir / f"{output_name}.parquet"
                    blob_client = container_client.get_blob_client(blob.name)

                    with open(output_path, "wb") as f:
                        download_stream = blob_client.download_blob()
                        f.write(download_stream.readall())

                    info(f"Downloaded model output: {output_name} ({blob.size:,} bytes)")
                    outputs_downloaded += 1

            if outputs_downloaded > 0:
                success(f"Downloaded {outputs_downloaded} model output(s)")

        # Download calibration results if they exist (for calibration jobs)
        calibration_blob = f"views/jobs/{job_id}/calibration/summary.json"
        try:
            blob_client = container_client.get_blob_client(calibration_blob)

            # Create calibration directory
            calib_dir = job_output_dir / "calibration"
            calib_dir.mkdir(parents=True, exist_ok=True)

            calib_path = calib_dir / "summary.json"
            with open(calib_path, "wb") as f:
                download_stream = blob_client.download_blob()
                f.write(download_stream.readall())

            success("Downloaded calibration summary")

            # Parse and display calibration summary
            with open(calib_path) as f:
                calib_data = json.load(f)
                section("Calibration Results")
                info(f"  Algorithm: {calib_data.get('algorithm', 'N/A')}")
                summary = calib_data.get("summary", {})
                info(
                    f"  Trials completed: {summary.get('n_completed', 'N/A')}/{summary.get('n_trials', 'N/A')}"
                )
                info(f"  Best loss: {summary.get('best_value', 'N/A')}")
                if calib_data.get("best_params"):
                    info("  Best parameters:")
                    for param, value in calib_data["best_params"].items():
                        info(f"    {param}: {value}")

        except Exception as e:
            if verbose:
                warning(f"Could not download calibration results: {e}")
            # Not all jobs have calibration results, so this is not an error

        # Convert Parquet to CSV if requested
        if csv:
            import polars as pl

            section("Converting Parquet to CSV")
            parquet_files = list(job_output_dir.rglob("*.parquet"))

            for parquet_path in parquet_files:
                try:
                    csv_path = parquet_path.with_suffix(".csv")
                    info(f"Converting {parquet_path.name}...")

                    df = pl.read_parquet(parquet_path)
                    df.write_csv(csv_path)

                    info(f"  → {csv_path.name} ({csv_path.stat().st_size:,} bytes)")
                except Exception as e:
                    warning(f"Failed to convert {parquet_path.name}: {e}")

            success(f"Converted {len(parquet_files)} Parquet file(s) to CSV")

        # Show summary
        section("Download complete")
        info(f"Results saved to: {job_output_dir}")

        # Suggest next steps
        info("\nNext steps:")
        info("  - Load Parquet files with polars: pl.read_parquet('path/to/data.parquet')")
        info("  - Or use DuckDB for SQL queries: duckdb.sql('SELECT * FROM read_parquet(...)')")

    except Exception as e:
        error(f"Failed to download results: {e}")
        if verbose:
            import traceback

            console.print(traceback.format_exc())
        raise typer.Exit(code=1)
