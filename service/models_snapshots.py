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
from codes import PermissionLevel, USER
import codes

from stores import pg_store
from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapipy.errors import NotFoundError
from utils import check_permissions
logger = get_logger(__name__)

from __init__ import t

from sqlalchemy import UniqueConstraint
from sqlalchemy.inspection import inspect
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, Session, SQLModel, select, JSON, Column, String
from volume_utils import files_listfiles
from models_base import TapisModel, TapisApiModel
from models_misc import PermissionsModel, LogsModel
from models_volumes import Volume


class SnapshotBase(TapisApiModel):
    # Required
    snapshot_id: str = Field(..., description = "Name of this snapshot.", primary_key = True)
    source_volume_id: str = Field(..., description = "The volume_id to use as source of snapshot.")
    source_volume_path: str = Field(..., description = "Path in source volume_id to make snapshot of")

    # Required if volume/{source_volume_id}/{source_volume_path} is a file.
    destination_path: str = Field("", description = "Path to copy to. Snapshots of singular files require destination_path.")

    # Optional
    description: str = Field("", description = "Description of this snapshot.")
    size_limit: int = Field(1024, description = "Size in MB to limit snapshot to. We'll start warning if you've gone past the limit.")
    cron: str = Field("", description = "cron bits")
    retention_policy: str = Field("", description = "retention_policy bits")


class SnapshotBaseRead(SnapshotBase):
    # Provided
    size: int = Field(0, description = "Size of snapshot currently in MB")
    status: str = Field("REQUESTED", description = "Current status of snapshot.")
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this snapshot was created.")
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this snapshot was updated.")


class SnapshotBaseFull(SnapshotBaseRead):
    # Provided
    tenant_id: str = Field("", description = "Tapis tenant used during creation of this snapshot.")
    site_id: str = Field("", description = "Tapis site used during creation of this snapshot.")
    k8_name: str = Field("", description = "Name to use for Kubernetes name.")
    logs: str = Field("", description = "Logs from kubernetes pods, useful for debugging and reading results.")
    permissions: List[str] = Field([], description = "Snapshot permissions for each user.", sa_column=Column(ARRAY(String, dimensions=1)))


TapisSnapshotBaseFull = create_model("TapisSnapshotBaseFull", __base__= type("_ComboModel", (SnapshotBaseFull, TapisModel), {}))


class Snapshot(TapisSnapshotBaseFull, table=True, validate=True):
    @validator('snapshot_id')
    def check_snapshot_id(cls, v):
        # In case we want to add reserved keywords.
        reserved_snapshot_ids = []
        if v in reserved_snapshot_ids:
            raise ValueError(f"snapshot_id overlaps with reserved pod ids: {reserved_snapshot_ids}")
        # Regex match full snapshot_id to ensure a-z0-9.
        res = re.fullmatch(r'[a-z][a-z0-9]+', v)
        if not res:
            raise ValueError(f"snapshot_id must be lowercase alphanumeric. First character must be alpha.")
        # snapshot_id char limit = 64
        if len(v) > 128 or len(v) < 3:
            raise ValueError(f"snapshot_id length must be between 3-128 characters. Inputted length: {len(v)}")
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
        # By default, add author permissions to model.
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

    @validator('source_volume_id', pre=False)
    def check_source_volume_id(cls, v):
        # Check volume_id because there's no reason not to. Should follow same rules as initate volume_id create.
        # Regex match full source_volume_id to ensure a-z0-9.
        res = re.fullmatch(r'[a-z][a-z0-9]+', v)
        if not res:
            raise ValueError(f"source_volume_id must be lowercase alphanumeric. First character must be alpha.")
        # source_volume_id must now match a volume_id in the database.
        source_volume = Volume.db_get_with_pk(v, tenant=g.request_tenant_id, site=g.site_id)
        if not source_volume:
            raise ValueError(f"source_volume_id does not exist in our database. source_volume_id: {v}.")
        # check_source_volume_id_and_path ensures user has permissions
        return v

    @validator('source_volume_path')
    def check_source_volume_path(cls, v):
        # Source volume path must be a valid path.
        if not v.startswith("/"):
            raise ValueError(f"source_volume_path must start with a /")
        # check_source_volume_id_and_path() ensures source_volume_path exists in nfs.
        return v

    @validator('size_limit')
    def check_size_limit(cls, v):
        # Size limit must be greater than 0 and below 3GB in true bytes.
        if v < 1 or v > 3072:
            raise ValueError(f"size_limit must be between 1 and 3072 (MB)")
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

    @root_validator(pre=False)
    def check_source_volume_id_and_path(cls, values):
        # NOTE: Pydantic loops during validation, so for a few calls, source_volume_id and source_volume_path will be NONE.
        # Must account for this. By end of loop, everything will be set properly.
        source_volume_id = values.get('source_volume_id')
        source_volume_path = values.get('source_volume_path')
        destination_path = values.get('destination_path')

        if source_volume_path and source_volume_id and destination_path is not None:
            # Ensure source_volume_id exists in database.
            try:
                volume = Volume.db_get_with_pk(source_volume_id, tenant=g.request_tenant_id, site=g.site_id)
            except:
                raise ValueError(f"source_volume_id does not exist in our database. source_volume_id: {source_volume_id}.")

            # Ensure user has USER or ADMIN permissions to source_volume_id.
            if not check_permissions(user=g.username, object=volume, object_type="volume", level=USER, roles=g.roles):
                raise ValueError(f"User does not have permission to use source_volume_id. source_volume_id: {source_volume_id}.")
            
            # Ensure source_volume_path exists in nfs.
            try:
                source_listing = files_listfiles(
                    path = f"/volumes/{source_volume_id}/{source_volume_path}")
            except NotFoundError:
                raise NotFoundError(f"source_volume_path: {source_volume_path} not found source_volume_id: {source_volume_id}.")
            # Enforce destination_path being set if source_listing is a file and not a directory.
            file_end_path = f"{source_volume_id}/{source_volume_path}".replace("///", "/").replace("//", "/")
            if len(source_listing) == 1 and source_listing[0].type == 'file' and source_listing[0].url.endswith(file_end_path):
                if not destination_path:
                    msg = "destination_path must be set if source_volume_path is a file so we can save to a proper path."
                    logger.debug(f'{msg} source_listing: {source_listing}')
                    raise ValueError(msg)
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
        stmt = select(Snapshot).where(Snapshot.permissions.overlap(permission_list))   

        # Run command
        results = store.run("execute", stmt, scalars=True, all=True)

        logger.info(f"Got rows from table {tenant}.{table_name}.")
        return results


class NewSnapshot(SnapshotBase):
    """
    Object with fields that users are allowed to specify for the Snapshot class.
    """
    pass


class UpdateSnapshot(TapisApiModel):
    """
    Object with fields that users are allowed to specify when updating the Snapshot class.
    """
    description: Optional[str] = Field("", description = "Description of this snapshot.")
    size_limit: Optional[int] = Field(1024, description = "Size in MB to limit snapshot to. We'll start warning if you've gone past the limit.")
    cron: Optional[str] = Field("", description = "cron bits")
    retention_policy: Optional[str] = Field("", description = "retention_policy bits")


class SnapshotResponseModel(SnapshotBaseRead):
    """
    Response object for Snapshot class.
    """
    pass


class SnapshotResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: SnapshotResponseModel
    status: str
    version: str


class SnapshotsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[SnapshotResponseModel]
    status: str
    version: str


class DeleteSnapshotResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str


class SnapshotPermissionsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PermissionsModel
    status: str
    version: str


class SnapshotLogsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: LogsModel
    status: str
    version: str