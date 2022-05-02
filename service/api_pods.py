from fastapi import APIRouter
from models import Pod, NewPod, Password
from channels import CommandChannel
from req_utils import g, ok
from codes import REQUESTED
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


### /pods
@router.get("/pods", tags=["pods"])
async def get_pods():
    logger.info("GET /pods - Top of get_pods.")

    # TODO .display(), search, permissions
    pods = Pod.db_get_all(tenant=g.request_tenant_id, site=g.site_id)
    logger.info("Pods retrieved.")
    return ok(result=pods[0], msg="Pods retrieved successfully.")

@router.post("/pods", tags=["pods"])
async def create_pod(new_pod: NewPod):
    logger.info("POST /pods - Top of create_pod.")

    # Create full Pod object. Validates as well.
    pod = Pod(**new_pod.dict())

    # Create pod password db entry. If it's successful, we continue.
    password = Password(pod_name=pod.pod_name)
    password.db_create()
    logger.debug(f"Created password entry for {pod.pod_name}")

    # Create pod database entry
    pod.db_create()
    logger.debug(f"New pod saved in db. pod_name: {pod.pod_name}; database_type: {pod.database_type}; tenant: {g.request_tenant_id}.")

    ### Update status to REQUESTED, probably need to create "ensure_one_worker like fn here"
    pod.status = REQUESTED
    pod.db_update()

    # Send command to start new pod
    ch = CommandChannel(name=pod.site_id)
    ch.put_cmd(pod_name=pod.pod_name,
               tenant_id=pod.tenant_id,
               site_id=pod.site_id)
    ch.close()
    logger.debug(f"Command Channel - Added msg for pod_name: {pod.pod_name}.")

    # TODO Permissions?

    return ok(result=pod, msg="Pod created successfully.")
