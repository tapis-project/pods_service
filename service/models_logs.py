from asyncio import protocols
import http
import re
from sre_constants import ANY
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set, Optional
from wsgiref import validate
from pydantic import BaseModel, Field, validator, model_validator, create_model
from codes import PermissionLevel, USER

from stores import pg_store
from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapipy.errors import NotFoundError
from utils import check_permissions
logger = get_logger(__name__)

from sqlalchemy import UniqueConstraint, BigInteger
from sqlalchemy.inspection import inspect
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlmodel import Field, Session, SQLModel, select, JSON, Column, String
from volume_utils import files_listfiles
from models_base import TapisModel, TapisApiModel
from models_misc import PermissionsModel, LogsModel
from models_volumes import Volume
import uuid

class LogBase(TapisApiModel):
    resource_type: str = Field(..., description="Type of resource (e.g., pod, cluster, service).", index=True)
    resource_id: str = Field(..., description="ID of the resource (e.g., pod_id, cluster_id).", index=True)
    log_type: str = Field(..., description="Type of log (e.g., action, exec, deployment, metrics, docker, kubernetes, service).", index=True)
    username: Optional[str] = Field(default=None, description="User who triggered the log event.", index=True)
    message: Optional[str] = Field(default=None, description="Short message or summary of the log event.")
    details: Optional[Dict[str, Any]] = Field(default_factory=dict, sa_column=Column(JSON), description="Structured details for the log event.")


class LogBaseRead(LogBase):
    # Provided
    time: datetime | None = Field(None, description="Time (UTC) that this log was created.", index=True, primary_key=True)
    id: Optional[str] = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Primary key for the log entry.",
        sa_column=Column(UUID(as_uuid=False), primary_key=True, index=True, unique=True, nullable=False)
    )


class LogBaseFull(LogBaseRead):
    # Provided
    tenant_id: str = Field("", description="Tapis tenant used during creation of this log.", index=True)
    site_id: str = Field("", description="Tapis site used during creation of this log.")

    def display(self):
        display = self.dict()
        display.pop('action_logs', None)
        display.pop('tenant_id', None)
        display.pop('site_id', None)
        display.pop('permissions', None)
        return display

TapisLogBaseFull = create_model("TapisLogBaseFull", __base__=type("_ComboModel", (LogBaseFull, TapisModel), {}))

class Log(TapisLogBaseFull, table=True, validate=True):
    @validator('tenant_id')
    def check_tenant_id(cls, v):
        return g.request_tenant_id

    @validator('site_id')
    def check_site_id(cls, v):
        return g.site_id

    @validator('time')
    def check_time(cls, v):
        return datetime.utcnow()

    @validator('resource_type')
    def check_resource_type(cls, v):
        if not v.isascii():
            raise ValueError("resource_type must be ASCII.")
        return v

    @validator('resource_id')
    def check_resource_id(cls, v):
        if not v.isascii():
            raise ValueError("resource_id must be ASCII.")
        return v

    @validator('log_type')
    def check_log_type(cls, v):
        if not v.isascii():
            raise ValueError("log_type must be ASCII.")
        return v

    @validator('username')
    def check_username(cls, v):
        if v is not None and not v.isascii():
            raise ValueError("username must be ASCII.")
        return v

    @validator('message')
    def check_message(cls, v):
        if v is not None and not v.isascii():
            raise ValueError("message must be ASCII.")
        return v


class LogResponseModel(LogBase):
    pass


class LogResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: LogResponseModel
    status: str
    version: str


class LogsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[LogResponseModel]
    status: str
    version: str