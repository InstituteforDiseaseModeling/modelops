"""Azure Blob Storage based content-addressable storage adapter."""

import hashlib
import logging
import os
from typing import Optional

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError, ResourceExistsError
from modelops_contracts.ports import CAS

logger = logging.getLogger(__name__)


class AzureCAS(CAS):
    """Azure Blob Storage based CAS implementation."""
    
    def __init__(
        self,
        container: str,
        prefix: str = "cas",
        storage_account: Optional[str] = None,
        connection_string: Optional[str] = None
    ):
        """Initialize Azure CAS.
        
        Args:
            container: Container name
            prefix: Blob prefix for CAS objects
            storage_account: Storage account name (uses env if not specified)
            connection_string: Connection string (uses env if not specified)
        """
        self.container = container
        self.prefix = prefix.rstrip("/")
        
        # Initialize client
        if connection_string:
            self.blob_service = BlobServiceClient.from_connection_string(connection_string)
        elif storage_account:
            # Use default credential (managed identity, env vars, etc)
            account_url = f"https://{storage_account}.blob.core.windows.net"
            from azure.identity import DefaultAzureCredential
            self.blob_service = BlobServiceClient(
                account_url=account_url,
                credential=DefaultAzureCredential()
            )
        else:
            # Try to get from environment
            conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
            if conn_str:
                self.blob_service = BlobServiceClient.from_connection_string(conn_str)
            else:
                raise ValueError(
                    "Must provide either storage_account, connection_string, "
                    "or set AZURE_STORAGE_CONNECTION_STRING environment variable"
                )
        
        # Get container client
        self.container_client = self.blob_service.get_container_client(container)
        
        # Verify container exists
        try:
            self.container_client.get_container_properties()
        except ResourceNotFoundError:
            raise ValueError(f"Container {container} does not exist")
    
    def put(self, data: bytes, checksum_hex: str) -> str:
        """Store data in Azure Blob CAS.
        
        Args:
            data: Raw bytes to store
            checksum_hex: Expected SHA256 hex digest for verification
            
        Returns:
            Reference string for retrieving the data
            
        Raises:
            ValueError: If data doesn't match expected checksum
        """
        # Verify checksum
        actual_checksum = hashlib.sha256(data).hexdigest()
        if actual_checksum != checksum_hex:
            raise ValueError(
                f"Checksum mismatch: expected {checksum_hex}, got {actual_checksum}"
            )
        
        # Build blob name
        blob_name = f"{self.prefix}/{checksum_hex[:2]}/{checksum_hex}"
        blob_client = self.container_client.get_blob_client(blob_name)
        
        # Check if already exists (CAS is immutable)
        try:
            props = blob_client.get_blob_properties()
            logger.debug(f"Blob already exists: {blob_name}")
            return checksum_hex
        except ResourceNotFoundError:
            pass
        
        # Upload to Azure
        try:
            blob_client.upload_blob(
                data,
                overwrite=False,  # CAS is immutable
                metadata={"sha256": checksum_hex}
            )
            logger.info(f"Stored {len(data)} bytes to blob {blob_name}")
            return checksum_hex
        except ResourceExistsError:
            # Someone else uploaded it concurrently, that's fine
            return checksum_hex
        except Exception as e:
            raise RuntimeError(f"Failed to store to Azure Blob: {e}")
    
    def get(self, ref: str) -> bytes:
        """Retrieve data from Azure Blob CAS.
        
        Args:
            ref: Reference returned from put() (the checksum)
            
        Returns:
            Raw bytes data
            
        Raises:
            KeyError: If reference not found
        """
        # Build blob name
        blob_name = f"{self.prefix}/{ref[:2]}/{ref}"
        blob_client = self.container_client.get_blob_client(blob_name)
        
        try:
            # Download blob
            downloader = blob_client.download_blob()
            data = downloader.readall()
            
            # Verify integrity
            actual_checksum = hashlib.sha256(data).hexdigest()
            if actual_checksum != ref:
                raise ValueError(
                    f"Data corruption detected: expected {ref}, got {actual_checksum}"
                )
            
            return data
        except ResourceNotFoundError:
            raise KeyError(f"CAS object not found: {ref}")
        except Exception as e:
            raise RuntimeError(f"Failed to retrieve from Azure Blob: {e}")
    
    def exists(self, ref: str) -> bool:
        """Check if a reference exists in Azure Blob CAS.
        
        Args:
            ref: Reference to check
            
        Returns:
            True if exists, False otherwise
        """
        blob_name = f"{self.prefix}/{ref[:2]}/{ref}"
        blob_client = self.container_client.get_blob_client(blob_name)
        
        try:
            blob_client.get_blob_properties()
            return True
        except ResourceNotFoundError:
            return False