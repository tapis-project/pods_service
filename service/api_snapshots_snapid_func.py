from fastapi import APIRouter
from models_pods import Pod
from models_snapshots import Snapshot, SnapshotPermissionsResponse
from models_misc import SetPermission, FilesListResponse
from volume_utils import files_listfiles
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()

#### /pods/snapshots/{snapshot_id}/functionHere

@router.get(
    "/pods/snapshots/{snapshot_id}/list",
    tags=["Snapshots"],
    summary="list_snapshot_files",
    operation_id="list_snapshot_files",
    response_model=FilesListResponse)
async def list_snapshot_files(snapshot_id):
    """
    List files in snapshot.
    """
    logger.info(f"GET /pods/snapshots/{snapshot_id}/list - Top of list_snapshot_files.")

    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    list_of_files = files_listfiles(
        system_id = conf.nfs_tapis_system_id,
        path = f"/snapshots/{snapshot.snapshot_id}/")
    
    pruned_list_of_files = []
    for file in list_of_files:
        file = file.__dict__
        file.pop('group', "")
        file.pop('owner', "")
        file.pop('mimeType', "")
        file.pop('url', "")
        pruned_list_of_files.append(file)

    return ok(result=pruned_list_of_files, msg = "Snapshot file listing retrieved successfully.")


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
        raise KeyError(f"Operation would result in snapshot with no users in 'ADMIN' roll. Rolling back.")

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
        raise KeyError(f"Could not find permission for snapshot with username {user} when deleting permission")

    # Delete permission
    del curr_perms[user]

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise KeyError(f"Operation would result in snapshot with no users in ADMIN role. Rolling back.")

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")
    
    # Update snapshot object and commit
    snapshot.permissions = perm_list
    snapshot.db_update()

    return ok(result={"permissions": snapshot.permissions}, msg = "Snapshot permission deleted successfully.")
