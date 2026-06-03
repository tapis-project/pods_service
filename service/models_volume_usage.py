"""
VolumeUsageLog — time-series disk-usage measurements for volumes and snapshots.

Written by the health_central loop every N ticks via check_volume_sizes().
Queried by GET /pods/volumes/{id}/usage and GET /pods/snapshots/{id}/usage.
"""
from __future__ import annotations
from typing import Optional, List
from datetime import datetime
from collections import defaultdict

from sqlmodel import SQLModel, Field, select

from stores import pg_store
from tapisservice.logs import get_logger
logger = get_logger(__name__)


class VolumeUsageLog(SQLModel, table=True):
    __tablename__ = "volumeusagelog"

    id: Optional[int] = Field(default=None, primary_key=True)
    object_id: str = Field(..., index=True, description="volume_id or snapshot_id")
    object_type: str = Field("volume", description="'volume' or 'snapshot'")
    tenant_id: str = Field(..., index=True)
    site_id: str = Field(...)
    size_mb: float = Field(0.0, description="Measured size in MB")
    size_limit_mb: Optional[float] = Field(None, description="Size limit in MB at measurement time")
    over_limit: bool = Field(False)
    measured_at: datetime = Field(default_factory=datetime.utcnow, index=True)

    # ── write ─────────────────────────────────────────────────────────────────

    @classmethod
    def log_measurement(
        cls,
        object_id: str,
        object_type: str,
        tenant_id: str,
        site_id: str,
        size_mb: float,
        size_limit_mb: Optional[float] = None,
    ) -> "VolumeUsageLog":
        store = pg_store[site_id][tenant_id]
        over_limit = size_limit_mb is not None and size_mb > size_limit_mb
        entry = cls(
            object_id=object_id,
            object_type=object_type,
            tenant_id=tenant_id,
            site_id=site_id,
            size_mb=round(size_mb, 2),
            size_limit_mb=size_limit_mb,
            over_limit=over_limit,
            measured_at=datetime.utcnow(),
        )
        store.run("add", entry)
        return entry

    # ── read ──────────────────────────────────────────────────────────────────

    @classmethod
    def get_recent(
        cls,
        object_id: str,
        object_type: str,
        tenant_id: str,
        site_id: str,
        limit: int = 100,
    ) -> List["VolumeUsageLog"]:
        store = pg_store[site_id][tenant_id]
        stmt = (
            select(cls)
            .where(cls.object_id == object_id)
            .where(cls.object_type == object_type)
            .where(cls.site_id == site_id)
            .where(cls.tenant_id == tenant_id)
            .order_by(cls.measured_at.desc())
            .limit(limit)
        )
        return store.run("execute", stmt, scalars=True, all=True) or []

    @classmethod
    def get_all_recent(
        cls,
        object_type: str,
        tenant_id: str,
        site_id: str,
        limit_per_object: int = 50,
    ) -> List["VolumeUsageLog"]:
        """Last N measurements for every object of the given type in a tenant."""
        store = pg_store[site_id][tenant_id]
        stmt = (
            select(cls)
            .where(cls.object_type == object_type)
            .where(cls.site_id == site_id)
            .where(cls.tenant_id == tenant_id)
            .order_by(cls.measured_at.desc())
            .limit(limit_per_object * 200)
        )
        all_rows = store.run("execute", stmt, scalars=True, all=True) or []
        grouped: dict = defaultdict(list)
        for row in all_rows:
            if len(grouped[row.object_id]) < limit_per_object:
                grouped[row.object_id].append(row)
        result: List["VolumeUsageLog"] = []
        for rows in grouped.values():
            result.extend(rows)
        return result

    # ── housekeeping ──────────────────────────────────────────────────────────

    @classmethod
    def purge_old(
        cls,
        object_id: str,
        object_type: str,
        tenant_id: str,
        site_id: str,
        keep: int = 500,
    ) -> None:
        """Delete all but the `keep` most recent rows for this object."""
        store = pg_store[site_id][tenant_id]
        rows = cls.get_recent(object_id, object_type, tenant_id, site_id, limit=keep + 200)
        if len(rows) <= keep:
            return
        cutoff_ts = rows[keep - 1].measured_at
        stmt = (
            select(cls)
            .where(cls.object_id == object_id)
            .where(cls.object_type == object_type)
            .where(cls.tenant_id == tenant_id)
            .where(cls.site_id == site_id)
            .where(cls.measured_at < cutoff_ts)
        )
        old = store.run("execute", stmt, scalars=True, all=True) or []
        for row in old:
            store.run("delete", row)

    # ── serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "object_id": self.object_id,
            "object_type": self.object_type,
            "tenant_id": self.tenant_id,
            "site_id": self.site_id,
            "size_mb": self.size_mb,
            "size_limit_mb": self.size_limit_mb,
            "over_limit": self.over_limit,
            "measured_at": self.measured_at.isoformat() if self.measured_at else None,
        }
