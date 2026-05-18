"""
TemplateGallery — up to 3 photos + a markdown note per template.

Photos are stored as raw bytes in the DB (BYTEA in Postgres).
The note is a markdown string. Both are tenant-scoped.

Endpoints: GET/PUT /pods/templates/{id}/gallery
           GET/PUT/DELETE /pods/templates/{id}/gallery/photos/{n}  (n = 1,2,3)
"""
from __future__ import annotations
from typing import Optional
from datetime import datetime

import sqlalchemy as sa
from sqlmodel import SQLModel, Field, select, Column

from stores import pg_store
from tapisservice.logs import get_logger
logger = get_logger(__name__)


class TemplateGallery(SQLModel, table=True):
    __tablename__ = "templategallery"

    id: Optional[int]     = Field(default=None, primary_key=True)
    template_id: str      = Field(..., index=True)
    tenant_id: str        = Field(..., index=True)
    site_id: str          = Field(...)
    markdown_note: Optional[str] = Field(None)

    photo_1:      Optional[bytes] = Field(default=None, sa_column=Column(sa.LargeBinary, nullable=True))
    photo_1_mime: Optional[str]   = Field(None)
    photo_2:      Optional[bytes] = Field(default=None, sa_column=Column(sa.LargeBinary, nullable=True))
    photo_2_mime: Optional[str]   = Field(None)
    photo_3:      Optional[bytes] = Field(default=None, sa_column=Column(sa.LargeBinary, nullable=True))
    photo_3_mime: Optional[str]   = Field(None)

    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # ── class methods ───────────────────────────────────────────────

    @classmethod
    def db_get(cls, template_id: str, tenant: str, site: str) -> Optional["TemplateGallery"]:
        store = pg_store[site][tenant]
        stmt  = select(cls).where(cls.template_id == template_id, cls.tenant_id == tenant)
        rows  = store.run("execute", stmt, scalars=True, all=True)
        return rows[0] if rows else None

    @classmethod
    def db_get_or_create(cls, template_id: str, tenant: str, site: str) -> "TemplateGallery":
        existing = cls.db_get(template_id, tenant, site)
        if existing:
            return existing
        gallery = cls(template_id=template_id, tenant_id=tenant, site_id=site)
        store = pg_store[site][tenant]
        store.run("add", gallery)
        return gallery

    def db_save(self, tenant: str, site: str) -> None:
        self.updated_at = datetime.utcnow()
        store = pg_store[site][tenant]
        store.run("add", self)

    # ── helpers ─────────────────────────────────────────────────────

    def photo_slots(self) -> list[int]:
        """Return list of slot numbers (1-3) that have a photo."""
        slots = []
        if self.photo_1: slots.append(1)
        if self.photo_2: slots.append(2)
        if self.photo_3: slots.append(3)
        return slots

    def get_photo(self, n: int) -> tuple[Optional[bytes], Optional[str]]:
        """Return (bytes, mime) for slot n (1-3)."""
        if n == 1: return self.photo_1, self.photo_1_mime
        if n == 2: return self.photo_2, self.photo_2_mime
        if n == 3: return self.photo_3, self.photo_3_mime
        return None, None

    def set_photo(self, n: int, data: bytes, mime: str) -> None:
        if n == 1: self.photo_1, self.photo_1_mime = data, mime
        elif n == 2: self.photo_2, self.photo_2_mime = data, mime
        elif n == 3: self.photo_3, self.photo_3_mime = data, mime

    def clear_photo(self, n: int) -> None:
        if n == 1: self.photo_1 = None; self.photo_1_mime = None
        elif n == 2: self.photo_2 = None; self.photo_2_mime = None
        elif n == 3: self.photo_3 = None; self.photo_3_mime = None

    def to_meta_dict(self) -> dict:
        """Metadata dict (no photo bytes) returned by the gallery GET endpoint."""
        return {
            "template_id":    self.template_id,
            "markdown_note":  self.markdown_note,
            "photos":         self.photo_slots(),
            "updated_at":     self.updated_at.isoformat() if self.updated_at else None,
        }
