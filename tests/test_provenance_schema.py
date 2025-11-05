#!/usr/bin/env python
"""Test ProvenanceSchema DSL and path generation."""

import pytest
import hashlib
from pathlib import Path
from modelops.services.provenance_schema import (
    ProvenanceSchema,
    BUNDLE_INVALIDATION_SCHEMA,
    TOKEN_INVALIDATION_SCHEMA,
    DEFAULT_SCHEMA,
)


def make_test_digest(value: str) -> str:
    """Create a test digest for a value."""
    return hashlib.blake2b(value.encode(), digest_size=32).hexdigest()


class TestProvenanceSchemaCore:
    """Core DSL template tests."""

    def test_simple_template(self):
        """Test basic template without sharding."""
        schema = ProvenanceSchema(
            sim_path_template="bundle/{bundle_ref}/param_{param_id}/seed_{seed}",
            agg_path_template="agg/{aggregation_id}",
        )

        path = schema.sim_path(
            bundle_ref="sha256:" + "a" * 64, param_id=make_test_digest("params1"), seed=42
        )

        assert "bundle/sha256:" + "a" * 64 in path
        assert "param_" in path
        assert "seed_42" in path

    def test_hash_function(self):
        """Test DSL hash() function."""
        schema = ProvenanceSchema(
            sim_path_template="data/{hash(bundle_digest)[:8]}/full",
            agg_path_template="agg/{aggregation_id}",
        )

        bundle_digest = "sha256:abcdef123456"
        path = schema.sim_path(bundle_digest=bundle_digest)

        # Hash should be taken and sliced to 8 chars
        expected_hash = hashlib.blake2b(bundle_digest.encode(), digest_size=32).hexdigest()[:8]
        assert expected_hash in path

    def test_shard_function(self):
        """Test DSL shard() function for directory sharding."""
        schema = ProvenanceSchema(
            sim_path_template="data/{shard(param_id,2,2)}/params_{param_id[:8]}",
            agg_path_template="agg/{aggregation_id}",
        )

        param_id = make_test_digest("test_params")
        path = schema.sim_path(param_id=param_id)

        # Shard creates hash of the value, then shards the hash
        # So we need to hash param_id to get the sharding parts
        param_hash = hashlib.blake2b(param_id.encode(), digest_size=32).hexdigest()

        parts = path.split("/")
        # Should have structure like: default/v1/data/XX/XX/params_XXXXXXXX
        assert param_hash[0:2] in parts  # First shard level
        assert param_hash[2:4] in parts  # Second shard level
        assert f"params_{param_id[:8]}" in path

    def test_direct_slicing(self):
        """Test direct variable slicing with [:n] syntax."""
        schema = ProvenanceSchema(
            sim_path_template="short_{param_id[:6]}/full_{param_id}",
            agg_path_template="agg/{aggregation_id}",
        )

        param_id = make_test_digest("parameters")
        path = schema.sim_path(param_id=param_id)

        assert f"short_{param_id[:6]}" in path
        assert f"full_{param_id}" in path

    def test_nested_template(self):
        """Test complex nested template."""
        schema = ProvenanceSchema(
            name="complex",
            version=2,
            sim_path_template="sims/{hash(bundle_digest)[:12]}/{shard(param_id,3,2)}/s_{seed}",
            agg_path_template="aggs/{hash(target)[:8]}/a_{aggregation_id[:12]}",
        )

        bundle_digest = "sha256:testbundle"
        param_id = make_test_digest("params")

        path = schema.sim_path(bundle_digest=bundle_digest, param_id=param_id, seed=100)

        # Should include schema name and version
        assert "complex/v2" in path
        assert "sims/" in path
        assert "s_100" in path


class TestInvalidationSchemas:
    """Test the predefined invalidation schemas."""

    def test_bundle_invalidation_schema(self):
        """Test BUNDLE_INVALIDATION_SCHEMA structure."""
        schema = BUNDLE_INVALIDATION_SCHEMA

        bundle_digest = make_test_digest("bundle123")
        param_id = make_test_digest("params456")

        path = schema.sim_path(bundle_digest=bundle_digest, param_id=param_id, seed=42)

        # Should have bundle/v1 prefix
        assert "bundle/v1/sims" in path
        # Should use bundle digest directly (not double-hashed)
        assert bundle_digest[:12] in path
        # Should have sharded param_id (shard function hashes first)
        param_hash = hashlib.blake2b(param_id.encode(), digest_size=32).hexdigest()
        assert param_hash[0:2] in path.split("/")
        # Should have seed
        assert "seed_42" in path

    def test_token_invalidation_schema(self):
        """Test TOKEN_INVALIDATION_SCHEMA structure."""
        schema = TOKEN_INVALIDATION_SCHEMA

        model_digest = make_test_digest("model789")
        param_id = make_test_digest("params456")

        path = schema.sim_path(model_digest=model_digest, param_id=param_id, seed=42)

        # Should have token/v1 prefix
        assert "token/v1/sims" in path
        # Should use model digest directly (not double-hashed)
        assert model_digest[:12] in path
        # Should have seed
        assert "seed_42" in path

    def test_default_schema_is_token(self):
        """Verify DEFAULT_SCHEMA uses token invalidation."""
        assert DEFAULT_SCHEMA is TOKEN_INVALIDATION_SCHEMA
        assert DEFAULT_SCHEMA.name == "token"

    def test_schemas_produce_different_paths(self):
        """Verify different schemas produce different paths for same inputs."""
        param_id = make_test_digest("params")
        seed = 42

        # Bundle schema uses bundle_digest
        bundle_path = BUNDLE_INVALIDATION_SCHEMA.sim_path(
            bundle_digest="digest1", param_id=param_id, seed=seed
        )

        # Token schema uses model_digest
        token_path = TOKEN_INVALIDATION_SCHEMA.sim_path(
            model_digest="digest1",  # Same value but different param name
            param_id=param_id,
            seed=seed,
        )

        # Paths should differ due to different schema names
        assert bundle_path != token_path
        assert "bundle/v1" in bundle_path
        assert "token/v1" in token_path


