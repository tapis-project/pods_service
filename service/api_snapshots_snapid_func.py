from fastapi import APIRouter
from models_pods import Pod
from models_snapshots import Snapshot, SnapshotPermissionsResponse
from models_misc import SetPermission, FilesListResponse
from volume_utils import files_listfiles, files_insert, files_download
from fastapi import Query, Path, File
from fastapi.responses import StreamingResponse
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from tapisservice.logs import get_logger
from errors import ResourceError
logger = get_logger(__name__)

router = APIRouter()

#### /pods/snapshots/{snapshot_id}/functionHere

@router.get(
    "/pods/snapshots/{snapshot_id}/list",
    tags=["Snapshots"],
    summary="list_snapshot_files",
    operation_id="list_snapshot_files",
    response_model=FilesListResponse)
async def list_snapshot_files(snapshot_id, path: str = Query(default="")):
    """
    List files in snapshot. Optional ?path= to list a subdirectory (relative to snapshot root).
    """
    logger.info(f"GET /pods/snapshots/{snapshot_id}/list - Top of list_snapshot_files.")

    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    sub = path.strip("/") if path else ""
    full_path = f"/snapshots/{snapshot.snapshot_id}/{sub}" if sub else f"/snapshots/{snapshot.snapshot_id}/"
    list_of_files = files_listfiles(path=full_path)
    
    pruned_list_of_files = []
    for file in list_of_files:
        file.pop('group', "")
        file.pop('owner', "")
        pruned_list_of_files.append(file)

    return ok(result=pruned_list_of_files, msg = "Snapshot file listing retrieved successfully.")


@router.get(
    "/pods/snapshots/{snapshot_id}/contents/{path:path}",
    tags=["Snapshots"],
    summary="get_snapshot_contents",
    operation_id="get_snapshot_contents",
    responses={
        200: {
            "description": "A streamed response of the file contents.",
            "content": {"application/octet-stream": {}, "application/zip": {}}
        }
    }
)
async def get_snapshot_contents(
        snapshot_id: str = Path(..., description="Unique identifier for the snapshot."),
        path: str = Path(..., description="Path relative to the snapshot's root directory. Cannot be empty or /."),
        zip: bool = Query(default=False, description="If true, directory contents are compressed using ZIP format.")):
    """
    Get file or directory contents as a stream of data from a Tapis Snapshot.

    Use the **zip** query parameter to request directories as a zip archive. This is not allowed if path would result in all files in the snapshot being included. Please download individual directories, files or objects.
    """
    logger.info(f"GET /pods/snapshots/{snapshot_id}/contents/{path} - Retrieving contents.")

    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    # Validate path to prevent accessing all files on the host
    if not path or path == "/":
        raise ResourceError("Requesting no path or / path is not allowed. Please download individual directories, files or objects.", 400)

    # Call files_download from snapshot_utils
    file_content, filename = files_download(
        path = f"/snapshots/{snapshot.snapshot_id}/{path}",
        zip=zip)
    
    if zip:
        # If zip is True, file_content is a generator for the ZIP file
        return StreamingResponse(file_content, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename={filename}"})
    else:
        # Assuming file_content is a generator for a regular file
        return StreamingResponse(file_content, media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get(
    "/pods/snapshots/{snapshot_id}/download/{path:path}",
    tags=["Snapshots"],
    summary="download_snapshot_file",
    operation_id="download_snapshot_file",
    responses={
        200: {
            "description": "A streamed response of the file contents.",
            "content": {"application/octet-stream": {}}
        }
    }
)
async def download_snapshot_file(
        snapshot_id: str = Path(..., description="Unique identifier for the snapshot."),
        path: str = Path(..., description="Path to the file relative to the snapshot's root directory. Cannot be empty or /.")):
    """
    Download a specific file from a Tapis Snapshot.
    
    Efficiently handles large files (100MB - 10GB) from NFS-backed storage by streaming in chunks.
    
    Note:
    - This endpoint is for downloading individual files
    - For directories, use get_snapshot_contents with zip=true
    - Path cannot be empty or / to prevent downloading entire snapshot
    """
    logger.info(f"GET /pods/snapshots/{snapshot_id}/download/{path} - Downloading file.")

    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    # Validate path to prevent accessing all files
    if not path or path == "/":
        raise ResourceError("Requesting no path or / path is not allowed. Please specify a file path.", 400)

    # Call files_download from volume_utils (without zip for single file)
    file_content, filename = files_download(
        path=f"/snapshots/{snapshot.snapshot_id}/{path}",
        zip=False)
    
    # Extract just the filename for cleaner download name
    clean_filename = filename.split('/')[-1] if '/' in filename else filename
    
    return StreamingResponse(
        file_content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename={clean_filename}",
            "X-Snapshot-Id": snapshot.snapshot_id,
            "X-Source-Path": path
        })


@router.get(
    "/pods/snapshots/{snapshot_id}/permissions",
    tags=["Permissions"],
    summary="get_snapshot_permissions",
    operation_id="get_snapshot_permissions",
    response_model=SnapshotPermissionsResponse)
async def get_snapshot_permissions(snapshot_id):
    """
    Get a snapshots permissions.

    Note:
    - There are 3 levels of permissions, READ, USER, and ADMIN.
    - Permissions are granted/revoked to individual TACC usernames.

    Returns all volue permissions.
    """
    logger.info(f"GET /pods/snapshots/{snapshot_id}/permissions - Top of get_snapshot_permissions.")

    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result={"permissions": snapshot.permissions}, msg = "Snapshot permissions retrieved successfully.")


