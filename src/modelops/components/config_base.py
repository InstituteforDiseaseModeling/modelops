"""Base configuration model with YAML loading capabilities."""

from pathlib import Path
from typing import Any, TypeVar

import typer
import yaml
from pydantic import BaseModel, ValidationError
from rich.console import Console

T = TypeVar("T", bound="ConfigModel")
console = Console()


class ConfigModel(BaseModel):
    """Base model with YAML loading/saving capabilities."""

    @classmethod
    def from_yaml(cls: type[T], path: Path) -> T:
        """
        Load and validate configuration from YAML file.

        Args:
            path: Path to YAML configuration file

        Returns:
            Validated configuration model instance

        Raises:
            typer.Exit: On file not found, invalid YAML, or validation errors
        """
        if not path.exists():
            console.print(f"[red]Configuration file not found:[/red] {path}")
            raise typer.Exit(1)

        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            cls._handle_yaml_error(e, path)
        except Exception as e:
            console.print(f"[red]Error reading configuration file:[/red] {path}")
            console.print(f"[dim]{e}[/dim]")
            raise typer.Exit(1)

        try:
            return cls(**data)
        except ValidationError as e:
            cls._handle_validation_error(e, path)

    @classmethod
    def from_yaml_optional(cls: type[T], path: Path | None) -> T | None:
        """
        Load configuration from YAML if path provided and exists.

        Args:
            path: Optional path to YAML configuration file

        Returns:
            Validated configuration model instance or None
        """
        if path and path.exists():
            return cls.from_yaml(path)
        return None

    @classmethod
    def load_or_default(cls: type[T], path: Path | None, **defaults) -> T:
        """
        Load from YAML or create with default values.

        Args:
            path: Optional path to YAML configuration file
            **defaults: Default values if file not provided

        Returns:
            Configuration model instance
        """
        if path and path.exists():
            return cls.from_yaml(path)
        return cls(**defaults)

    def to_yaml(self, path: Path):
        """
        Write configuration to YAML file.

        Args:
            path: Path to write YAML file
        """
        with open(path, "w") as f:
            yaml.safe_dump(
                self.model_dump(by_alias=True, exclude_unset=False),
                f,
                default_flow_style=False,
                sort_keys=False,
            )

    def to_yaml_string(self) -> str:
        """
        Convert configuration to YAML string.

        Returns:
            YAML formatted string of the configuration
        """
        return yaml.dump(
            self.model_dump(by_alias=True, exclude_unset=False),
            default_flow_style=False,
            sort_keys=False,
        )

    def to_pulumi_config(self) -> dict[str, Any]:
        """
        Convert to dictionary for Pulumi consumption.

        Returns:
            Dictionary with aliases applied, suitable for Pulumi
        """
        return self.model_dump(by_alias=True, exclude_unset=True)

    @classmethod
    def _handle_validation_error(cls, error: ValidationError, path: Path):
        """Pretty-print validation errors."""
        console.print(f"[red]Invalid {cls.__name__} configuration:[/red] {path.name}\n")

        for err in error.errors():
            # Build field path
            field_path = " → ".join(str(loc) for loc in err["loc"])

            # Format error based on type
            error_type = err["type"]
            if "missing" in error_type:
                console.print(f"  [yellow]Missing required field:[/yellow] {field_path}")
            elif (
                "assertion_error" in error_type
                or "value_error" in error_type
                or "literal_error" in error_type
            ):
                console.print(f"  [yellow]{field_path}:[/yellow] {err['msg']}")
            else:
                console.print(f"  [yellow]{field_path}:[/yellow] {err['msg']}")

        console.print("\n[dim]Check the configuration file format and required fields[/dim]")
        raise typer.Exit(1)

    @classmethod
    def _handle_yaml_error(cls, error: yaml.YAMLError, path: Path):
        """Handle YAML parsing errors."""
        console.print(f"[red]Invalid YAML syntax in:[/red] {path.name}")

        # Try to extract line number from error
        if hasattr(error, "problem_mark"):
            mark = error.problem_mark
            console.print(f"  Line {mark.line + 1}, Column {mark.column + 1}")

        console.print(f"\n[dim]{error}[/dim]")
        raise typer.Exit(1)
