"""Azure Blob Storage backend implementation."""

import logging
import os

from .cloud import CloudBlobBackend

logger = logging.getLogger(__name__)


class AzureBlobBackend(CloudBlobBackend):
    """Azure blob storage backend.

    Uses connection info from BlobStorage Pulumi outputs via environment
    variables or Kubernetes secrets.

    Environment setup:
        # After provisioning with Pulumi
        mops storage up examples/storage.yaml
        mops storage connection-string > ~/.modelops/storage.env
        source ~/.modelops/storage.env
    """

    def __init__(self, container: str = "cache", connection_string: str | None = None):
        """Initialize Azure blob backend.

        Args:
            container: Container name (default: "cache")
            connection_string: Optional explicit connection string.
                              If not provided, uses AZURE_STORAGE_CONNECTION_STRING env var.
        """
        self.connection_string = connection_string
        super().__init__(container=container)

    def _detect_provider(self) -> str:
        """Detect Azure from environment."""
        conn_str = self.connection_string or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")

        if conn_str:
            return "azure"

        raise ValueError(
            "Azure storage connection not found. Either:\n"
            "1. Pass connection_string parameter\n"
            "2. Set AZURE_STORAGE_CONNECTION_STRING environment variable\n"
            "3. Run: mops storage connection-string > ~/.modelops/storage.env && source ~/.modelops/storage.env"
        )

    def _initialize_client(self) -> None:
        """Initialize Azure blob client."""
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise ImportError(
                "azure-storage-blob package not installed. "
                "Install with: pip install azure-storage-blob"
            )

        # Get connection string
        conn_str = self.connection_string or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")

        try:
            self.client = BlobServiceClient.from_connection_string(conn_str)
            self.ensure_container()
        except Exception as e:
            raise RuntimeError(f"Failed to initialize Azure blob client: {e}")

    def ensure_container(self) -> None:
        """Ensure container exists."""
        try:
            container_client = self.client.get_container_client(self.container)
            if not container_client.exists():
                logger.info(f"Creating container: {self.container}")
                self.client.create_container(self.container)
            else:
                logger.debug(f"Container exists: {self.container}")
        except Exception as e:
            # Container might already exist or we might not have permissions
            logger.debug(f"Container check/create for {self.container}: {e}")

    # Azure-specific implementations

    def _azure_exists(self, key: str) -> bool:
        """Azure implementation of exists."""
        try:
            blob_client = self.client.get_blob_client(self.container, key)
            return blob_client.exists()
        except Exception as e:
            logger.error(f"Error checking existence of {key}: {e}")
            return False

    def _azure_load(self, key: str) -> bytes:
        """Azure implementation of load."""
        try:
            blob_client = self.client.get_blob_client(self.container, key)
            return blob_client.download_blob().readall()
        except Exception as e:
            raise KeyError(f"Failed to load key '{key}': {e}")

    def _azure_save(self, key: str, data: bytes) -> None:
        """Azure implementation of save."""
        try:
            blob_client = self.client.get_blob_client(self.container, key)
            blob_client.upload_blob(data, overwrite=True)
            logger.debug(f"Saved {len(data)} bytes to {key}")
        except Exception as e:
            raise RuntimeError(f"Failed to save key '{key}': {e}")

    def _azure_delete(self, key: str) -> None:
        """Azure implementation of delete."""
        try:
            blob_client = self.client.get_blob_client(self.container, key)
            blob_client.delete_blob()
            logger.debug(f"Deleted key: {key}")
        except Exception as e:
            raise KeyError(f"Failed to delete key '{key}': {e}")

    def _azure_list_keys(self, prefix: str) -> list[str]:
        """Azure implementation of list_keys."""
        try:
            container_client = self.client.get_container_client(self.container)
            blobs = container_client.list_blobs(name_starts_with=prefix)
            keys = [blob.name for blob in blobs]
            logger.debug(f"Listed {len(keys)} keys with prefix: {prefix}")
            return keys
        except Exception as e:
            logger.error(f"Failed to list keys with prefix '{prefix}': {e}")
            return []
