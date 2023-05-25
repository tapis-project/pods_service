from fastapi import APIRouter
from models_pods import Pod, NewPod, Password, PodsResponse, PodResponse
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok
from codes import REQUESTED, ON
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods

@router.get(
    "/pods",
    tags=["Pods"],
    summary="get_pods",
    operation_id="get_pods",
    response_model=PodsResponse)
async def get_pods():
    """
    Get all pods in your respective tenant and site that you have READ or higher access to.

    Returns a list of pods.
    """
    logger.info("GET /pods - Top of get_pods.")

    # TODO search
    pods =  Pod.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)

    pods_to_show = []
    for pod in pods:
        pods_to_show.append(pod.display())

    logger.info("Pods retrieved.")
    return ok(result=pods_to_show, msg="Pods retrieved successfully.")


@router.post(
    "/pods",
    tags=["Pods"],
    summary="create_pod",
    operation_id="create_pod",
    response_model=PodResponse)
async def create_pod(new_pod: NewPod):
    """
    Create a pod with inputted information.
    
    Notes:
    - Author will be given ADMIN level permissions to the pod.
    - status_requested defaults to "ON". So pod will immediately begin creation.

    Returns new pod object.
    """
    logger.info("POST /pods - Top of create_pod.")

    # Create full Pod object. Validates as well.
    pod = Pod(**new_pod.dict())

    # Create pod password db entry. If it's successful, we continue.
    password = Password(pod_id=pod.pod_id)
    password.db_create()
    logger.debug(f"Created password entry for {pod.pod_id}")

    # Create pod database entry
    pod.db_create()
    logger.debug(f"New pod saved in db. pod_id: {pod.pod_id}; pod_template: {pod.pod_template}; tenant: {g.request_tenant_id}.")

    # If status_requested = On, then we request pod and put a command. Else leave in default STOPPED state. 
    if pod.status_requested == ON:
        pod.status = REQUESTED
        pod.db_update()

        # Send command to start new pod
        ch = CommandChannel(name=pod.site_id)
        ch.put_cmd(object_id=pod.pod_id,
                   object_type="pod",
                   tenant_id=pod.tenant_id,
                   site_id=pod.site_id)
        ch.close()
        logger.debug(f"Command Channel - Added msg for pod_id: {pod.pod_id}.")

    return ok(result=pod.display(), msg="Pod created successfully.")
