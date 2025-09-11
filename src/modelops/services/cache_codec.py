"""ZIP-based codec for SimReturn caching.

This module provides deterministic, safe, and portable serialization
of simulation results using ZIP containers with JSON manifests.

Key features:
- Deterministic: Identical content produces identical bytes
- Safe: No code execution risk (unlike pickle)
- Portable: Any language can read ZIP files
- Compressed: Reduces storage with ZIP_DEFLATED or respects Arrow compression
- Validated: SHA256 hashes ensure integrity

TODO: needed? let's come back to this.
"""

import io
import json
import zipfile
import hashlib
import platform
import re
from datetime import datetime
from typing import Dict, Optional, Set, Tuple, Callable
from zipfile import ZipFile, ZipInfo, ZIP_STORED, ZIP_DEFLATED

from modelops_contracts import SimReturn, make_param_id

def sanitize_table_name(name: str) -> str:
    """Sanitize table name for safe storage.
    
    Prevents path traversal and filesystem escapes.
    
    Args:
        name: Original table name
        
    Returns:
        Safe filename (no slashes, dots, or special chars)
    """
    # Replace dangerous chars, prevent path traversal
    name = re.sub(r'[^\w\-_.]', '_', name)
    name = name.replace('..', '_')
    return name[:100]  # Reasonable length limit


def encode_zip(
    simret: SimReturn,
    params: Optional[dict] = None,
    seed: Optional[int] = None,
    fn_ref: Optional[str] = None,
    compression: str = "deflate"  # "stored" | "deflate"
) -> bytes:
    """Encode SimReturn as deterministic ZIP with manifest.
    
    Creates a ZIP archive with:
    - manifest.json: Metadata and table registry
    - tables/*.arrow: Arrow IPC data files
    
    The ZIP is deterministic: identical inputs produce identical bytes.
    
    Args:
        simret: Dict of table_name -> Arrow IPC bytes
        params: Original parameters (for provenance)
        seed: Random seed used
        fn_ref: Function reference (e.g., "module:function")
        compression: "stored" (no compression) or "deflate" (ZIP compression)
                    Use "stored" if Arrow data is already compressed
    
    Returns:
        Deterministic ZIP bytes
    """
    buf = io.BytesIO()
    compress_type = ZIP_STORED if compression == "stored" else ZIP_DEFLATED
    
    # Sort for determinism
    names = sorted(simret.keys())
    
    with ZipFile(buf, "w", compression=compress_type) as z:
        # Build manifest
        manifest = {
            "version": 2,
            "modelops_version": "0.1.0",
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "provenance": {
                "params": params,
                "seed": seed,
                "fn_ref": fn_ref,
                "param_id": make_param_id(params) if params else None
            },
            "tables": {}
        }
        
        # Try to get library versions if available
        try:
            import polars as pl
            manifest["runtime"]["polars"] = pl.__version__
        except ImportError:
            pass
        
        try:
            import pyarrow as pa
            manifest["runtime"]["pyarrow"] = pa.__version__
        except ImportError:
            pass
        
        # Write tables with deterministic timestamps
        table_hashes = []
        for name in names:
            safe_name = sanitize_table_name(name)
            payload = simret[name]
            
            # Create ZipInfo with fixed timestamp for determinism
            zi = ZipInfo(filename=f"tables/{safe_name}.arrow")
            zi.date_time = (1980, 1, 1, 0, 0, 0)  # Fixed for byte-identical ZIPs
            zi.compress_type = compress_type
            
            z.writestr(zi, payload)
            
            # Record metadata
            sha256 = hashlib.sha256(payload).hexdigest()
            info = z.getinfo(zi.filename)
            
            manifest["tables"][name] = {
                "filename": safe_name,  # Track sanitized name
                "size_bytes": len(payload),
                "size_stored": info.file_size,
                "size_compressed": info.compress_size,
                "sha256": sha256,
                "compression_ratio": round(info.compress_size / len(payload), 3) if len(payload) > 0 else 1.0
            }
            
            # For root hash computation
            table_hashes.append((name, sha256))
        
        # Compute deterministic root hash
        root_hasher = hashlib.sha256()
        for name, hash_val in sorted(table_hashes):
            root_hasher.update(name.encode('utf-8'))
            root_hasher.update(hash_val.encode('utf-8'))
        manifest["bundle_hash"] = root_hasher.hexdigest()
        
        # Volatile metadata (not part of identity)
        manifest["_volatile"] = {
            "created_at": datetime.now().isoformat(),
        }
        
        # Write manifest with fixed timestamp
        manifest_zi = ZipInfo(filename="manifest.json")
        manifest_zi.date_time = (1980, 1, 1, 0, 0, 0)
        manifest_zi.compress_type = compress_type
        z.writestr(manifest_zi, json.dumps(manifest, indent=2, sort_keys=True))
    
    return buf.getvalue()


