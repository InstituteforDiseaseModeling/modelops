"""Tests for infra utility helpers."""

from modelops.infra.utils import kubelet_object_id, safe_get_nested


def test_safe_get_nested_first_match():
    data = {"a": {"b": {"c": 5}}, "x": 1}
    result = safe_get_nested(data, ("a", "b", "c"), ("x",))
    assert result == 5


def test_safe_get_nested_no_match_returns_none():
    data = {"a": {"b": {}}}
    assert safe_get_nested(data, ("a", "missing")) is None


def test_kubelet_object_id_supports_multiple_casings():
    profile = {"kubeletidentity": {"objectId": "abc123"}}
    assert kubelet_object_id(profile) == "abc123"

    snake_profile = {"kubeletIdentity": {"object_id": "def456"}}
    assert kubelet_object_id(snake_profile) == "def456"

    assert kubelet_object_id(None) is None
