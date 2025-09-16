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

from sqlalchemy import UniqueConstraint
from sqlalchemy.inspection import inspect
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, Session, SQLModel, select, JSON, Column, String
from volume_utils import files_listfiles
from models_base import TapisModel, TapisApiModel
from models_misc import PermissionsModel, LogsModel
from models_volumes import Volume


class ClusterBase(TapisApiModel):
    # Required
    cluster_id: str = Field(..., description="Unique cluster identifier.", primary_key=True)
    name: str = Field(..., description="Human-readable name for the cluster.")
    type: str = Field(..., description="Cluster type, e.g., 'localK8InCluster', 'tailscaleK8', 'docker'.")
    description: str = Field(..., description="Description of the cluster.")
    # Cluster connection/config fields
    k8config: Optional[Dict[str, Any]] = Field(default_factory=dict, sa_column=Column(JSON), description="Kubernetes config (kubeconfig or API details).")
    k8username: Optional[str] = Field(None, description="Username to use for cluster access.")
    namespace: Optional[str] = Field(None, description="Kubernetes namespace to use by default.")
    settings: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Cluster-type-specific settings (e.g., tailscale flags, endpoints, etc.)")


class ClusterBaseRead(ClusterBase):
    # Provided
    status: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Last-known status/capabilities of the cluster.")
    creation_ts: datetime | None = Field(None, description="Time (UTC) that this cluster was created.")
    update_ts: datetime | None = Field(None, description="Time (UTC) that this cluster was updated.")


class ClusterBaseFull(ClusterBaseRead):
    # Provided
    tenant_id: str = Field("", description="Tapis tenant used during creation of this cluster.")
    site_id: str = Field("", description="Tapis site used during creation of this cluster.")
    #owner: str = Field(default=g.username, description="Username of the cluster owner.", index=True)
    permissions: List[str] = Field(default_factory=list, sa_column=Column(ARRAY(String, dimensions=1)), description="Cluster permissions for each user.")
    action_logs: List[str] = Field(default_factory=list, sa_column=Column(ARRAY(String, dimensions=1)), description="Log of past 10 actions taken on this cluster.")

    def display(self):
        display = self.dict()
        display.pop('action_logs', None)
        display.pop('tenant_id', None)
        display.pop('site_id', None)
        display.pop('permissions', None)
        return display


TapisClusterBaseFull = create_model("TapisClusterBaseFull", __base__=type("_ComboModel", (ClusterBaseFull, TapisModel), {}))


class Cluster(TapisClusterBaseFull, table=True, validate=True):
    @validator('cluster_id')
    def check_cluster_id(cls, v):
        reserved_cluster_ids = ["default", "admin", "system"]
        if v in reserved_cluster_ids:
            raise ValueError(f"cluster_id overlaps with reserved cluster ids: {reserved_cluster_ids}")
        res = re.fullmatch(r'[a-z][a-z0-9\-]+', v)
        if not res:
            raise ValueError(f"cluster_id must be lowercase alphanumeric or hyphen, starting with alpha.")
        if len(v) > 64 or len(v) < 3:
            raise ValueError(f"cluster_id length must be between 3-64 characters. Inputted length: {len(v)}")
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

    @validator('action_logs')
    def check_action_logs(cls, v):
        return [f"{datetime.utcnow().strftime('%y/%m/%d %H:%M')}: Cluster object created by '{g.username}'"]

    @validator('permissions')
    def check_permissions(cls, v):
        if not v:
            v = [f"{g.username}:ADMIN"]
        return v

    @validator('description')
    def check_description(cls, v):
        if not v.isascii():
            raise ValueError(f"description field may only contain ASCII characters.")
        if len(v) > 255:
            raise ValueError(f"description field must be less than 255 characters. Inputted length: {len(v)}")
        return v

    def display(self):
        display = self.dict()
        display.pop('action_logs', None)
        display.pop('tenant_id', None)
        display.pop('site_id', None)
        display.pop('permissions', None)
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
        stmt = select(Cluster).where(Cluster.permissions.overlap(permission_list))
        results = store.run("execute", stmt, scalars=True, all=True)
        logger.info(f"Got rows from table {tenant}.{table_name}.")
        return results


class NewCluster(ClusterBase):
    """
    Object with fields that users are allowed to specify for the Cluster class.
    """
    pass


class UpdateCluster(TapisApiModel):
    """
    Object with fields that users are allowed to specify when updating the Cluster class.
    """
    description: Optional[str] = Field("", description = "Description of this cluster.")
    settings: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Cluster-type-specific settings.")
    k8config: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Kubernetes config.")
    username: Optional[str] = Field(None, description="Username to use for cluster access.")
    namespace: Optional[str] = Field(None, description="Kubernetes namespace to use by default.")


class ClusterResponseModel(ClusterBaseRead):
    pass


class ClusterResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: ClusterResponseModel
    status: str
    version: str


class ClustersResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[ClusterResponseModel]
    status: str
    version: str


class ClusterDeleteResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str


class ClusterPermissionsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PermissionsModel
    status: str
    version: str


class ClusterLogsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: LogsModel
    status: str
    version: str