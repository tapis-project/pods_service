from asyncio import protocols
import http
import re
from sre_constants import ANY
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set
from wsgiref import validate
from pydantic import BaseModel, Field, validator, model_validator, conint
from codes import PERMISSION_LEVELS

from stores import pg_store
from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from sqlalchemy import UniqueConstraint
from sqlalchemy.inspection import inspect
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, Session, SQLModel, select, JSON, Column, String
from models_base import TapisApiModel, TapisModel


class SetPermission(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Volume class.
    """
    # Required
    user: str = Field(..., description = "User to modify permissions for. Supports 'username' or 'tenant.<tenant_id>' format.")
    level: str = Field(..., description = "Permission level to give the user.")

    @validator('user')
    def check_user(cls, v):
        if not v:
            raise ValueError("user field cannot be empty.")
        # Allow site-wide wildcard '**' (templates visible to all tenants, admin-only at API layer)
        if v == '**':
            return v
        # Allow tenant.<tenant_id> format (e.g., tenant.dev, tenant.public)
        if v.startswith('tenant.'):
            tenant_id = v[len('tenant.'):]
            if not tenant_id:
                raise ValueError("'tenant.' permission must include a tenant ID. e.g. 'tenant.public'.")
            if not tenant_id.isascii():
                raise ValueError(f"'tenant.' permission tenant ID must be ASCII. Got '{tenant_id}'.")
            res = re.fullmatch(r'[a-z][a-z0-9-]*', tenant_id)
            if not res:
                raise ValueError(f"'tenant.' permission tenant ID must be lowercase alphanumeric (with hyphens). Got '{tenant_id}'.")
            if len(tenant_id) > 64:
                raise ValueError(f"'tenant.' permission tenant ID must be less than 64 characters. Got length {len(tenant_id)}.")
        else:
            # Standard username: alphanumeric with underscores/hyphens/dots/@ (for emails); leading _ allowed for service accounts
            res = re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.@-]*', v)
            if not res:
                raise ValueError(f"User must start with a letter or underscore and may contain alphanumeric characters, underscores, hyphens, dots, or @ (for email addresses), or use 'tenant.<tenant_id>' format. Got '{v}'.")
        return v

    @validator('level')
    def check_level(cls, v):
        if v not in PERMISSION_LEVELS:
            raise ValueError(f"level must be in {PERMISSION_LEVELS}")
        return v

    @model_validator(mode="after")
    def check_tenant_level(cls, values):
        user = getattr(values, 'user', '')
        level = getattr(values, 'level', '')
        if user and user.startswith('tenant.') and level != 'READ':
            raise ValueError(f"tenant.* permissions only support READ level (cross-tenant auth gate). Got '{level}'.")
        if user == '**' and level != 'READ':
            raise ValueError(f"Site-wide '**' permissions only support READ level. Got '{level}'.")
        return values

class DeletePermission(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Volume class.
    """
    # Required
    user: str = Field(..., description = "User to delete permissions from.")


class PermissionsModel(TapisApiModel):
    permissions: List[str] = Field([], description = "Pod permissions for each user.")


class LogsModel(TapisApiModel):
    logs: str = Field("", description = "Logs from kubernetes pods, useful for debugging and reading results.")
    action_logs: List[str] = Field([], description = "Log of actions taken on this pod.", sa_column=Column(ARRAY(String, dimensions=1)))


class CredentialsModel(TapisApiModel):
    user_username: str
    user_password: str


class FileModel(TapisApiModel):
    path: str = Field(..., description = "Path of object.")
    name: str = Field(..., description = "Name of object.")
    type: str = Field(..., description = "Type of object.")
    size: int = Field(..., description = "Size of object in bytes.")
    lastModified: str = Field(..., description = "Last modified date of object.")
    nativePermissions: str = Field(..., description = "Native permissions of object.")


class FilesListResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[FileModel]
    status: str
    version: str


class FilesUploadResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str 
