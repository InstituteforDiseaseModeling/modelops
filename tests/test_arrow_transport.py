"""Tests for clean Arrow IPC data transport handling."""

import base64
import io
import pytest
import polars as pl
from modelops.worker.arrow_transport import decode_arrow_data, extract_arrow_from_artifact


def create_test_arrow_bytes() -> bytes:
    """Create valid Arrow IPC bytes for testing."""
    df = pl.DataFrame({"col": [1, 2, 3]})
    buf = io.BytesIO()
    df.write_ipc(buf)
    return buf.getvalue()


class TestDecodeArrowData:
    """Test the decode_arrow_data function."""

    def test_raw_bytes_passthrough(self):
        """Test that raw bytes are returned unchanged."""
        arrow_bytes = create_test_arrow_bytes()
        result = decode_arrow_data(arrow_bytes)
        assert result == arrow_bytes

    def test_base64_with_hint(self):
        """Test base64 decoding with explicit hint."""
        arrow_bytes = create_test_arrow_bytes()
        base64_str = base64.b64encode(arrow_bytes).decode("ascii")
        result = decode_arrow_data(base64_str, encoding_hint="base64")
        assert result == arrow_bytes

    def test_base64_auto_detect(self):
        """Test base64 auto-detection when it starts with ARROW."""
        arrow_bytes = create_test_arrow_bytes()
        base64_str = base64.b64encode(arrow_bytes).decode("ascii")
        result = decode_arrow_data(base64_str)
        assert result == arrow_bytes

    def test_invalid_string_raises(self):
        """Test that invalid string data raises ValueError."""
        with pytest.raises(ValueError, match="Cannot decode Arrow data"):
            decode_arrow_data("not base64 data")

    def test_wrong_type_raises(self):
        """Test that wrong type raises TypeError."""
        with pytest.raises(TypeError, match="Arrow data must be bytes or str"):
            decode_arrow_data(123)


class TestExtractArrowFromArtifact:
    """Test the extract_arrow_from_artifact function."""

    def test_direct_bytes(self):
        """Test extraction from direct bytes."""
        arrow_bytes = create_test_arrow_bytes()
        result = extract_arrow_from_artifact(arrow_bytes)
        assert result == arrow_bytes

    def test_dict_with_inline_base64(self):
        """Test extraction from dict with base64 inline field."""
        arrow_bytes = create_test_arrow_bytes()
        base64_str = base64.b64encode(arrow_bytes).decode("ascii")
        artifact = {"inline": base64_str, "size": len(arrow_bytes)}
        result = extract_arrow_from_artifact(artifact)
        assert result == arrow_bytes

    def test_dict_with_inline_bytes(self):
        """Test extraction from dict with bytes inline field."""
        arrow_bytes = create_test_arrow_bytes()
        artifact = {"inline": arrow_bytes, "size": len(arrow_bytes)}
        result = extract_arrow_from_artifact(artifact)
        assert result == arrow_bytes

    def test_dict_with_data_field(self):
        """Test extraction from dict with data field instead of inline."""
        arrow_bytes = create_test_arrow_bytes()
        base64_str = base64.b64encode(arrow_bytes).decode("ascii")
        artifact = {"data": base64_str}
        result = extract_arrow_from_artifact(artifact)
        assert result == arrow_bytes

    def test_dict_missing_fields_raises(self):
        """Test that dict without inline or data raises ValueError."""
        artifact = {"size": 100, "checksum": "abc"}
        with pytest.raises(ValueError, match="missing 'inline' or 'data'"):
            extract_arrow_from_artifact(artifact)

    def test_wrong_type_raises(self):
        """Test that wrong type raises TypeError."""
        with pytest.raises(TypeError, match="Artifact must be dict or bytes"):
            extract_arrow_from_artifact("string data")


class TestIntegrationWithPolars:
    """Test that extracted data works with Polars."""

    def test_round_trip_through_base64(self):
        """Test full round trip through base64 encoding."""
        # Create original DataFrame
        df_original = pl.DataFrame({"id": [1, 2, 3], "value": [10.5, 20.3, 30.1]})

        # Convert to Arrow bytes
        buf = io.BytesIO()
        df_original.write_ipc(buf)
        arrow_bytes = buf.getvalue()

        # Simulate serialization for JSON-RPC
        base64_str = base64.b64encode(arrow_bytes).decode("ascii")
        artifact = {"inline": base64_str}

        # Extract and load back
        extracted_bytes = extract_arrow_from_artifact(artifact)
        df_loaded = pl.read_ipc(io.BytesIO(extracted_bytes))

        # Verify data is identical
        assert df_loaded.equals(df_original)
        assert df_loaded.shape == df_original.shape
        assert list(df_loaded.columns) == list(df_original.columns)
