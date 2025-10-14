"""Declarative schema for provenance-based storage paths.

This module provides a Pydantic model for defining storage path templates
using a simple DSL. Different schemas create isolated storage namespaces,
enabling different invalidation strategies.
"""

import re
import hashlib
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field, field_validator


class ProvenanceSchema(BaseModel, frozen=True):
    """Declarative schema for provenance-based storage paths.

    Defines path templates using a DSL that supports variable interpolation
    and functions like hash() and shard(). Each schema instance creates
    an isolated storage namespace.

    DSL Functions:
    - {var}: Direct interpolation of variable value
    - {hash(var)[:n]}: Take first n chars of hash of variable
    - {shard(var,d,w)}: Shard based on hash for d-depth, w-width tree

    Example schemas:
    - Bundle invalidation: paths include bundle_digest
    - Token invalidation: paths include model_digest from manifest
    """

    # Schema metadata
    name: str = Field(default="default", description="Schema identifier")
    version: int = Field(default=1, description="Schema version for migrations")

    # Path templates with DSL
    root_template: str = Field(
        default="{schema_name}/v{version}",
        description="Root directory template"
    )

    sim_path_template: str = Field(
        default="sims/{bundle_digest[:12]}/{shard(param_id,2,2)}/params_{param_id[:8]}/seed_{seed}",
        description="Template for simulation result paths"
    )

    agg_path_template: str = Field(
        default="aggs/{bundle_digest[:12]}/target_{target}/agg_{aggregation_id}",
        description="Template for aggregation result paths"
    )

    job_path_template: str = Field(
        default="jobs/{job_type}/{job_id}",
        description="Template for job-level paths"
    )

    @field_validator("root_template", "sim_path_template", "agg_path_template", "job_path_template")
    @classmethod
    def validate_template(cls, v: str) -> str:
        """Validate DSL template syntax."""
        # Basic validation - ensure balanced braces
        if v.count("{") != v.count("}"):
            raise ValueError(f"Unbalanced braces in template: {v}")

        # Check for valid DSL patterns - includes slicing like param_id[:8]
        pattern = r"\{([a-z_]+(?:\[:\d+\])?|hash\([a-z_]+\)(?:\[:\d+\])?|shard\([a-z_]+,\d+,\d+\))\}"
        invalid = re.findall(r"\{[^}]+\}", v)
        for match in invalid:
            if not re.match(pattern, match):
                raise ValueError(f"Invalid DSL expression: {match}")

        return v

    def render_path(self, template: str, context: Dict[str, Any]) -> str:
        """Render a path template with the given context.

        Args:
            template: DSL template string
            context: Variables to interpolate

        Returns:
            Rendered path string
        """
        # Add schema metadata to context
        full_context = {
            "schema_name": self.name,
            "version": self.version,
            **context
        }

        # Process DSL expressions
        def replace_expr(match):
            expr = match.group(1)

            # Check if it's a function call (contains parentheses)
            if '(' not in expr:
                # Direct variable interpolation (with optional slicing)
                var_match = re.match(r"([a-z_]+)(?:\[:(\d+)\])?", expr)
                if var_match:
                    var_name = var_match.group(1)
                    length = int(var_match.group(2)) if var_match.group(2) else None

                    if var_name in full_context:
                        value = str(full_context[var_name])
                        return value[:length] if length else value

            # hash(var)[:n] function
            hash_match = re.match(r"hash\(([a-z_]+)\)(?:\[:(\d+)\])?", expr)
            if hash_match:
                var_name = hash_match.group(1)
                length = int(hash_match.group(2)) if hash_match.group(2) else None

                if var_name not in full_context:
                    raise KeyError(f"Variable {var_name} not in context")

                value = str(full_context[var_name])
                hash_val = hashlib.blake2b(value.encode(), digest_size=32).hexdigest()
                return hash_val[:length] if length else hash_val

            # shard(var,depth,width) function
            shard_match = re.match(r"shard\(([a-z_]+),(\d+),(\d+)\)", expr)
            if shard_match:
                var_name = shard_match.group(1)
                depth = int(shard_match.group(2))
                width = int(shard_match.group(3))

                if var_name not in full_context:
                    raise KeyError(f"Variable {var_name} not in context")

                value = str(full_context[var_name])
                hash_val = hashlib.blake2b(value.encode(), digest_size=32).hexdigest()

                # Create sharded path like "ab/cd" for depth=2, width=2
                parts = []
                for i in range(depth):
                    start = i * width
                    parts.append(hash_val[start:start + width])
                return "/".join(parts)

            raise ValueError(f"Unknown DSL expression: {expr}")

        # Replace all DSL expressions
        return re.sub(r"\{([^}]+)\}", replace_expr, template)

    def sim_path(self, **kwargs) -> str:
        """Render simulation result path."""
        return self.render_path(
            f"{self.root_template}/{self.sim_path_template}",
            kwargs
        )

    def agg_path(self, **kwargs) -> str:
        """Render aggregation result path."""
        return self.render_path(
            f"{self.root_template}/{self.agg_path_template}",
            kwargs
        )

    def job_path(self, **kwargs) -> str:
        """Render job-level path."""
        return self.render_path(
            f"{self.root_template}/{self.job_path_template}",
            kwargs
        )


# Pre-defined schema instances for different strategies
BUNDLE_INVALIDATION_SCHEMA = ProvenanceSchema(
    name="bundle",
    version=2,  # Bump version to avoid collisions with existing double-hashed data
    sim_path_template="sims/{bundle_digest[:12]}/{shard(param_id,2,2)}/params_{param_id[:8]}/seed_{seed}",
    agg_path_template="aggs/{bundle_digest[:12]}/target_{target}/agg_{aggregation_id}"
)

TOKEN_INVALIDATION_SCHEMA = ProvenanceSchema(
    name="token",
    version=2,  # Bump version to avoid collisions with existing double-hashed data
    sim_path_template="sims/{model_digest[:12]}/{shard(param_id,2,2)}/params_{param_id[:8]}/seed_{seed}",
    agg_path_template="aggs/{model_digest[:12]}/target_{target}/agg_{aggregation_id}"
)

DEFAULT_SCHEMA = TOKEN_INVALIDATION_SCHEMA
