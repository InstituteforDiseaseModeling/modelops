"""CLI commands for managing and viewing simulation results."""

import typer
import json
from pathlib import Path
from typing import Optional
from datetime import datetime

from ..services.provenance_store import ProvenanceStore
from ..services.provenance_schema import (
    ProvenanceSchema,
    BUNDLE_INVALIDATION_SCHEMA,
    TOKEN_INVALIDATION_SCHEMA
)
from .display import console, success, warning, error, info, section, info_dict

app = typer.Typer(
    name="results",
    help="View and manage simulation results",
    no_args_is_help=True
)


@app.command()
def list(
    storage_dir: Path = typer.Option(
        Path("/tmp/modelops/provenance"),
        "--storage-dir",
        "-s",
        help="Storage directory for provenance store"
    ),
    result_type: str = typer.Option(
        "sim",
        "--type",
        "-t",
        help="Type of results to list: sim or agg"
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        "-n",
        help="Maximum number of results to show"
    ),
    schema: str = typer.Option(
        "bundle",
        "--schema",
        help="Schema to use: bundle or token"
    )
):
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
                table.add_row(
                    r.get("bundle_ref", "")[:20] + "..." if len(r.get("bundle_ref", "")) > 20 else r.get("bundle_ref", ""),
                    r.get("entrypoint", ""),
                    r.get("param_id", "")[:8],
                    str(r.get("seed", "")),
                    r.get("path", "").replace(str(storage_dir) + "/", "")[:40] + "..."
                )
        else:  # agg
            table.add_column("Bundle Ref", style="dim")
            table.add_column("Target")
            table.add_column("Num Results")
            table.add_column("Path", style="dim")

            for r in results:
                table.add_row(
                    r.get("bundle_ref", "")[:20] + "..." if len(r.get("bundle_ref", "")) > 20 else r.get("bundle_ref", ""),
                    r.get("target_entrypoint", ""),
                    str(r.get("n_sim_returns", "")),
                    r.get("path", "").replace(str(storage_dir) + "/", "")[:40] + "..."
                )

        console.print(table)

    except Exception as e:
        error(f"Failed to list results: {e}")
        raise typer.Exit(code=1)


@app.command()
def show(
    path: Path = typer.Argument(
        ...,
        help="Path to result directory (from list command)"
    ),
    output_format: str = typer.Option(
        "summary",
        "--format",
        "-f",
        help="Output format: summary, json, or artifacts"
    )
):
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

        with open(metadata_file, "r") as f:
            metadata = json.load(f)

        # Load result
        result_file = path / "result.json"
        if result_file.exists():
            with open(result_file, "r") as f:
                result = json.load(f)
        else:
            result = None

        # Load manifest if present
        manifest_file = path / "manifest.json"
        if manifest_file.exists():
            with open(manifest_file, "r") as f:
                manifest = json.load(f)
        else:
            manifest = None

        # Display based on format
        if output_format == "json":
            # Full JSON output
            output = {
                "metadata": metadata,
                "result": result,
                "manifest": manifest
            }
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

            info_dict({
                "Bundle": metadata.get("bundle_ref", "N/A"),
                "Entrypoint": metadata.get("entrypoint", metadata.get("target_entrypoint", "N/A")),
                "Params": metadata.get("param_id", "N/A")[:16] if metadata.get("param_id") else "N/A",
                "Seed": metadata.get("seed", "N/A")
            })

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
        help="Storage directory for provenance store"
    ),
    schema_name: Optional[str] = typer.Option(
        None,
        "--schema",
        help="Schema to clear (default: current schema)"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Skip confirmation prompt"
    )
):
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
        help="Storage directory for provenance store"
    )
):
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
                    if "sims" in str(path):
                        num_sims += 1
                    elif "aggs" in str(path):
                        num_aggs += 1
                elif path.name.startswith("artifact_"):
                    num_artifacts += 1

        section("Storage Statistics")
        info_dict({
            "Storage directory": str(storage_dir),
            "Total size": f"{total_size / (1024*1024):.2f} MB",
            "Simulation results": str(num_sims),
            "Aggregation results": str(num_aggs),
            "Artifact files": str(num_artifacts)
        })

        # Show schema breakdown
        schema_dirs = [d for d in storage_dir.iterdir() if d.is_dir()]
        if schema_dirs:
            section("Schemas")
            for schema_dir in schema_dirs:
                schema_size = sum(f.stat().st_size for f in schema_dir.rglob("*") if f.is_file())
                info(f"  {schema_dir.name}: {schema_size / (1024*1024):.2f} MB")

    except Exception as e:
        error(f"Failed to get statistics: {e}")
        raise typer.Exit(code=1)