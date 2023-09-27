from fastapi import APIRouter
from models_pods import Pod, UpdatePod, PodResponse, Password, DeletePodResponse
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok, error

from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods/{pod_id}

@router.put(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="update_pod",
    operation_id="update_pod",
    response_model=PodResponse)
async def update_pod(pod_id, update_pod: UpdatePod):
    """
    Update a pod.

    Note:
    - Pod will not be restarted, you must restart the pod for any pod-related changes to proliferate.

    Returns updated pod object.
    """
    logger.info(f"UPDATE /pods/{pod_id} - Top of update_pod.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    
    pre_update_pod = pod.dict().copy()

    # Pod existence is already checked above. Now we validate update and update with values that are set.
    input_data = update_pod.dict(exclude_unset=True)
    for key, value in input_data.items():
        setattr(pod, key, value)

    post_update_pod = pod.dict().copy()

    # Only update if there's a change
    if pod != pre_update_pod:
        updated_fields = {key: post_update_pod[key] for key in post_update_pod if key in pre_update_pod and post_update_pod[key] != pre_update_pod[key]}
        pod.db_update(f"'{g.username}' updated pod, updated_fields: {updated_fields}")
    else:
        return error(result=pod.display(), msg="Incoming data made no changes to pod. Is incoming data equal to current data?")
        
    return ok(
        result=pod.display(),
        msg="Pod updated successfully.",
        metadata={"note":("Pod will require restart when updating command, environment_variables,",
                          "status_requested, volume_mounts, networking, or resources.")})


@router.delete(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="delete_pod",
    operation_id="delete_pod",
    response_model=DeletePodResponse)
async def delete_pod(pod_id):
    """
    Delete a pod.

    Returns "".
    """
    logger.info(f"DELETE /pods/{pod_id} - Top of delete_pod.")

    # Needs to delete pod, service, db_pod, db_password
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    password = Password.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    pod.db_delete()
    password.db_delete()

    return ok(result="", msg="Pod successfully deleted.")


@router.get(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="get_pod",
    operation_id="get_pod",
    response_model=PodResponse)
async def get_pod(pod_id):
    """
    Get a pod.

    Returns retrieved pod object.
    """
    logger.info(f"GET /pods/{pod_id} - Top of get_pod.")

    # TODO .display(), search, permissions

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result=pod.display(), msg="Pod retrieved successfully.")
