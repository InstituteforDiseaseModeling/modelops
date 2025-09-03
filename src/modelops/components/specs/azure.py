"""Azure provider configuration models with validation."""

from __future__ import annotations
from typing import Any, Optional, Literal, Dict, List, Union
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.functional_validators import AfterValidator
from typing_extensions import Annotated
from ..config_base import ConfigModel


# ---------- Enums & Type Aliases ----------

PoolMode = Literal["System", "User"]
TaintEffect = Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]
ACRSku = Literal["Basic", "Standard", "Premium"]


# ---------- Validators ----------

def _semverish(v: str) -> str:
    """Validate Kubernetes version format."""
    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", v):
        raise ValueError("Must be MAJOR.MINOR or MAJOR.MINOR.PATCH (e.g., 1.32 or 1.29.7)")
    return v


def _dns_label(v: str) -> str:
    """Validate DNS-1123 label format."""
    if not v:
        raise ValueError("Cannot be empty")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", v):
        raise ValueError("Must be DNS-1123 compliant: lowercase alphanumeric + '-', no leading/trailing '-'")
    if len(v) > 63:
        raise ValueError("Must be 63 characters or less")
    return v


def _acr_name(v: str) -> str:
    """Validate ACR name format."""
    if not re.fullmatch(r"[a-z0-9]{5,50}", v):
        raise ValueError("ACR name must be 5-50 lowercase alphanumeric characters only")
    return v


def _label_key(k: str) -> str:
    """Validate Kubernetes label key format."""
    # Handle prefixed keys like 'example.com/key'
    if "/" in k:
        prefix, name = k.split("/", 1)
        # Prefix must be a valid DNS subdomain
        if not re.fullmatch(r"([a-z0-9]([-a-z0-9]*[a-z0-9])?\.)*[a-z]{2,}", prefix):
            raise ValueError(f"Invalid label prefix domain: {prefix}")
        k = name  # Validate name part below
    
    # Name segment validation
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?", k):
        raise ValueError(f"Invalid label key segment: {k}")
    
    return k


# Type annotations
Semverish = Annotated[str, AfterValidator(_semverish)]
DNSLabel = Annotated[str, AfterValidator(_dns_label)]
ACRName = Annotated[str, AfterValidator(_acr_name)]
LabelKey = Annotated[str, AfterValidator(_label_key)]


# ---------- Taint Model ----------

class Taint(BaseModel):
    """Kubernetes taint specification."""
    key: str
    value: Optional[str] = None
    effect: TaintEffect
    
    model_config = ConfigDict(extra="forbid")
    
    @classmethod
    def parse(cls, raw: Union[str, Dict[str, Any]]) -> "Taint":
        """
        Parse taint from string or dict format.
        
        String format: 'key=value:Effect' or 'key:Effect'
        Dict format: {'key': 'key', 'value': 'value', 'effect': 'Effect'}
        """
        if isinstance(raw, dict):
            return cls(**raw)
        
        # Parse string format
        match = re.fullmatch(r"([^=:]+)(=([^:]+))?:(NoSchedule|PreferNoSchedule|NoExecute)", raw)
        if not match:
            raise ValueError(
                "Taint must be 'key=value:Effect' or 'key:Effect' "
                "(e.g., 'gpu=true:NoSchedule' or 'gpu:NoSchedule')"
            )
        
        key = match.group(1)
        value = match.group(3)  # May be None
        effect = match.group(4)
        return cls(key=key, value=value, effect=effect)
    
    def to_azure_format(self) -> str:
        """Convert to Azure taint string format."""
        if self.value:
            return f"{self.key}={self.value}:{self.effect}"
        return f"{self.key}:{self.effect}"


# ---------- Node Pool Model ----------

class NodePool(BaseModel):
    """AKS node pool configuration."""
    name: DNSLabel
    vm_size: str  # Keep as string - Azure has many SKUs
    mode: PoolMode = "User"
    
    # Sizing: either fixed 'count' OR autoscale ('min' & 'max')
    count: Optional[int] = None
    min: Optional[int] = None
    max: Optional[int] = None
    
    # Labels and taints
    labels: Dict[str, str] = Field(default_factory=dict)
    taints: List[Taint] = Field(default_factory=list)
    
    model_config = ConfigDict(extra="forbid")
    
    @field_validator("count", "min", "max")
    @classmethod
    def validate_nonnegative(cls, v, info):
        """Ensure counts are non-negative."""
        if v is not None and v < 0:
            raise ValueError(f"{info.field_name} must be >= 0")
        return v
    
    @field_validator("labels", mode="before")
    @classmethod
    def validate_label_keys(cls, v):
        """Validate label keys follow Kubernetes format."""
        if not isinstance(v, dict):
            return v
        
        validated = {}
        for key, value in v.items():
            try:
                validated_key = _label_key(key)
                validated[key] = str(value)  # Ensure value is string
            except ValueError as e:
                raise ValueError(f"Invalid label key '{key}': {e}")
        return validated
    
    @field_validator("taints", mode="before")
    @classmethod
    def parse_taints(cls, v):
        """Parse taints from string or dict format."""
        if v is None:
            return []
        return [Taint.parse(t) for t in v]
    
    @model_validator(mode="after")
    def validate_sizing(self):
        """Validate XOR: either count OR (min, max)."""
        if self.count is not None:
            # Fixed size mode
            if self.min is not None or self.max is not None:
                raise ValueError("Specify either 'count' OR 'min'/'max', not both")
            if self.count < 1:
                raise ValueError("'count' must be >= 1")
        else:
            # Autoscaling mode
            if self.min is None or self.max is None:
                raise ValueError("When not using 'count', both 'min' and 'max' are required")
            if self.min < 0:
                raise ValueError("'min' must be >= 0")
            if self.max < 1:
                raise ValueError("'max' must be >= 1")
            if self.min > self.max:
                raise ValueError("'min' must be <= 'max'")
        return self