class TestAggregationPaths:
    """Test aggregation task path generation."""

    def test_aggregation_path_basic(self):
        """Test basic aggregation path generation."""
        schema = ProvenanceSchema(
            sim_path_template="sims/{param_id}",
            agg_path_template="aggs/{target}/agg_{aggregation_id}",
        )

        path = schema.agg_path(
            target="covid_deaths", aggregation_id=make_test_digest("agg123")[:16]
        )

        assert "aggs/covid_deaths" in path
        assert "agg_" in path

    def test_aggregation_with_hash(self):
        """Test aggregation path with hash function."""
        schema = ProvenanceSchema(
            sim_path_template="sims/{param_id}",
            agg_path_template="aggs/{hash(bundle_digest)[:8]}/{aggregation_id[:12]}",
        )

        bundle_digest = "sha256:bundle456"
        agg_id = make_test_digest("aggregation")

        path = schema.agg_path(bundle_digest=bundle_digest, aggregation_id=agg_id)

        bundle_hash = hashlib.blake2b(bundle_digest.encode(), digest_size=32).hexdigest()[:8]
        assert bundle_hash in path
        assert agg_id[:12] in path


class TestDSLValidation:
    """Test DSL syntax validation."""

    def test_valid_templates(self):
        """Test that valid templates are accepted."""
        valid_templates = [
            "{var}",
            "{var[:8]}",
            "{hash(var)[:12]}",
            "{shard(var,2,2)}",
            "prefix/{var}/suffix",
            "complex/{hash(a)[:8]}/{shard(b,3,2)}/{c[:4]}",
        ]

        for template in valid_templates:
            schema = ProvenanceSchema(sim_path_template=template)
            assert schema.sim_path_template == template

    def test_invalid_templates(self):
        """Test that invalid templates are rejected."""
        invalid_templates = [
            "{invalid-var}",  # Hyphens not allowed
            "{Hash(var)}",  # Capital letters not allowed
            "{shard(var,2)}",  # Wrong number of args
            "{{double}}",  # Double braces
        ]

        for template in invalid_templates:
            with pytest.raises(ValueError):
                ProvenanceSchema(sim_path_template=template)

    def test_unbalanced_braces(self):
        """Test that unbalanced braces are rejected."""
        with pytest.raises(ValueError, match="Unbalanced braces"):
            ProvenanceSchema(sim_path_template="test/{var")

        with pytest.raises(ValueError, match="Unbalanced braces"):
            ProvenanceSchema(sim_path_template="test/var}")


class TestSchemaVersioning:
    """Test schema versioning support."""

    def test_version_in_path(self):
        """Test that version appears in generated paths."""
        schema_v1 = ProvenanceSchema(name="test", version=1, sim_path_template="data/{param_id}")

        schema_v2 = ProvenanceSchema(name="test", version=2, sim_path_template="data/{param_id}")

        path_v1 = schema_v1.sim_path(param_id="abc")
        path_v2 = schema_v2.sim_path(param_id="abc")

        assert "test/v1/data" in path_v1
        assert "test/v2/data" in path_v2
        assert path_v1 != path_v2

    def test_custom_root_template(self):
        """Test custom root template."""
        schema = ProvenanceSchema(
            name="custom",
            root_template="storage/{version}/{schema_name}",
            sim_path_template="sims/{param_id}",
        )

        path = schema.sim_path(param_id="test")
        assert "storage/1/custom/sims" in path


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_missing_variable(self):
        """Test handling of missing variables in context."""
        schema = ProvenanceSchema(sim_path_template="data/{param_id}/extra_{extra_var}")

        # Should raise ValueError (not KeyError) for unknown DSL expression
        with pytest.raises(ValueError, match="Unknown DSL expression"):
            schema.sim_path(param_id="test")  # Missing extra_var

    def test_empty_variable(self):
        """Test handling of empty string variables."""
        schema = ProvenanceSchema(sim_path_template="data/{param_id}/seed_{seed}")

        path = schema.sim_path(param_id="", seed=0)
        assert "data//seed_0" in path  # Empty param_id creates double slash

    def test_unicode_handling(self):
        """Test Unicode string handling in variables."""
        schema = ProvenanceSchema(sim_path_template="data/{hash(text)[:8]}")

        # Unicode text should be hashed properly
        path = schema.sim_path(text="测试数据")
        # Should create a valid hash
        assert "data/" in path
        assert len(path.split("/")[-1]) == 8  # 8-char hash

    def test_numeric_variables(self):
        """Test numeric variables are converted to strings."""
        schema = ProvenanceSchema(sim_path_template="run_{run_id}/step_{step}")

        path = schema.sim_path(run_id=123, step=456)
        assert "run_123/step_456" in path


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
