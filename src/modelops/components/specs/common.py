"""Common shared models used across specifications."""

from typing import Any

from pydantic import BaseModel, Field, field_validator


class ResourceRequirements(BaseModel):
    """Kubernetes resource requirements for containers."""

    requests: dict[str, str] = Field(default_factory=lambda: {"memory": "1Gi", "cpu": "1"})
    limits: dict[str, str] = Field(default_factory=lambda: {"memory": "1Gi", "cpu": "1"})

    @field_validator("requests", "limits")
    @classmethod
    def validate_resources(cls, v):
        """Validate resource format."""
        valid_keys = {"memory", "cpu", "nvidia.com/gpu", "ephemeral-storage"}
        for key in v:
            if key not in valid_keys:
                raise ValueError(f"Unknown resource type: {key}. Valid types: {valid_keys}")
        return v


class EnvVar(BaseModel):
    """Kubernetes environment variable specification."""

    name: str
    value: str | None = None
    valueFrom: dict[str, Any] | None = Field(None, alias="value_from")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        """Validate environment variable name."""
        if not v:
            raise ValueError("Environment variable name cannot be empty")
        # K8s env var names must be C_IDENTIFIER
        import re

        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", v):
            raise ValueError(
                f"Invalid environment variable name: {v}. "
                "Must start with letter or underscore, contain only alphanumeric and underscores"
            )
        return v


class Toleration(BaseModel):
    """Kubernetes toleration specification."""

    key: str
    operator: str = "Equal"
    value: str | None = None
    effect: str | None = None
    tolerationSeconds: int | None = Field(None, alias="toleration_seconds")
