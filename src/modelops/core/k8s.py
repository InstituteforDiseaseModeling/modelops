"""Kubernetes naming utilities and validators.

This module provides functions to ensure Kubernetes resource names
comply with DNS-1123 and other naming requirements.
"""

import re


def dns_1123_subdomain(name: str, fallback: str = "default", max_length: int = 63) -> str:
    """Sanitize a name to be DNS-1123 subdomain compliant.

    DNS-1123 subdomain requirements:
    - Must consist of lower case alphanumeric characters, '-' or '.'
    - Must start with an alphanumeric character
    - Must end with an alphanumeric character
    - Maximum length of 63 characters (configurable)

    Args:
        name: The name to sanitize
        fallback: Default value if sanitization results in empty string
        max_length: Maximum allowed length (default 63 for labels)

    Returns:
        Sanitized name that is DNS-1123 compliant

    Example:
        >>> dns_1123_subdomain("My-App_Name!")
        "my-app-name"
        >>> dns_1123_subdomain("123_start")
        "123-start"
        >>> dns_1123_subdomain("!@#$%", fallback="app")
        "app"
    """
    if not name:
        return fallback

    # Convert to lowercase and replace non-alphanumeric with hyphens
    sanitized = re.sub(r"[^a-z0-9-]", "-", name.lower())

    # Remove leading/trailing hyphens
    sanitized = sanitized.strip("-")

    # Collapse multiple hyphens
    sanitized = re.sub(r"-+", "-", sanitized)

    # Truncate to max length
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip("-")

    # Return fallback if empty after sanitization
    return sanitized or fallback


def dns_1123_label(name: str, fallback: str = "default", max_length: int = 63) -> str:
    """Sanitize a name to be DNS-1123 label compliant.

    DNS-1123 label requirements (stricter than subdomain):
    - Must consist of lower case alphanumeric characters or '-'
    - Must start with an alphanumeric character
    - Must end with an alphanumeric character
    - Maximum length of 63 characters

    Args:
        name: The name to sanitize
        fallback: Default value if sanitization results in empty string
        max_length: Maximum allowed length (always 63 for labels)

    Returns:
        Sanitized name that is DNS-1123 label compliant
    """
    # Labels don't allow dots, so we use subdomain and remove dots
    sanitized = dns_1123_subdomain(name, fallback, max_length)
    sanitized = sanitized.replace(".", "-")

    # Collapse multiple hyphens again after dot replacement
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")

    return sanitized or fallback


def validate_namespace_name(name: str) -> bool:
    """Check if a name is valid for a Kubernetes namespace.

    Args:
        name: The namespace name to validate

    Returns:
        True if valid, False otherwise
    """
    # Namespace names must be DNS-1123 labels
    if not name or len(name) > 63:
        return False

    pattern = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
    return bool(re.match(pattern, name))


def sanitize_label_value(value: str, max_length: int = 63) -> str:
    """Sanitize a Kubernetes label value.

    Label value requirements:
    - Can be empty
    - Must be 63 characters or less
    - Must begin and end with alphanumeric ([a-z0-9A-Z])
    - Can contain dashes (-), underscores (_), dots (.), and alphanumerics

    Args:
        value: The label value to sanitize
        max_length: Maximum allowed length (default 63)

    Returns:
        Sanitized label value
    """
    if not value:
        return ""

    # Keep alphanumeric, dash, underscore, dot
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "", value)

    # Ensure starts and ends with alphanumeric
    sanitized = re.sub(r"^[^a-zA-Z0-9]+", "", sanitized)
    sanitized = re.sub(r"[^a-zA-Z0-9]+$", "", sanitized)

    # Truncate to max length
    if len(sanitized) > max_length:
        # Truncate and ensure it still ends with alphanumeric
        sanitized = sanitized[:max_length]
        sanitized = re.sub(r"[^a-zA-Z0-9]+$", "", sanitized)

    return sanitized
