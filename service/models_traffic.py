from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import Field, validator, create_model
import uuid

from tapisservice.logs import get_logger
from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf

from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import or_
from sqlmodel import Field, SQLModel, select, JSON, Column, String, delete, func, text
from models_base import TapisModel, TapisApiModel

logger = get_logger(__name__)


class TrafficLogBase(TapisApiModel):
    pod_id: str = Field(..., description="Pod this traffic entry belongs to.", index=True)
    ts: datetime = Field(..., description="UTC timestamp of the request.", index=True)
    method: str = Field(..., description="HTTP method (GET, POST, etc).")
    path: str = Field(..., description="Request path.")
    status_code: int = Field(..., description="HTTP response status code.", index=True)
    duration_ms: float = Field(0.0, description="Request duration in milliseconds.")
    source_ip: str = Field("", description="Client source IP address.")
    username: Optional[str] = Field(default=None, description="X-Tapis-User header value when tapis_auth is active.")
    entry_point: str = Field("", description="Traefik entryPoint name.")
    router_name: str = Field("", description="Traefik routerName (encodes pod_id).")
    raw_headers: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON), description="Full request headers when no tapis_auth present.")


class TrafficLogBaseRead(TrafficLogBase):
    id: Optional[str] = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Primary key for the traffic log entry.",
        sa_column=Column(UUID(as_uuid=False), primary_key=True, index=True, unique=True, nullable=False)
    )


class TrafficLogBaseFull(TrafficLogBaseRead):
    tenant_id: str = Field("", description="Tapis tenant.", index=True)
    site_id: str = Field("", description="Tapis site.")

    def display(self):
        d = self.dict()
        d.pop('tenant_id', None)
        d.pop('site_id', None)
        return d


TapisTrafficLogBaseFull = create_model(
    "TapisTrafficLogBaseFull",
    __base__=type("_ComboModel", (TrafficLogBaseFull, TapisModel), {})
)


class TrafficLog(TapisTrafficLogBaseFull, table=True, validate=True):
    __tablename__ = "traffic_logs"

    @validator('tenant_id')
    def set_tenant_id(cls, v):
        if v:
            return v
        return g.request_tenant_id

    @validator('site_id')
    def set_site_id(cls, v):
        if v:
            return v
        return g.site_id

    @classmethod
    def purge_old(cls, pod_id: str, tenant: str, site: str, keep: int = 1000):
        """Delete rows beyond the `keep` most-recent for a given pod."""
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        pod_id_filter = or_(cls.pod_id == pod_id, cls.pod_id.like(f'{pod_id}@%'))
        subq = (
            select(cls.ts)
            .where(pod_id_filter, cls.tenant_id == tenant, cls.site_id == site)
            .order_by(cls.ts.desc())
            .offset(keep)
            .limit(1)
        )
        cutoff_ts = store.run("scalar", subq)
        if cutoff_ts:
            stmt = delete(cls).where(
                pod_id_filter,
                cls.tenant_id == tenant,
                cls.site_id == site,
                cls.ts <= cutoff_ts
            )
            store.run("execute", stmt)

    @classmethod
    def get_recent(cls, pod_id: str, tenant: str, site: str,
                   limit: int = 100,
                   method: Optional[str] = None,
                   status_class: Optional[str] = None,
                   status_code: Optional[int] = None,
                   username: Optional[str] = None,
                   since: Optional[datetime] = None,
                   until: Optional[datetime] = None) -> List['TrafficLog']:
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        # Match bare pod_id OR legacy rows stored with @{entrypoint} suffix (e.g. headscale@http)
        q = select(cls).where(
            or_(cls.pod_id == pod_id, cls.pod_id.like(f'{pod_id}@%')),
            cls.tenant_id == tenant,
            cls.site_id == site,
        )
        if method:
            q = q.where(cls.method == method.upper())
        if status_code:
            q = q.where(cls.status_code == status_code)
        elif status_class:
            prefix = int(status_class[0])
            q = q.where(cls.status_code >= prefix * 100, cls.status_code < (prefix + 1) * 100)
        if username:
            q = q.where(cls.username == username)
        if since:
            q = q.where(cls.ts >= since)
        if until:
            q = q.where(cls.ts <= until)
        q = q.order_by(cls.ts.desc()).limit(min(limit, 1000))
        return store.run("execute", q, scalars=True, all=True)


class TrafficLogResponseModel(TrafficLogBase):
    id: str
    tenant_id: str
    site_id: str


class TrafficLogsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[TrafficLogResponseModel]
    status: str
    version: str
