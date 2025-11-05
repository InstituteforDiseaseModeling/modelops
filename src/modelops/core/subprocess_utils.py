"""Subprocess utilities for consistent environment handling."""

import os
import subprocess
from pathlib import Path


def get_pulumi_env() -> dict[str, str]:
    """Get environment with Pulumi passphrase properly configured.

    Returns:
        Environment dict with PULUMI_CONFIG_PASSPHRASE_FILE set
    """
    from . import automation

    # Ensure passphrase is configured
    automation._ensure_passphrase()

    # Return copy of environment with passphrase file set
    env = dict(os.environ)

    # Explicitly ensure passphrase file is set (redundant but safe)
    passphrase_file = Path.home() / ".modelops" / "secrets" / "pulumi-passphrase"
    if passphrase_file.exists():
        env["PULUMI_CONFIG_PASSPHRASE_FILE"] = str(passphrase_file)

    # Remove direct passphrase if present
    env.pop("PULUMI_CONFIG_PASSPHRASE", None)

    return env


def run_pulumi_command(
    cmd: list[str],
    cwd: str | Path | None = None,
    capture_output: bool = True,
    text: bool = True,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a Pulumi command with proper environment setup.

    Args:
        cmd: Command list to run
        cwd: Working directory
        capture_output: Whether to capture output
        text: Whether to return text output
        **kwargs: Additional arguments for subprocess.run

    Returns:
        CompletedProcess result
    """
    # Get environment with passphrase
    env = get_pulumi_env()

    # Merge with any env provided in kwargs
    if "env" in kwargs:
        env.update(kwargs.pop("env"))

    return subprocess.run(cmd, cwd=cwd, capture_output=capture_output, text=text, env=env, **kwargs)
