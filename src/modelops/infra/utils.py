"""Infra utility helpers shared across provisioning components."""

from __future__ import annotations

from typing import Any, Iterable, Sequence


def safe_get_nested(obj: Any, *key_variants: Sequence[str]) -> Any | None:
    """Return the first nested value found when trying several key paths.

    Args:
        obj: Root dictionary (or None) to inspect.
        key_variants: Each entry is a sequence of keys that should exist in order.

    Returns:
        The first value found following one of the provided key paths, or None if
        no path could be resolved.
    """
    for keys in key_variants:
        current = obj
        for key in keys:
            if not isinstance(current, dict):
                break
            if key not in current:
                break
            current = current[key]
        else:
            return current
    return None


def kubelet_object_id(identity_profile: dict[str, Any] | None) -> str | None:
    """Extract the kubelet managed identity object ID from Azure payloads.

    Azure's ManagedCluster.identityProfile historically reached Pulumi with snake_case
    keys (``object_id``) but newer versions forward Azure's native camelCase fields
    (``objectId``). See https://github.com/InstituteforDiseaseModeling/modelops/issues/7
    for the regression triggered by this upstream inconsistency.

    Args:
        identity_profile: The identity profile payload (or None) returned by Azure.

    Returns:
        The kubelet identity's object id if available, otherwise None.
    """
    return safe_get_nested(
        identity_profile,
        ("kubeletidentity", "object_id"),
        ("kubeletidentity", "objectId"),
        ("kubeletIdentity", "object_id"),
        ("kubeletIdentity", "objectId"),
    )


__all__ = ["safe_get_nested", "kubelet_object_id"]
