from fastapi import APIRouter
from models_pods import Pod, NewPod, UpdatePod, PodResponse, Password, DeletePodResponse
from models_volumes import Volume, VolumeResponse, DeleteVolumeResponse, UpdateVolume
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok, error
from volume_utils import files_delete
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods/volumes/{volume_id}

@router.put(
    "/pods/volumes/{volume_id}",
    tags=["Volumes"],
    summary="update_volume",
    operation_id="update_volume",
    response_model=VolumeResponse)
async def update_volume(volume_id, update_volume: UpdateVolume):
    """
    Update a volume.

    Note:
    - Fields that change volume source or sink are not modifiable. Please recreate your volume in that case.

    Returns updated volume object.
    """
    logger.info(f"UPDATE /pods/volumes/{volume_id} - Top of update_volume.")

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    pre_update_volume = volume.copy()

    # Volume existence is already checked above. Now we validate update and update with values that are set.
    input_data = update_volume.dict(exclude_unset=True)
    for key, value in input_data.items():
        setattr(volume, key, value)

    # Only update if there's a change
    if volume != pre_update_volume:
        volume.db_update()
    else:
        return error(result=volume.display(), msg="Incoming data made no changes to volume. Is incoming data equal to current data?")

    return ok(result=volume.display(), msg="Volume updated successfully.")


@router.delete(
    "/pods/volumes/{volume_id}",
    tags=["Volumes"],
    summary="delete_volume",
    operation_id="delete_volume",
    response_model=DeleteVolumeResponse)
async def delete_volume(volume_id):
    """
    Delete a volume.

    Returns "".
    """
    logger.info(f"DELETE /pods/volumes/{volume_id} - Top of delete_volume.")

    # Needs to delete volume, nfs folder
    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    # Delete folder
    res = files_delete(
        path = f"/volumes/{volume.volume_id}")

    volume.db_delete()

    return ok(result="", msg="Volume successfully deleted.")


@router.get(
    "/pods/volumes/{volume_id}",
    tags=["Volumes"],
    summary="get_volume",
    operation_id="get_volume",
    response_model=VolumeResponse)
async def get_volume(volume_id):
    """
    Get a volume.

    Returns retrieved volume object.
    """
    logger.info(f"GET /pods/volumes/{volume_id} - Top of get_volume.")

    # TODO search

    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result=volume.display(), msg="Volume retrieved successfully.")