def decode_zip(
    blob: bytes,
    include: Optional[Set[str]] = None,
    validate: bool = True
) -> SimReturn:
    """Decode ZIP to SimReturn with optional validation.
    
    Args:
        blob: ZIP bytes from encode_zip
        include: If provided, only load these table names
        validate: Whether to verify SHA256 hashes
    
    Returns:
        Dict of table_name -> Arrow IPC bytes
        
    Raises:
        ValueError: If validation fails or version unsupported
    """
    out: Dict[str, bytes] = {}
    
    with zipfile.ZipFile(io.BytesIO(blob), "r") as z:
        # Load and validate manifest
        manifest = json.loads(z.read("manifest.json"))
        
        if manifest.get("version", 1) > 2:
            raise ValueError(f"Unsupported manifest version: {manifest['version']}")
        
        # Optionally validate bundle hash
        if validate and "bundle_hash" in manifest:
            hasher = hashlib.sha256()
            for name in sorted(manifest["tables"].keys()):
                hasher.update(name.encode('utf-8'))
                hasher.update(manifest["tables"][name]["sha256"].encode('utf-8'))
            if hasher.hexdigest() != manifest["bundle_hash"]:
                raise ValueError("Bundle hash mismatch - data may be corrupted")
        
        # Load requested tables
        for name, info in manifest["tables"].items():
            if include and name not in include:
                continue
            
            # Use sanitized filename from manifest (v2) or compute it (v1)
            if "filename" in info:
                filename = f"tables/{info['filename']}.arrow"
            else:
                # v1 compatibility
                filename = f"tables/{sanitize_table_name(name)}.arrow"
            
            data = z.read(filename)
            
            # Validate individual table hash
            if validate:
                actual_hash = hashlib.sha256(data).hexdigest()
                if actual_hash != info["sha256"]:
                    raise ValueError(f"Hash mismatch for table '{name}'")
            
            out[name] = data
    
    return out


def decode_zip_with_meta(blob: bytes) -> Tuple[SimReturn, dict]:
    """Decode ZIP returning both data and metadata.
    
    Args:
        blob: ZIP bytes from encode_zip
        
    Returns:
        Tuple of (SimReturn dict, manifest dict)
    """
    with zipfile.ZipFile(io.BytesIO(blob), "r") as z:
        manifest = json.loads(z.read("manifest.json"))
    
    tables = decode_zip(blob, validate=True)
    return tables, manifest


def decode_zip_lazy(blob: bytes) -> Dict[str, Callable[[], bytes]]:
    """Return lazy loaders for each table.
    
    Useful for large results where you don't want to load
    all tables into memory at once.
    
    Args:
        blob: ZIP bytes from encode_zip
        
    Returns:
        Dict of table_name -> callable that loads that table
    """
    with zipfile.ZipFile(io.BytesIO(blob), "r") as z:
        manifest = json.loads(z.read("manifest.json"))
    
    loaders = {}
    for name in manifest["tables"]:
        def make_loader(n):
            return lambda: decode_zip(blob, include={n})[n]
        loaders[name] = make_loader(name)
    
    return loaders


def inspect_zip(blob: bytes) -> dict:
    """Inspect ZIP contents without loading data.
    
    Args:
        blob: ZIP bytes from encode_zip
        
    Returns:
        Manifest dict with table metadata
    """
    with zipfile.ZipFile(io.BytesIO(blob), "r") as z:
        return json.loads(z.read("manifest.json"))
