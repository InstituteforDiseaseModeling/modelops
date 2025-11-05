"""Azure Blob Storage implementation of VersionedStore.

Uses ETags for optimistic concurrency control, providing lock-free
concurrent updates with automatic retry on conflicts.
"""

import logging

from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.storage.blob import BlobServiceClient

from .versioned import VersionToken

logger = logging.getLogger(__name__)


class AzureVersionedStore:
    """Azure blob storage with ETags for Compare-And-Swap.

    ETags are HTTP standard (RFC 7232) entity tags that change
    whenever a blob is modified. Azure automatically maintains them,
    making them perfect for optimistic concurrency control.
    """

    def __init__(self, connection_string: str, container: str = "job-registry"):
        """Initialize Azure blob store.

        Args:
            connection_string: Azure Storage connection string
            container: Blob container name (created if doesn't exist)
        """
        self.client = BlobServiceClient.from_connection_string(connection_string)
        self.container = container
        self._ensure_container()

    def _ensure_container(self) -> None:
        """Ensure the container exists."""
        try:
            container_client = self.client.get_container_client(self.container)
            if not container_client.exists():
                logger.info(f"Creating container: {self.container}")
                container_client.create_container()
        except ResourceExistsError:
            # Container already exists (race condition)
            pass
        except Exception as e:
            logger.warning(f"Container check for {self.container}: {e}")

    def get(self, key: str) -> tuple[bytes, VersionToken] | None:
        """Get current value and ETag version."""
        try:
            blob_client = self.client.get_blob_client(self.container, key)

            # Get properties first (includes ETag)
            props = blob_client.get_blob_properties()

            # Then download content
            content = blob_client.download_blob().readall()

            return (content, VersionToken(props.etag))

        except ResourceNotFoundError:
            return None
        except Exception as e:
            logger.error(f"Failed to get {key}: {e}")
            raise

    def put(self, key: str, value: bytes, version: VersionToken) -> bool:
        """Update blob if ETag matches (CAS operation).

        Uses if_match condition to ensure atomic update only if
        the blob hasn't changed since we read it.
        """
        try:
            blob_client = self.client.get_blob_client(self.container, key)

            blob_client.upload_blob(
                value,
                overwrite=True,
                if_match=version.value,  # ETag for conditional update
                content_type="application/json",
            )

            logger.debug(f"Updated {key} with CAS (etag: {version.value})")
            return True

        except ResourceModifiedError:
            # Expected when concurrent update happens
            logger.debug(f"CAS conflict on {key} (etag: {version.value})")
            return False
        except ResourceNotFoundError:
            # Key was deleted between get and put
            logger.debug(f"Key {key} not found for update")
            return False
        except Exception as e:
            logger.error(f"Failed to put {key}: {e}")
            raise

    def create_if_absent(self, key: str, value: bytes) -> bool:
        """Create blob only if it doesn't exist.

        This is atomic - either creates or fails if exists.
        """
        try:
            blob_client = self.client.get_blob_client(self.container, key)

            # overwrite=False ensures it fails if exists
            blob_client.upload_blob(value, overwrite=False, content_type="application/json")

            logger.debug(f"Created {key}")
            return True

        except ResourceExistsError:
            # Expected when key already exists
            logger.debug(f"Key {key} already exists")
            return False
        except Exception as e:
            logger.error(f"Failed to create {key}: {e}")
            raise

    def list_keys(self, prefix: str = "") -> list[str]:
        """List all blob names with given prefix."""
        try:
            container_client = self.client.get_container_client(self.container)

            if prefix:
                blobs = container_client.list_blobs(name_starts_with=prefix)
            else:
                blobs = container_client.list_blobs()

            keys = [blob.name for blob in blobs]
            logger.debug(f"Listed {len(keys)} keys with prefix '{prefix}'")
            return keys

        except Exception as e:
            logger.error(f"Failed to list keys with prefix '{prefix}': {e}")
            return []

    def delete(self, key: str) -> bool:
        """Delete a blob."""
        try:
            blob_client = self.client.get_blob_client(self.container, key)
            blob_client.delete_blob()
            logger.debug(f"Deleted {key}")
            return True

        except ResourceNotFoundError:
            logger.debug(f"Key {key} not found for deletion")
            return False
        except Exception as e:
            logger.error(f"Failed to delete {key}: {e}")
            raise
