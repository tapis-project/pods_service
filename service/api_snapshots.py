from fastapi import APIRouter
from models_snapshots import Snapshot, NewSnapshot, SnapshotsResponse, SnapshotResponse
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from codes import AVAILABLE, CREATING
from volume_utils import files_movecopy
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)


router = APIRouter()


#### /pods/snapshots

@router.get(
    "/pods/snapshots",
    tags=["Snapshots"],
    summary="get_snapshots",
    operation_id="get_snapshots",
    response_model=SnapshotsResponse)
async def get_snapshots():
    """
    Get all snapshots in your respective tenant and site that you have READ or higher access to.

    Returns a list of snapshots.
    """
    logger.info("GET /pod/snapshots - Top of get_snapshots.")

    # TODO search
    snapshots =  Snapshot.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)

    snapshots_to_show = []
    for snapshot in snapshots:
        snapshots_to_show.append(snapshot.display())

    logger.info("Snapshots retrieved.")
    return ok(result=snapshots_to_show, msg="Snapshots retrieved successfully.")


@router.post(
    "/pods/snapshots",
    tags=["Snapshots"],
    summary="create_snapshot",
    operation_id="create_snapshot",
    response_model=SnapshotResponse)
async def create_snapshot(new_snapshot: NewSnapshot):
    """
    Create a snapshot with inputted information.
    
    Notes:
    - Author will be given ADMIN level permissions to the snapshot.

    Returns new snapshot object.
    """
    logger.info("POST /pods/snapshot - Top of create_snapshot.")

    # Create full Snapshot object. Validates as well.
    snapshot = Snapshot(**new_snapshot.dict())

    # Create snapshot database entry
    snapshot.db_create()
    logger.debug(f"New snapshot saved in db. snapshot_id: {snapshot.snapshot_id}; tenant: {g.request_tenant_id}.")

    snapshot.status = CREATING
    snapshot.db_update()
    logger.debug(f"API has updated snapshot status to CREATING")

    ### TODO
    # Move requested files from original folder to snapshot folder
    res = files_movecopy(
        system_id = conf.nfs_tapis_system_id,
        operation = "COPY",
        source_path = f"/volumes/{snapshot.source_volume_id}/{snapshot.source_volume_path}",
        new_path = f"/snapshots/{snapshot.snapshot_id}/{snapshot.destination_path}")

    # If we get to this point we can update snapshot status
    snapshot.status = AVAILABLE
    snapshot.db_update()
    logger.debug(f"API has updated snapshot status to AVAILABLE")

    return ok(result=snapshot.display(), msg="Snapshot created successfully.")
