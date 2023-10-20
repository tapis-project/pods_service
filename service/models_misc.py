from asyncio import protocols
import http
import re
from sre_constants import ANY
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set
from wsgiref import validate
from pydantic import BaseModel, Field, validator, root_validator, conint
from codes import PERMISSION_LEVELS

from stores import pg_store
from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from __init__ import t

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
    user: str = Field(..., description = "User to modify permissions for.")
    level: str = Field(..., description = "Permission level to give the user.")

    @validator('level')
    def check_level(cls, v):
        if v not in PERMISSION_LEVELS:
            raise ValueError(f"level must be in {PERMISSION_LEVELS}")
        return v

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
