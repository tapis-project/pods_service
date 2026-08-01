from fastapi import APIRouter, UploadFile
from models_pods import Pod
from models_volumes import Volume, VolumePermissionsResponse
from models_misc import SetPermission, FilesListResponse, FilesUploadResponse
from volume_utils import files_listfiles, files_insert, files_download, object_root
from fastapi import Query, Path, File
from fastapi.responses import StreamingResponse
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from tapisservice.logs import get_logger
from errors import ResourceError
logger = get_logger(__name__)

router = APIRouter()

#### usage endpoints — appended below the existing permission endpoints

#### /pods/volumes/{volume_id}/functionHere

@router.get(
    "/pods/volumes/{volume_id}/list",
    tags=["Volumes"],
    summary="list_volume_files",
    operation_id="list_volume_files",
    response_model=FilesListResponse)
async def list_volume_files(volume_id, path: str = Query(default="")):
    """
    List files in volume. Optional ?path= to list a subdirectory (relative to volume root).
    """
    logger.info(f"GET /pods/volumes/{volume_id}/list - Top of list_volume_files.")

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    # base_path is the VOLUME root, not the tenant root — see volume_utils.object_root.
    # ?path=../othervolume must not resolve, and a tenant-base guard would allow it.
    sub = path.strip("/") if path else ""
    list_of_files = files_listfiles(path=sub, base_path=object_root("volumes", volume.volume_id))
    
    pruned_list_of_files = []
    for file in list_of_files:
        file.pop('group', "")
        file.pop('owner', "")
        pruned_list_of_files.append(file)

    return ok(result=pruned_list_of_files, msg = "Volume file listing retrieved successfully.")


@router.get(
    "/pods/volumes/{volume_id}/contents/{path:path}",
    tags=["Volumes"],
    summary="get_volume_contents",
    operation_id="get_volume_contents",
    responses={
        200: {
            "description": "A streamed response of the file contents.",
            "content": {"application/octet-stream": {}, "application/zip": {}}
        }
    }
)
async def get_volume_contents(
        volume_id: str = Path(..., description="Unique identifier for the volume."),
        path: str = Path(..., description="Path relative to the volume's root directory. Cannot be empty or /."),
        zip: bool = Query(default=False, description="If true, directory contents are compressed using ZIP format.")):
    """
    Get file or directory contents as a stream of data from a Tapis Volume.

    Use the **zip** query parameter to request directories as a zip archive. This is not allowed if path would result in all files in the volume being included. Please download individual directories, files or objects.
    """
    logger.info(f"GET /pods/volumes/{volume_id}/contents/{path} - Retrieving contents.")

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    # Validate path to prevent accessing all files on the host
    if not path or path == "/":
        raise ResourceError("Requesting no path or / path is not allowed. Please download individual directories, files or objects.", 400)

    # Call files_download from volume_utils
    file_content, filename = files_download(
        path=path, base_path=object_root("volumes", volume.volume_id),
        zip=zip)
    
    if zip:
        # If zip is True, file_content is a generator for the ZIP file
        return StreamingResponse(
            file_content,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"})
    else:
        # Assuming file_content is a generator for a regular file
        return StreamingResponse(
            file_content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.post(
    "/pods/volumes/{volume_id}/upload/{path:path}",
    tags=["Volumes"],
    summary="upload_to_volume",
    operation_id="upload_to_volume",
    response_model=FilesUploadResponse)
async def upload_to_volume(
        volume_id: str = Path(..., description="Unique identifier for the volume."),
        path: str = Path(..., description="Path within the volume where the file will be uploaded. Cannot be empty or /."),
        file: UploadFile = File(..., description="The file to upload.")):
    """
    Upload to volume.
    """
    logger.info(f"POST /pods/volumes/{volume_id}/upload/{path} - Top of upload_to_volume.")

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    insert_res = files_insert(
        file = file.file,
        path=path, base_path=object_root("volumes", volume.volume_id))

    return ok(result=f"{insert_res}", msg = "Volume file upload successful.")


@router.get(
    "/pods/volumes/{volume_id}/download/{path:path}",
    tags=["Volumes"],
    summary="download_volume_file",
    operation_id="download_volume_file",
    responses={
        200: {
            "description": "A streamed response of the file contents.",
            "content": {"application/octet-stream": {}}
        }
    }
)
async def download_volume_file(
        volume_id: str = Path(..., description="Unique identifier for the volume."),
        path: str = Path(..., description="Path to the file relative to the volume's root directory. Cannot be empty or /.")):
    """
    Download a specific file from a Tapis Volume.
    
    Efficiently handles large files (100MB - 10GB) from NFS-backed storage by streaming in chunks.
    
    Note:
    - This endpoint is for downloading individual files
    - For directories, use get_volume_contents with zip=true
    - Path cannot be empty or / to prevent downloading entire volume
    """
    logger.info(f"GET /pods/volumes/{volume_id}/download/{path} - Downloading file.")

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    # Validate path to prevent accessing all files
    if not path or path == "/":
        raise ResourceError("Requesting no path or / path is not allowed. Please specify a file path.", 400)

    # Call files_download from volume_utils (without zip for single file)
    file_content, filename = files_download(
        path=path, base_path=object_root("volumes", volume.volume_id),
        zip=False)
    
    # Extract just the filename for cleaner download name
    clean_filename = filename.split('/')[-1] if '/' in filename else filename
    
    return StreamingResponse(
        file_content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename={clean_filename}",
            "X-Volume-Id": volume.volume_id,
            "X-Source-Path": path
        })


@router.get(
    "/pods/volumes/{volume_id}/permissions",
    tags=["Permissions"],
    summary="get_volume_permissions",
    operation_id="get_volume_permissions",
    response_model=VolumePermissionsResponse)
async def get_volume_permissions(volume_id):
    """
    Get a volumes permissions.

    Note:
    - There are 3 levels of permissions, READ, USER, and ADMIN.
    - Permissions are granted/revoked to individual TACC usernames.

    Returns all volue permissions.
    """
    logger.info(f"GET /pods/volumes/{volume_id}/permissions - Top of get_volume_permissions.")

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result={"permissions": volume.permissions}, msg = "Volume permissions retrieved successfully.")


