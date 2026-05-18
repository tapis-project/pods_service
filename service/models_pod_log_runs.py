from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import Field, validator, create_model
import uuid

from tapisservice.logs import get_logger
from tapisservice.tapisfastapi.utils import g

from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, select, Column, String, delete, func, text
from models_base import TapisModel, TapisApiModel

logger = get_logger(__name__)


class PodLogRunBase(TapisApiModel):
    pod_id: str = Field(..., description="Pod this run belongs to.", index=True)
    run_index: int = Field(..., description="1-based monotonically incrementing run counter per pod.")
    started_at: datetime = Field(..., description="UTC time this pod run became AVAILABLE.")
    stopped_at: Optional[datetime] = Field(default=None, description="UTC time this run ended (STOPPED/ERROR/COMPLETE).")
    logs: Optional[str] = Field(default=None, description="Pod stdout for this run.")
    log_size_bytes: int = Field(default=0, description="Byte length of logs field.")
    is_active: bool = Field(default=True, description="True = one of the last N kept runs in DB.")
    is_archived: bool = Field(default=False, description="True = logs exported to gzip file on disk.")
    archive_path: Optional[str] = Field(default=None, description="Absolute path to .log.gz archive file.")


class PodLogRunBaseRead(PodLogRunBase):
    id: Optional[str] = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Primary key.",
        sa_column=Column(UUID(as_uuid=False), primary_key=True, index=True, unique=True, nullable=False)
    )


class PodLogRunBaseFull(PodLogRunBaseRead):
    tenant_id: str = Field("", description="Tapis tenant.", index=True)
    site_id: str = Field("", description="Tapis site.")

    def display(self):
        d = self.dict()
        d.pop('tenant_id', None)
        d.pop('site_id', None)
        return d

    def display_meta(self):
        """Return metadata without the (potentially large) logs field."""
        d = self.display()
        d.pop('logs', None)
        return d


TapisPodLogRunBaseFull = create_model(
    "TapisPodLogRunBaseFull",
    __base__=type("_ComboModel", (PodLogRunBaseFull, TapisModel), {})
)


class PodLogRun(TapisPodLogRunBaseFull, table=True, validate=True):
    __tablename__ = "pod_log_runs"
    __table_args__ = (
        UniqueConstraint("pod_id", "tenant_id", "run_index", name="uq_pod_log_run"),
    )

    @validator('tenant_id')
    def set_tenant_id(cls, v):
        return g.request_tenant_id

    @validator('site_id')
    def set_site_id(cls, v):
        return g.site_id

    @classmethod
    def get_max_run_index(cls, pod_id: str, tenant: str, site: str) -> int:
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = (
            select(func.max(cls.run_index))
            .where(cls.pod_id == pod_id, cls.tenant_id == tenant, cls.site_id == site)
        )
        result = store.run("scalar", stmt)
        return result or 0

    @classmethod
    def get_active_run(cls, pod_id: str, tenant: str, site: str) -> Optional['PodLogRun']:
        """Return the current unstopped run (stopped_at is None), if any."""
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = (
            select(cls)
            .where(
                cls.pod_id == pod_id,
                cls.tenant_id == tenant,
                cls.site_id == site,
                cls.stopped_at == None,  # noqa: E711
            )
            .order_by(cls.run_index.desc())
            .limit(1)
        )
        return store.run("execute", stmt, scalars=True, first=True)

    @classmethod
    def get_or_create_active_run(cls, pod_id: str, tenant: str, site: str) -> 'PodLogRun':
        """Return the current open run, or create a new one."""
        existing = cls.get_active_run(pod_id, tenant, site)
        if existing:
            return existing
        next_index = cls.get_max_run_index(pod_id, tenant, site) + 1
        run = cls(
            pod_id=pod_id,
            run_index=next_index,
            started_at=datetime.utcnow(),
            tenant_id=tenant,
            site_id=site,
        )
        run.db_create(tenant=tenant, site=site)
        return run

    @classmethod
    def finalize_run(cls, pod_id: str, tenant: str, site: str):
        """Close any open run for this pod by setting stopped_at."""
        run = cls.get_active_run(pod_id, tenant, site)
        if run:
            run.stopped_at = datetime.utcnow()
            run.db_update(tenant=tenant, site=site)

    @classmethod
    def list_runs(cls, pod_id: str, tenant: str, site: str) -> List['PodLogRun']:
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = (
            select(cls)
            .where(cls.pod_id == pod_id, cls.tenant_id == tenant, cls.site_id == site)
            .order_by(cls.run_index.desc())
        )
        return store.run("execute", stmt, scalars=True, all=True)

    @classmethod
    def get_run_by_index(cls, pod_id: str, run_index: int, tenant: str, site: str) -> Optional['PodLogRun']:
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = (
            select(cls)
            .where(
                cls.pod_id == pod_id,
                cls.run_index == run_index,
                cls.tenant_id == tenant,
                cls.site_id == site,
            )
        )
        return store.run("execute", stmt, scalars=True, first=True)

    @classmethod
    def get_active_runs(cls, pod_id: str, tenant: str, site: str) -> List['PodLogRun']:
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = (
            select(cls)
            .where(
                cls.pod_id == pod_id,
                cls.tenant_id == tenant,
                cls.site_id == site,
                cls.is_active == True,  # noqa: E712
            )
            .order_by(cls.run_index.desc())
        )
        return store.run("execute", stmt, scalars=True, all=True)


class PodLogRunMetaResponse(TapisApiModel):
    id: str
    pod_id: str
    run_index: int
    started_at: datetime
    stopped_at: Optional[datetime]
    log_size_bytes: int
    is_active: bool
    is_archived: bool
    archive_path: Optional[str]


class PodLogRunFullResponse(PodLogRunMetaResponse):
    logs: Optional[str]


class PodLogRunsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[PodLogRunMetaResponse]
    status: str
    version: str


class PodLogRunResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PodLogRunFullResponse
    status: str
    version: str