# ---------- AKS Configuration ----------

class AKSConfig(BaseModel):
    """Azure Kubernetes Service configuration."""
    name: DNSLabel
    kubernetes_version: Optional[Semverish] = None  # Optional - Azure uses latest if not specified
    node_pools: List[NodePool]
    
    model_config = ConfigDict(extra="forbid")
    
    @model_validator(mode="after")
    def require_system_pool(self):
        """Ensure at least one System pool exists."""
        if not any(p.mode == "System" for p in self.node_pools):
            raise ValueError("At least one node pool with mode='System' is required")
        return self


# ---------- ACR Configuration ----------

class ACRConfig(BaseModel):
    """Azure Container Registry configuration."""
    name: ACRName
    sku: ACRSku = "Standard"
    per_user_registry: bool = True  # Per-user in dev, org-level in prod
    
    model_config = ConfigDict(extra="forbid")


# ---------- Main Provider Configuration ----------

class AzureProviderConfig(ConfigModel):
    """Azure provider configuration for infrastructure."""
    provider: Literal["azure"]
    subscription_id: str  # Keep as string - UUID validation is too strict
    location: str = "eastus2"
    resource_group: str  # Base name, will be suffixed with username
    username: Optional[str] = None  # Explicit username override
    
    # Required and optional components
    aks: AKSConfig
    acr: Optional[ACRConfig] = None
    
    # SSH configuration (optional)
    ssh_public_key: Optional[str] = None
    
    # Allow future extensibility
    model_config = ConfigDict(extra="allow")
    
    @field_validator("subscription_id")
    @classmethod
    def validate_subscription_id(cls, v):
        """Basic validation of subscription ID format."""
        # Azure subscription IDs are GUIDs
        if not re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', v):
            raise ValueError("Invalid Azure subscription ID format (expected GUID)")
        return v
    
    @field_validator("resource_group")
    @classmethod
    def validate_resource_group(cls, v):
        """Validate resource group name."""
        if not re.match(r'^[-\w._()]+$', v):
            raise ValueError("Resource group name can only contain alphanumeric, underscore, parentheses, hyphen, period")
        if len(v) > 90:
            raise ValueError("Resource group name must be 90 characters or less")
        return v
    
    @model_validator(mode="after")
    def derive_username(self):
        """Derive username from config or system if not provided.
        
        Uses local system username for resource isolation, not Azure AD username.
        Azure AD would require subprocess/SDK calls; local username is simpler.
        """
        if not self.username:
            from ...core.config import get_username
            user = get_username()
            if user:
                # Sanitize for Azure naming: lowercase, alphanumeric + dash
                sanitized = re.sub(r"[^a-z0-9-]", "", user.lower()).strip("-")
                # Limit length
                self.username = (sanitized[:20] if len(sanitized) > 20 else sanitized) or "user"
            else:
                self.username = "user"
        return self
    
    @property
    def resource_group_final(self) -> str:
        """Get final resource group name with username suffix."""
        # Pattern: modelops-{env}-rg-{username}
        # Environment will be added by the component
        return f"{self.resource_group}-{self.username}"
    
    def get_acr_name(self, env: str) -> str:
        """Get ACR name based on environment and configuration."""
        if not self.acr:
            return ""
        
        base_name = self.acr.name
        
        # Per-user in dev/staging, org-level in prod
        if env in ("dev", "staging") and self.acr.per_user_registry:
            # Append username for per-user registry
            acr_name = f"{base_name}{self.username}"
        else:
            # Org-level registry (prod or explicit config)
            import random
            import string
            # Generate a short random suffix for uniqueness
            suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
            acr_name = f"{base_name}{suffix}"
        
        # Ensure ACR name is valid (lowercase alphanumeric only)
        acr_name = re.sub(r"[^a-z0-9]", "", acr_name.lower())
        
        # Limit to 50 characters (Azure ACR limit)
        return acr_name[:50] if len(acr_name) > 50 else acr_name