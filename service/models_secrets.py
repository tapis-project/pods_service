from datetime import datetime
from typing import List, Dict, Optional, Literal
from pydantic import validator, create_model
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Column, String, Field

from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
from models_base import TapisApiModel, TapisModel

logger = get_logger(__name__)

# Constants for SK operations - all secrets stored under Pods service account
PODS_SERVICE_ACCOUNT = "pods"  # TODO change to conf
PODS_SERVICE_TENANT = "admin"  # All secrets stored in admin tenant under pods service


class SecretBase(TapisApiModel):
    secret_id: str = Field(..., description="Name of the secret.", primary_key=True)
    scope: str = Field("user", description="Scope of secret: 'user' or 'pod'")
    pod_id: Optional[str] = Field(None, description="Pod ID if scope is 'pod'")
    description: str = Field("", description="Description of this secret.")
    readable: bool = Field(True, description="If True, secret value can be retrieved via GET /secrets/{id}/value. Pod injection always works regardless of this setting.")
    writable: bool = Field(True, description="If True, secret value can be updated via PUT or POST recreation. If False, secret is write-once.")

class SecretBaseRead(SecretBase):
    # Provided
    sk_secret_name: str = Field("", description="Full secret name used in SK (prefixed).")
    creation_ts: Optional[datetime] = Field(None, description="Time (UTC) that this secret was created.")
    added_by: str = Field("", description="User who added this secret.")


class SecretBaseFull(SecretBaseRead):
    # Provided
    tenant_id: str = Field("", description="Tapis tenant used during creation of this secret.")
    site_id: str = Field("", description="Tapis site used during creation of this secret.")
    permissions: List[str] = Field([], description="Secret permissions for each user.", sa_column=Column(ARRAY(String, dimensions=1)))


TapisSecretBaseFull = create_model("TapisSecretBaseFull", __base__=type("_ComboModel", (SecretBaseFull, TapisModel), {}))


class Secret(TapisSecretBaseFull, table=True, validate=True):
    @validator('scope')
    def check_scope(cls, v):
        if v not in ('user', 'pod'):
            raise ValueError(f"scope must be 'user' or 'pod'. Got: {v}")
        return v

    @validator('secret_id')
    def check_secret_id(cls, v):
        # Ensure secret name is alphanumeric with underscores/dashes
        if not v.replace('_', '').replace('-', '').isalnum():
            raise ValueError(f"secret_id must be alphanumeric and may include '_' or '-'. Got: {v}")
        # Ensure no spaces
        if " " in v:
            raise ValueError(f"secret_id may not contain spaces. Invalid name: '{v}'")
        # Length check
        if len(v) > 110:
            raise ValueError(f"secret_id must be less than 110 characters. Got: {len(v)}")
        return v

    @validator('pod_id')
    def check_pod_id(cls, v, values):
        scope = values.get('scope')
        if scope == 'pod' and not v:
            raise ValueError("pod_id is required when scope is 'pod'")
        if scope == 'user' and v:
            raise ValueError("pod_id should not be set when scope is 'user'")
        return v

    @validator('added_by', always=True)
    def check_added_by(cls, v):
        if not v or v == "":
            v = g.username
        if not v:
            raise ValueError("added_by field must be set to a valid username.")
        return v

    @validator('tenant_id', always=True)
    def check_tenant_id(cls, v):
        return v or g.request_tenant_id

    @validator('site_id', always=True)
    def check_site_id(cls, v):
        return v or g.site_id

    @validator('creation_ts', always=True)
    def check_creation_ts(cls, v):
        return v or datetime.utcnow()

    @validator('sk_secret_name', always=True)
    def generate_sk_secret_name(cls, v, values):
        # Generate prefixed secret name for SK
        # Format: pods_{site}_{tenant}_{scope}+{identifier}+{secret_id}
        # We use this format as SK has folder heirarchy based on +. Allowing us full secret_name length.
        # All secrets stored under Pods service account, partitioned by site/tenant/user
        site = values.get('site_id') or g.site_id
        tenant = values.get('tenant_id') or g.request_tenant_id
        scope = values.get('scope', 'user')
        secret_id = values.get('secret_id', '')
        
        if scope == "user":
            user = values.get('added_by') or g.username
            return f"pods_{tenant}_user+{user}+{secret_id}"
        elif scope == "pod":
            pod_id = values.get('pod_id')
            if not pod_id:
                raise ValueError("pod_id is required when scope is 'pod'")
            return f"pods_{tenant}_pod+{pod_id}+{secret_id}"
        
        return v

    @validator('description')
    def check_description(cls, v):
        if v and not v.isascii():
            raise ValueError("description field may only contain ASCII characters.")
        if v and len(v) > 500:
            raise ValueError(f"description must be less than 500 characters. Got: {len(v)}")
        return v or ""

    @validator('permissions', always=True)
    def check_permissions(cls, v, values):
        # Set default permission for creator
        if not v:
            added_by = values.get('added_by') or g.username
            v = [f"{added_by}:ADMIN"]
        return v

    def get_permissions(self):
        """
        Parse permissions list into a dictionary mapping username to permission level.
        Format: ['username:LEVEL', 'user2:LEVEL']
        Returns: {'username': 'LEVEL', 'user2': 'LEVEL'}
        """
        permissions_dict = {}
        for permission_str in self.permissions:
            if ':' in permission_str:
                user, level = permission_str.split(':', 1)
                permissions_dict[user] = level
        return permissions_dict

    def display(self):
        display = self.dict()
        display.pop('tenant_id', None)
        display.pop('permissions')
        display.pop('site_id', None)
        return display


class NewSecret(SecretBase):
    """
    Object with fields that users are allowed to specify for the Secret class.
    """
    secret_value: str = Field(..., description="The actual secret value to store.")


class UpdateSecret(TapisApiModel):
    """
    Object with fields that users are allowed to update for the Secret class.
    """
    description: Optional[str] = Field(None, description="Description of this secret.")
    secret_value: Optional[str] = Field(None, description="The new secret value to store.")


class SecretResponseModel(SecretBaseRead):
    """
    Response object for Secret class (no secret value returned).
    """
    pass


class SecretResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: SecretResponseModel
    status: str
    version: str


class SecretsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[SecretResponseModel]
    status: str
    version: str


class SecretDeleteResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str


class SecretValueResponse(TapisApiModel):
    """
    Response for getting the actual secret value.
    """
    message: str
    metadata: Dict
    result: Dict[str, str]  # {"secret_value": "..."}
    status: str
    version: str


class SecretPermissionsResponse(TapisApiModel):
    """
    Response for secret permissions operations.
    """
    message: str
    metadata: Dict
    result: Dict[str, List[str]]  # {"permissions": ["user:ADMIN", ...]}
    status: str
    version: str