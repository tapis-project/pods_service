from fastapi import APIRouter
from models import Pod, NewPod, UpdatePod
from channels import CommandChannel
from req_utils import g, ok
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


### /pods/{pod_name}
@router.put("/pods/{pod_name}", tags=["pods"])
async def update_pod(pod_name, update_pod: UpdatePod):
    pod = Pod.db_get_DAO(pod_name)

    # Do checks here, ensure pod exists. etc.

    pod = Pod(**update_pod.dict())

    # Do more update things.

    return ok("update_pod - Not yet implemented.")


@router.delete("/pods/{pod_name}", tags=["pods"])
async def delete_pod():
    # This is actually just a stop_pod.
    return ok("delete_pod - Not yet implemented.")


@router.get("/pods/{pod_name}", tags=["pods"])
async def get_pod(pod_name):
    logger.info(f"GET /pods/{pod_name} - Top of get_pods.")

    # TODO .display(), search, permissions

    pod = Pod.db_get_with_pk(pod_name, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result=pod, msg="Pod retrieved successfully.")
