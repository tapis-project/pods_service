from asyncio import protocols
import http
import re
from sre_constants import ANY
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set, Optional
from wsgiref import validate
from pydantic import BaseModel, Field, validator, root_validator, create_model
from codes import PERMISSION_LEVELS, PermissionLevel

from stores import pg_store
from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from sqlalchemy import UniqueConstraint
from sqlalchemy.inspection import inspect
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, Session, SQLModel, select, JSON, Column, String
from models_base import TapisModel, TapisApiModel
from models_misc import PermissionsModel, LogsModel


class VolumeBase(TapisApiModel):
    # Required
    volume_id: str = Field(..., description = "Name of this volume.", primary_key = True)

    # Optional
    description: str = Field("", description = "Description of this volume.")
    size_limit: int = Field(1024, description = "Size in MB to limit volume to. We'll start warning if you've gone past the limit.")


class VolumeBaseRead(VolumeBase):
    # Provided
    size: int = Field(0, description = "Size of volume currently in MB")
    status: str = Field("REQUESTED", description = "Current status of volume.")
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this volume was created.")
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this volume was updated.")


class VolumeBaseFull(VolumeBaseRead):
    # Provided
    tenant_id: str = Field("", description = "Tapis tenant used during creation of this volume.")
    site_id: str = Field("", description = "Tapis site used during creation of this volume.")
    k8_name: str = Field("", description = "Name to use for Kubernetes name.")
    logs: str = Field("", description = "Logs from kubernetes pods, useful for debugging and reading results.")
    permissions: List[str] = Field([], description = "Volume permissions for each user.", sa_column=Column(ARRAY(String, dimensions=1)))


TapisVolumeBaseFull = create_model("TapisVolumeBaseFull", __base__= type("_ComboModel", (VolumeBaseFull, TapisModel), {}))


class Volume(TapisVolumeBaseFull, table=True, validate=True):
    @validator('volume_id')
    def check_volume_id(cls, v):
        # In case we want to add reserved keywords.
        reserved_volume_ids = []
        if v in reserved_volume_ids:
            raise ValueError(f"volume_id overlaps with reserved pod ids: {reserved_volume_ids}")
        # Regex match full volume_id to ensure a-z0-9.
        res = re.fullmatch(r'[a-z][a-z0-9]+', v)
        if not res:
            raise ValueError(f"volume_id must be lowercase alphanumeric. First character must be alpha.")
        # volume_id char limit = 64
        if len(v) > 128 or len(v) < 3:
            raise ValueError(f"volume_id length must be between 3-128 characters. Inputted length: {len(v)}")
        return v

    @validator('tenant_id')
    def check_tenant_id(cls, v):
        return g.request_tenant_id

    @validator('site_id')
    def check_site_id(cls, v):
        return g.site_id

    @validator('creation_ts')
    def check_creation_ts(cls, v):
        return datetime.utcnow()
    
    @validator('update_ts')
    def check_update_ts(cls, v):
        return datetime.utcnow()

    @validator('permissions')
    def check_permissions(cls, v):
        #By default add author permissions to model.
        if not v:
            v = [f"{g.username}:ADMIN"]
        return v

    @validator('description')
    def check_description(cls, v):
        # ensure description is all ascii
        if not v.isascii():
            raise ValueError(f"description field may only contain ASCII characters.")            
        # make sure description < 255 characters
        if len(v) > 255:
            raise ValueError(f"description field must be less than 255 characters. Inputted length: {len(v)}")
        return v

    @root_validator(pre=False)
    def set_k8_name_and_networking_urls(cls, values):
        # NOTE: Pydantic loops during validation, so for a few calls, tenant_id and site_id will be NONE.
        # Must account for this. By end of loop, everything will be set properly.
        # In this case "tacc" tenant is backup.
        site_id = values.get('site_id')
        tenant_id = values.get('tenant_id') or "tacc"
        pod_id = values.get('pod_id')
        ### k8_name: podvol-<site>-<tenant>-<pod_id>
        values['k8_name'] = f"podvol-{site_id}-{tenant_id}-{pod_id}"
        return values

    def display(self):
        display = self.dict()
        display.pop('logs')
        display.pop('k8_name')
        display.pop('tenant_id')
        display.pop('permissions')
        display.pop('site_id')
        return display

    @classmethod
    def db_get_all_with_permission(cls, user, level, tenant, site):
        """
        Get all and ensure permission exists.
        """
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        table_name = cls.table_name()
        logger.info(f'Top of {table_name}.db_get_all_with_permissions() for tenant.site: {tenant}.{site}')

        # Get list of level specified + levels above.
        authorized_levels = PermissionLevel(level).authorized_levels()

        # Create list of permissions user needs to access this resource
        # In the case of level=USER, USER and ADMIN work, so: ["cgarcia:ADMIN", "cgarcia:USER"]
        permission_list = []
        for authed_level in authorized_levels:
            permission_list.append(f"{user}:{authed_level}")

        # Create statement
        stmt = select(Volume).where(Volume.permissions.overlap(permission_list))   

        # Run command
        results = store.run("execute", stmt, scalars=True, all=True)

        logger.info(f"Got rows from table {tenant}.{table_name}.")
        return results


class NewVolume(VolumeBase):
    """
    Object with fields that users are allowed to specify for the Volume class.
    """
    pass


class UpdateVolume(TapisApiModel):
    """
    Object with fields that users are allowed to specify when updating the Volume class.
    """
    description: Optional[str] = Field("", description = "Description of this volume.")
    size_limit: Optional[int] = Field(1024, description = "Size in MB to limit volume to. We'll start warning if you've gone past the limit.")


class VolumeResponseModel(VolumeBaseRead):
    """
    Response object for Volume class.
    """
    pass


class VolumeResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: VolumeResponseModel
    status: str
    version: str


class VolumesResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[VolumeResponseModel]
    status: str
    version: str


class DeleteVolumeResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str


class VolumePermissionsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PermissionsModel
    status: str
    version: str


class VolumeLogsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: LogsModel
    status: str
    version: str