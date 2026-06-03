"""
Template Gallery API — photos (up to 3) + markdown note per template.

GET    /pods/templates/{template_id}/gallery          → metadata (note + photo slots)
GET    /pods/templates/{template_id}/gallery/photos/{n} → raw photo bytes
PUT    /pods/templates/{template_id}/gallery/photos/{n} → upload photo (admin only)
DELETE /pods/templates/{template_id}/gallery/photos/{n} → delete photo (admin only)
PUT    /pods/templates/{template_id}/gallery/note       → update note (admin only)
"""
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse, Response
import io

from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.logs import get_logger

from models_template_gallery import TemplateGallery

logger = get_logger(__name__)
router = APIRouter()

_ALLOWED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAX_BYTES    = 5 * 1024 * 1024  # 5 MB per photo


def _require_gallery_admin():
    """Only site admins may modify template galleries. g.admin already covers the
    pods_admin SK role, and (in dev) local_admin_usernames — so no username hardcode."""
    if not getattr(g, 'admin', False):
        raise HTTPException(status_code=403, detail="Gallery uploads are restricted to administrators.")


def _get_gallery(template_id: str) -> TemplateGallery:
    return TemplateGallery.db_get_or_create(
        template_id=template_id,
        tenant=g.request_tenant_id,
        site=g.site_id,
    )


# ── GET gallery metadata ─────────────────────────────────────────

@router.get(
    "/pods/templates/{template_id}/gallery",
    tags=["Templates"],
    summary="get_template_gallery",
    operation_id="get_template_gallery",
)
def get_template_gallery(template_id: str):
    gallery = TemplateGallery.db_get(
        template_id=template_id,
        tenant=g.request_tenant_id,
        site=g.site_id,
    )
    if gallery is None:
        # Return empty gallery rather than 404 — template exists, gallery is just empty
        return ok(result={"template_id": template_id, "markdown_note": None, "photos": [], "updated_at": None})
    return ok(result=gallery.to_meta_dict())


# ── GET individual photo ─────────────────────────────────────────

@router.get(
    "/pods/templates/{template_id}/gallery/photos/{n}",
    tags=["Templates"],
    summary="get_template_gallery_photo",
    operation_id="get_template_gallery_photo",
)
def get_template_gallery_photo(template_id: str, n: int):
    if n not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="Photo slot n must be 1, 2, or 3.")
    gallery = TemplateGallery.db_get(
        template_id=template_id,
        tenant=g.request_tenant_id,
        site=g.site_id,
    )
    if gallery is None:
        raise HTTPException(status_code=404, detail="Gallery not found.")
    data, mime = gallery.get_photo(n)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Photo slot {n} is empty.")
    return StreamingResponse(io.BytesIO(data), media_type=mime or "image/png")


# ── PUT upload photo (admin only) ────────────────────────────────

@router.put(
    "/pods/templates/{template_id}/gallery/photos/{n}",
    tags=["Templates"],
    summary="upload_template_gallery_photo",
    operation_id="upload_template_gallery_photo",
)
async def upload_template_gallery_photo(
    template_id: str,
    n: int,
    file: UploadFile = File(..., description="Photo to upload (PNG, JPEG, GIF, WEBP, max 5 MB)."),
):
    _require_gallery_admin()
    if n not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="Photo slot n must be 1, 2, or 3.")

    mime = file.content_type or "image/png"
    if mime not in _ALLOWED_MIME:
        raise HTTPException(status_code=400, detail=f"Unsupported image type '{mime}'. Allowed: {sorted(_ALLOWED_MIME)}")

    data = await file.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Photo exceeds 5 MB limit ({len(data)//1024} KB uploaded).")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    gallery = _get_gallery(template_id)
    gallery.set_photo(n, data, mime)
    gallery.db_save(tenant=g.request_tenant_id, site=g.site_id)

    logger.info(f"gallery: {g.username} uploaded photo {n} for template {template_id} ({len(data)//1024} KB, {mime})")
    return ok(result=gallery.to_meta_dict(), msg=f"Photo {n} uploaded successfully.")


# ── DELETE photo (admin only) ────────────────────────────────────

@router.delete(
    "/pods/templates/{template_id}/gallery/photos/{n}",
    tags=["Templates"],
    summary="delete_template_gallery_photo",
    operation_id="delete_template_gallery_photo",
)
def delete_template_gallery_photo(template_id: str, n: int):
    _require_gallery_admin()
    if n not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="Photo slot n must be 1, 2, or 3.")

    gallery = TemplateGallery.db_get(
        template_id=template_id,
        tenant=g.request_tenant_id,
        site=g.site_id,
    )
    if gallery is None:
        raise HTTPException(status_code=404, detail="Gallery not found.")

    gallery.clear_photo(n)
    gallery.db_save(tenant=g.request_tenant_id, site=g.site_id)

    logger.info(f"gallery: {g.username} deleted photo {n} for template {template_id}")
    return ok(result=gallery.to_meta_dict(), msg=f"Photo {n} deleted.")


# ── PUT markdown note (admin only) ───────────────────────────────

@router.put(
    "/pods/templates/{template_id}/gallery/note",
    tags=["Templates"],
    summary="update_template_gallery_note",
    operation_id="update_template_gallery_note",
)
async def update_template_gallery_note(template_id: str, request_body: dict):
    _require_gallery_admin()
    # `.get("note", "")` returns None when the JSON body has "note": null, which would
    # crash len() with a 500 — coerce null/missing to "".
    note = request_body.get("note") or ""
    if len(note) > 4000:
        raise HTTPException(status_code=400, detail="Note exceeds 4000 character limit.")

    gallery = _get_gallery(template_id)
    gallery.markdown_note = note or None
    gallery.db_save(tenant=g.request_tenant_id, site=g.site_id)

    logger.info(f"gallery: {g.username} updated note for template {template_id}")
    return ok(result=gallery.to_meta_dict(), msg="Note updated.")
