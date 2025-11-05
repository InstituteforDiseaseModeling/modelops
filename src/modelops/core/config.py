"""ModelOps configuration management.

This module provides centralized configuration for ModelOps, stored in
~/.modelops/config.yaml. Configuration must be explicitly initialized
using 'mops config init' before use.
"""

from pathlib import Path

from pydantic import BaseModel, Field

from ..components.config_base import ConfigModel


class ConfigNotFoundError(Exception):
    """Raised when configuration file is not found."""

    pass


class PulumiConfig(BaseModel):
    """Pulumi-specific configuration."""

    backend_url: str | None = None
    """Optional backend URL override (e.g., azblob://container, file://path)."""

    organization: str = "organization"
    """Pulumi organization name (default: 'organization' for file backends)."""


class DefaultsConfig(BaseModel):
    """Default values for CLI commands."""

    environment: str = "dev"
    """Default environment for commands (dev, staging, prod)."""

    provider: str = "azure"
    """Default cloud provider."""

    username: str | None = None
    """Optional username override (defaults to system user)."""


class ModelOpsConfig(ConfigModel):
    """Main configuration model for ModelOps.

    This configuration is stored in ~/.modelops/config.yaml and provides
    defaults for CLI commands and Pulumi settings.
    """

    pulumi: PulumiConfig = Field(default_factory=PulumiConfig)
    """Pulumi-specific settings."""

    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    """Default values for CLI commands."""

    @classmethod
    def get_instance(cls) -> "ModelOpsConfig":
        """Get cached instance or load from file.

        This ensures we only read the config file once per session.
        First tries to load from unified config, then falls back to legacy.

        Returns:
            Cached ModelOpsConfig instance

        Raises:
            ConfigNotFoundError: If no configuration file exists
        """
        # Use hasattr to check if class variable exists
        if not hasattr(cls, "_cached_instance") or cls._cached_instance is None:
            from .paths import UNIFIED_CONFIG_FILE
            from .unified_config import UnifiedModelOpsConfig

            # Try unified config first
            if UNIFIED_CONFIG_FILE.exists():
                unified = UnifiedModelOpsConfig.load()
                # Convert to legacy format for compatibility
                cls._cached_instance = cls(
                    pulumi=PulumiConfig(
                        backend_url=unified.pulumi.backend_url,
                        organization=unified.pulumi.organization,
                    ),
                    defaults=DefaultsConfig(
                        environment=unified.settings.environment,
                        provider=unified.settings.provider,
                        username=unified.settings.username,
                    ),
                )
            else:
                # Fall back to legacy config
                cls._cached_instance = cls.load()
        return cls._cached_instance

    @classmethod
    def reset(cls):
        """Reset cached instance (useful for testing or forcing reload)."""
        if hasattr(cls, "_cached_instance"):
            cls._cached_instance = None

    @classmethod
    def load(cls) -> "ModelOpsConfig":
        """Load configuration from file.

        Returns:
            ModelOpsConfig instance loaded from ~/.modelops/config.yaml

        Raises:
            ConfigNotFoundError: If configuration file doesn't exist
        """
        config_path = cls.get_config_path()
        if not config_path.exists():
            raise ConfigNotFoundError(
                f"Configuration not found at {config_path}\n"
                "Run 'mops config init' to create configuration"
            )
        return cls.from_yaml(config_path)

    @classmethod
    def load_or_create(cls) -> "ModelOpsConfig":
        """Load configuration from file or create with defaults.

        This method is only used by 'mops config init' command.

        Returns:
            ModelOpsConfig instance loaded from file if exists,
            otherwise a new instance with default values.
        """
        config_path = cls.get_config_path()
        if config_path.exists():
            return cls.from_yaml(config_path)
        return cls()

    @staticmethod
    def get_config_path() -> Path:
        """Get the configuration file path.

        Returns:
            Path to ~/.modelops/config.yaml
        """
        from .paths import CONFIG_FILE

        return CONFIG_FILE

    def save(self) -> None:
        """Save configuration to ~/.modelops/config.yaml.

        Creates the .modelops directory if it doesn't exist.
        """
        config_path = self.get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        self.to_yaml(config_path)


def get_username() -> str:
    """Get username from config or system.

    Uses local system username (not Azure AD) for resource naming isolation.
    Azure AD username would require subprocess (az CLI) or heavy SDK dependencies.
    Users can override via config if local username doesn't match their identity.

    Tries unified config first, then legacy config, then system user.

    Returns:
        Username from config if set, otherwise the current system user.

    Raises:
        ConfigNotFoundError: If no configuration file exists
    """
    import getpass

    from .paths import UNIFIED_CONFIG_FILE

    # Try unified config first
    if UNIFIED_CONFIG_FILE.exists():
        from .unified_config import UnifiedModelOpsConfig

        unified = UnifiedModelOpsConfig.load()
        return unified.settings.username

    # Fall back to legacy config
    try:
        config = ModelOpsConfig.get_instance()
        if config.defaults.username:
            return config.defaults.username
    except ConfigNotFoundError:
        pass

    return getpass.getuser()