@router.post(
    "/pods/snapshots/{snapshot_id}/permissions",
    tags=["Permissions"],
    summary="set_snapshot_permission",
    operation_id="set_snapshot_permission",
    response_model=SnapshotPermissionsResponse)
async def set_snapshot_permission(snapshot_id, set_permission: SetPermission):
    """
    Set a permission for a snapshot.

    Returns updated snapshot permissions.
    """
    logger.info(f"POST /pods/snapshots/{snapshot_id}/permissions - Top of set_snapshot_permissions.")

    inp_user = set_permission.user
    inp_level = set_permission.level

    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    # Get formatted perms
    curr_perms = snapshot.get_permissions()

    # Update variable
    curr_perms[inp_user] = inp_level

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise ResourceError("Operation would leave the snapshot with no ADMIN-capable user. Rolling back.", 400)

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")

    # Update snapshot object and commit
    snapshot.permissions = perm_list
    snapshot.db_update()

    return ok(result={"permissions": snapshot.permissions}, msg = "Snapshot permissions updated successfully.")


@router.delete(
    "/pods/snapshots/{snapshot_id}/permissions/{user}",
    tags=["Permissions"],
    summary="delete_snapshot_permission",
    operation_id="delete_snapshot_permission",
    response_model=SnapshotPermissionsResponse)
async def delete_snapshot_permission(snapshot_id, user):
    """
    Delete a permission from a snapshot.

    Returns updated snapshot permissions.
    """
    logger.info(f"DELETE /pods/snapshots/{snapshot_id}/permissions/{user} - Top of delete_snapshot_permission.")

    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    # Get formatted perms
    curr_perms = snapshot.get_permissions()

    if user not in curr_perms.keys():
        raise ResourceError(f"Could not find permission for snapshot with username {user} when deleting permission.", 404)

    # Delete permission
    del curr_perms[user]

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise ResourceError("Operation would leave the snapshot with no ADMIN-capable user. Rolling back.", 400)

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")
    
    # Update snapshot object and commit
    snapshot.permissions = perm_list
    snapshot.db_update()

    return ok(result={"permissions": snapshot.permissions}, msg = "Snapshot permission deleted successfully.")


@router.get(
    "/pods/snapshots/{snapshot_id}/usage",
    tags=["Snapshots"],
    summary="get_snapshot_usage",
    operation_id="get_snapshot_usage")
async def get_snapshot_usage(
    snapshot_id,
    limit: int = Query(default=100, ge=1, le=1000, description="Max measurements to return (newest first)."),
):
    """
    Get disk-usage history for a snapshot. Returns the last `limit` measurements,
    newest first. The `over_limit` flag is informational — no enforcement applied.
    """
    logger.info(f"GET /pods/snapshots/{snapshot_id}/usage - Top of get_snapshot_usage.")
    Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    from models_volume_usage import VolumeUsageLog
    logs = VolumeUsageLog.get_recent(snapshot_id, "snapshot", g.request_tenant_id, g.site_id, limit=limit)
    return ok(result=[l.to_dict() for l in logs], msg="Snapshot usage history retrieved.")


@router.get(
    "/pods/snapshots/usage",
    tags=["Snapshots"],
    summary="list_snapshots_usage",
    operation_id="list_snapshots_usage")
async def list_snapshots_usage(
    limit_per: int = Query(default=50, ge=1, le=500, description="Max measurements per snapshot."),
):
    """
    Get recent disk-usage history for all snapshots in this tenant.
    """
    logger.info(f"GET /pods/snapshots/usage - Top of list_snapshots_usage.")
    from models_volume_usage import VolumeUsageLog
    logs = VolumeUsageLog.get_all_recent("snapshot", g.request_tenant_id, g.site_id, limit_per_object=limit_per)
    return ok(result=[l.to_dict() for l in logs], msg="Snapshot usage history retrieved.")
