from fastapi import APIRouter
from models_pods import Pod, NewPod, Password, PodsResponse, PodResponse
from models_volumes import Volume, NewVolume, VolumesResponse, VolumeResponse
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from codes import AVAILABLE, CREATING
from volume_utils import files_mkdir
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)


router = APIRouter()


#### /pods/volumes

@router.get(
    "/pods/volumes",
    tags=["Volumes"],
    summary="get_volumes",
    operation_id="get_volumes",
    response_model=VolumesResponse)
async def get_volumes():
    """
    Get all volumes in your respective tenant and site that you have READ or higher access to.

    Returns a list of volumes.
    """
    logger.info("GET /pod/volumes - Top of get_volumes.")

    # TODO search
    volumes =  Volume.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)

    volumes_to_show = []
    for volume in volumes:
        volumes_to_show.append(volume.display())

    logger.info("Volumes retrieved.")
    return ok(result=volumes_to_show, msg="Volumes retrieved successfully.")


@router.post(
    "/pods/volumes",
    tags=["Volumes"],
    summary="create_volume",
    operation_id="create_volume",
    response_model=VolumeResponse)
async def create_volume(new_volume: NewVolume):
    """
    Create a volume with inputted information.
    
    Notes:
    - Author will be given ADMIN level permissions to the volume.
    - status_requested defaults to "ON". So volume will immediately begin creation.

    Returns new volume object.
    """
    logger.info("POST /pods/volume - Top of create_volume.")

    # Create full Volume object. Validates as well.
    volume = Volume(**new_volume.dict())

    # Create volume database entry
    volume.db_create()
    logger.debug(f"New volume saved in db. volume_id: {volume.volume_id}; tenant: {g.request_tenant_id}.")

    volume.status = CREATING
    volume.db_update()
    logger.debug(f"API has updated volume status to CREATING")

    # Create folder
    res = files_mkdir(
        system_id = conf.nfs_tapis_system_id,
        path = f"/volumes/{volume.volume_id}")

    # If we get to this point we can update pod status
    volume.status = AVAILABLE
    volume.db_update()
    logger.debug(f"API has updated volume status to AVAILABLE")

    return ok(result=volume.display(), msg="Volume created successfully.")
