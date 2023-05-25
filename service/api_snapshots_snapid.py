from fastapi import APIRouter
from models_pods import Pod, NewPod, UpdatePod, PodResponse, Password, DeletePodResponse
from models_snapshots import Snapshot, SnapshotResponse, DeleteSnapshotResponse, UpdateSnapshot
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok, error
from volume_utils import files_delete
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods/snapshots/{snapshot_id}

@router.put(
    "/pods/snapshots/{snapshot_id}",
    tags=["Snapshots"],
    summary="update_snapshot",
    operation_id="update_snapshot",
    response_model=SnapshotResponse)
async def update_snapshot(snapshot_id, update_snapshot: UpdateSnapshot):
    """
    Update a snapshot.

    Note:
    - Fields that change snapshot source or sink are not modifiable. Please recreate your snapshot in that case.

    Returns updated snapshot object.
    """
    logger.info(f"UPDATE /pods/snapshots/{snapshot_id} - Top of update_snapshot.")

    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    pre_update_snapshot = snapshot.copy()

    # Snapshot existence is already checked above. Now we validate update and update with values that are set.
    input_data = update_snapshot.dict(exclude_unset=True)
    for key, value in input_data.items():
        setattr(snapshot, key, value)

    # Only update if there's a change
    if snapshot != pre_update_snapshot:
        snapshot.db_update()
    else:
        return error(result=snapshot.display(), msg="Incoming data made no changes to snapshot. Is incoming data equal to current data?")

    return ok(result=snapshot.display(), msg="Snapshot updated successfully.")


@router.delete(
    "/pods/snapshots/{snapshot_id}",
    tags=["Snapshots"],
    summary="delete_snapshot",
    operation_id="delete_snapshot",
    response_model=DeleteSnapshotResponse)
async def delete_snapshot(snapshot_id):
    """
    Delete a snapshot.

    Returns "".
    """
    logger.info(f"DELETE /pods/snapshots/{snapshot_id} - Top of delete_snapshot.")

    # Needs to delete snapshot, nfs folder
    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    # Delete folder
    res = files_delete(
        system_id = conf.nfs_tapis_system_id,
        path = f"/snapshots/{snapshot.snapshot_id}")

    snapshot.db_delete()

    return ok(result="", msg="Snapshot successfully deleted.")


@router.get(
    "/pods/snapshots/{snapshot_id}",
    tags=["Snapshots"],
    summary="get_snapshot",
    operation_id="get_snapshot",
    response_model=SnapshotResponse)
async def get_snapshot(snapshot_id):
    """
    Get a snapshot.

    Returns retrieved snapshot object.
    """
    logger.info(f"GET /pods/snapshots/{snapshot_id} - Top of get_snapshot.")

    # TODO search

    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result=snapshot.display(), msg="Snapshot retrieved successfully.")
