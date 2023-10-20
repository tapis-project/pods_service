from fastapi import APIRouter, UploadFile
from models_pods import Pod
from models_volumes import Volume, VolumePermissionsResponse
from models_misc import SetPermission, FilesListResponse, FilesUploadResponse
from volume_utils import files_listfiles, files_insert
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()

#### /pods/volumes/{volume_id}/functionHere

@router.get(
    "/pods/volumes/{volume_id}/list",
    tags=["Volumes"],
    summary="list_volume_files",
    operation_id="list_volume_files",
    response_model=FilesListResponse)
async def list_volume_files(volume_id):
    """
    List files in volume.
    """
    logger.info(f"GET /pods/volumes/{volume_id}/list - Top of list_volume_files.")

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    list_of_files = files_listfiles(
        path = f"/volumes/{volume.volume_id}")
    
    pruned_list_of_files = []
    for file in list_of_files:
        file.pop('group', "")
        file.pop('owner', "")
        pruned_list_of_files.append(file)

    return ok(result=pruned_list_of_files, msg = "Volume file listing retrieved successfully.")


@router.post(
    "/pods/volumes/{volume_id}/upload/{path}",
    tags=["Volumes"],
    summary="upload_to_volume",
    operation_id="upload_to_volume",
    response_model=FilesUploadResponse)
async def upload_to_volume(volume_id, path, file: UploadFile):
    """
    Upload to volume.
    """
    logger.info(f"POST /pods/volumes/{volume_id}/upload/{path} - Top of upload_to_volume.")

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    insert_res = files_insert(
        file = file.file,
        path = f"/volumes/{volume.volume_id}/{path}")

    return ok(result=f"{insert_res}", msg = "Volume file upload successful.")


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
        raise KeyError(f"Operation would result in volume with no users in 'ADMIN' roll. Rolling back.")

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
        raise KeyError(f"Could not find permission for volume with username {user} when deleting permission")

    # Delete permission
    del curr_perms[user]

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise KeyError(f"Operation would result in volume with no users in ADMIN role. Rolling back.")

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")
    
    # Update volume object and commit
    volume.permissions = perm_list
    volume.db_update()

    return ok(result={"permissions": volume.permissions}, msg = "Volume permission deleted successfully.")