@router.post(
    "/pods/volumes/{volume_id}/permissions",
    tags=["Permissions"],
    summary="set_volume_permission",
    operation_id="set_volume_permission",
    response_model=VolumePermissionsResponse)
async def set_volume_permission(volume_id, set_permission: SetPermission):
    """
    Set a permission for a volume.

    Returns updated volume permissions.
    """
    logger.info(f"POST /pods/volumes/{volume_id}/permissions - Top of set_volume_permissions.")

    inp_user = set_permission.user
    inp_level = set_permission.level

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    # Get formatted perms
    curr_perms = volume.get_permissions()

    # Update variable
    curr_perms[inp_user] = inp_level

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise ResourceError("Operation would leave the volume with no ADMIN-capable user. Rolling back.", 400)

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")

    # Update volume object and commit
    volume.permissions = perm_list
    volume.db_update()

    return ok(result={"permissions": volume.permissions}, msg = "Volume permissions updated successfully.")


@router.delete(
    "/pods/volumes/{volume_id}/permissions/{user}",
    tags=["Permissions"],
    summary="delete_volume_permission",
    operation_id="delete_volume_permission",
    response_model=VolumePermissionsResponse)
async def delete_volume_permission(volume_id, user):
    """
    Delete a permission from a volume.

    Returns updated volume permissions.
    """
    logger.info(f"DELETE /pods/volumes/{volume_id}/permissions/{user} - Top of delete_volume_permission.")

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    # Get formatted perms
    curr_perms = volume.get_permissions()

    if user not in curr_perms.keys():
        raise ResourceError(f"Could not find permission for volume with username {user} when deleting permission.", 404)

    # Delete permission
    del curr_perms[user]

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise ResourceError("Operation would leave the volume with no ADMIN-capable user. Rolling back.", 400)

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")
    
    # Update volume object and commit
    volume.permissions = perm_list
    volume.db_update()

    return ok(result={"permissions": volume.permissions}, msg = "Volume permission deleted successfully.")


@router.get(
    "/pods/volumes/{volume_id}/usage",
    tags=["Volumes"],
    summary="get_volume_usage",
    operation_id="get_volume_usage")
async def get_volume_usage(
    volume_id,
    limit: int = Query(default=100, ge=1, le=1000, description="Max measurements to return (newest first)."),
):
    """
    Get disk-usage history for a volume.

    Returns the last `limit` size measurements recorded by the health loop,
    newest first. The `over_limit` flag is set when `size_mb > size_limit_mb`
    at measurement time. No enforcement is applied — informational only.
    """
    logger.info(f"GET /pods/volumes/{volume_id}/usage - Top of get_volume_usage.")
    Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    from models_volume_usage import VolumeUsageLog
    logs = VolumeUsageLog.get_recent(volume_id, "volume", g.request_tenant_id, g.site_id, limit=limit)
    return ok(result=[l.to_dict() for l in logs], msg="Volume usage history retrieved.")


@router.get(
    "/pods/volumes/usage",
    tags=["Volumes"],
    summary="list_volumes_usage",
    operation_id="list_volumes_usage")
async def list_volumes_usage(
    limit_per: int = Query(default=50, ge=1, le=500, description="Max measurements per volume."),
):
    """
    Get recent disk-usage history for all volumes owned by this tenant.

    Returns measurements grouped by volume_id. Use `limit_per` to control
    how many time points you get per volume (default 50).
    """
    logger.info(f"GET /pods/volumes/usage - Top of list_volumes_usage.")
    from models_volume_usage import VolumeUsageLog
    logs = VolumeUsageLog.get_all_recent("volume", g.request_tenant_id, g.site_id, limit_per_object=limit_per)

    # Freshness stanza (same metadata.warnings pattern as GET /pods): tells the
    # UI which objects the sweep hasn't measured, without failing the request.
    metadata: dict = {}
    try:
        volumes = Volume.db_get_all(tenant=g.request_tenant_id, site=g.site_id)
        measured_ids = {l.object_id for l in logs}
        unmeasured = [v.volume_id for v in volumes if v.volume_id not in measured_ids]
        latest = max((l.measured_at for l in logs), default=None)
        metadata["last_measured_at"] = latest.isoformat() + "Z" if latest else None
        metadata["unmeasured"] = unmeasured
        if unmeasured:
            metadata["warnings"] = [
                f"{len(unmeasured)} volume(s) have no size measurements yet (the du sweep "
                f"runs every ~10 min; if this persists check admin health 'volume_sizes'): "
                + ", ".join(unmeasured[:10]) + ("…" if len(unmeasured) > 10 else "")]
    except Exception as e:
        logger.warning(f"list_volumes_usage freshness metadata failed: {e}")

    return ok(result=[l.to_dict() for l in logs], metadata=metadata, msg="Volume usage history retrieved.")
