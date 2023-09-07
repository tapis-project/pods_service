from fastapi import APIRouter
from models_pods import Pod, Password, PodResponse, PodPermissionsResponse, PodCredentialsResponse, PodLogsResponse
from models_misc import SetPermission
from channels import CommandChannel
from codes import OFF, ON, RESTART, REQUESTED, STOPPED
from tapisservice.tapisfastapi.utils import g, ok

from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods/{pod_id}/functionHere

@router.get(
    "/pods/{pod_id}/credentials",
    tags=["Credentials"],
    summary="get_pod_credentials",
    operation_id="get_pod_credentials",
    response_model=PodCredentialsResponse)
async def get_pod_credentials(pod_id):
    """
    Get the credentials created for a pod.

    Note:
    - These credentials are used in the case of templated pods, but for custom pods they're not.

    Returns user accessible credentials.
    """
    logger.info(f"GET /pods/{pod_id}/credentials - Top of get_pod_credentials.")

    # Do more update things.
    password = Password.db_get_with_pk(pod_id, g.request_tenant_id, g.site_id)
    user_cred = {"user_username": password.user_username,
                 "user_password": password.user_password}

    return ok(result=user_cred)


@router.get(
    "/pods/{pod_id}/logs",
    tags=["Logs"],
    summary="get_pod_logs",
    operation_id="get_pod_logs",
    response_model=PodLogsResponse)
async def get_pod_logs(pod_id):
    """
    Get a pods stdout logs and action_logs.
    
    Note:
    - Pod logs are only retrieved while pod is running.
    - If a pod is restarted or turned off and then on, the logs will be reset.
    - Action logs are detailed logs of actions taken on the pod.

    Returns pod stdout logs and action logs.
    """
    logger.info(f"GET /pods/{pod_id}/logs - Top of get_pod_logs.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result={"logs": pod.logs, "action_logs": pod.action_logs}, msg = "Pod logs retrieved successfully.")


@router.get(
    "/pods/{pod_id}/permissions",
    tags=["Permissions"],
    summary="get_pod_permissions",
    operation_id="get_pod_permissions",
    response_model=PodPermissionsResponse)
async def get_pod_permissions(pod_id):
    """
    Get a pods permissions.

    Note:
    - There are 3 levels of permissions, READ, USER, and ADMIN.
    - Permissions are granted/revoked to individual TACC usernames.

    Returns all pod permissions.
    """
    logger.info(f"GET /pods/{pod_id}/permissions - Top of get_pod_permissions.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result={"permissions": pod.permissions}, msg = "Pod permissions retrieved successfully.")


@router.post(
    "/pods/{pod_id}/permissions",
    tags=["Permissions"],
    summary="set_pod_permission",
    operation_id="set_pod_permission",
    response_model=PodPermissionsResponse)
async def set_pod_permission(pod_id, set_permission: SetPermission):
    """
    Set a permission for a pod.

    Returns updated pod permissions.
    """
    logger.info(f"POST /pods/{pod_id}/permissions - Top of set_pod_permissions.")

    inp_user = set_permission.user
    inp_level = set_permission.level

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Get formatted perms
    curr_perms = pod.get_permissions()

    # Update variable
    curr_perms[inp_user] = inp_level

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise KeyError(f"Operation would result in pod with no users in ADMIN role. Rolling back.")

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")

    # Update pod object and commit
    pod.permissions = perm_list
    pod.db_update(f"'{g.username}' set permission for '{inp_user}' to {inp_level}")

    return ok(result={"permissions": pod.permissions}, msg = "Pod permissions updated successfully.")


@router.delete(
    "/pods/{pod_id}/permissions/{user}",
    tags=["Permissions"],
    summary="delete_pod_permission",
    operation_id="delete_pod_permission",
    response_model=PodPermissionsResponse)
async def delete_pod_permission(pod_id, user):
    """
    Delete a permission from a pod.

    Returns updated pod permissions.
    """
    logger.info(f"DELETE /pods/{pod_id}/permissions/{user} - Top of delete_pod_permission.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Get formatted perms
    curr_perms = pod.get_permissions()

    if user not in curr_perms.keys():
        raise KeyError(f"Could not find permission for pod with username {user} when deleting permission")

    # Delete permission
    del curr_perms[user]

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise KeyError(f"Operation would result in pod with no users in ADMIN role. Rolling back.")

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")
    
    # Update pod object and commit
    pod.permissions = perm_list
    pod.db_update(f"'{g.username}' deleted permission for '{user}'")

    return ok(result={"permissions": pod.permissions}, msg = "Pod permission deleted successfully.")


@router.get(
    "/pods/{pod_id}/stop",
    tags=["Pods"],
    summary="stop_pod",
    operation_id="stop_pod",
    response_model=PodResponse)
async def stop_pod(pod_id):
    """
    Stop a pod.

    Note:
    - Sets status_requested to OFF. Pod will attempt to get to STOPPED status unless start_pod is ran.

    Returns updated pod object.
    """
    logger.info(f"GET /pods/{pod_id}/stop - Top of stop_pod.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    pod.status_requested = OFF
    pod.db_update(f"'{g.username}' ran stop_pod, set to OFF")
                  
    return ok(result=pod.display(), msg = "Updated pod's status_requested to OFF.")


@router.get(
    "/pods/{pod_id}/start",
    tags=["Pods"],
    summary="start_pod",
    operation_id="start_pod",
    response_model=PodResponse)
async def start_pod(pod_id):
    """
    Start a pod.

    Note:
    - Sets status_requested to ON. Pod will attempt to deploy.

    Returns updated pod object.
    """
    logger.info(f"GET /pods/{pod_id}/start - Top of start_pod.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Only run start_pod from status=STOPPED
    if not pod.status in [STOPPED]:
        raise RuntimeError(f"Pod must be in 'STOPPED' status to run 'start_pod'. Please run 'stop_pod' or 'restart_pod' instead.")
    else:
        pod.status_requested = ON
        pod.status = REQUESTED

        # Send command to start new pod
        ch = CommandChannel(name=pod.site_id)
        ch.put_cmd(object_id=pod.pod_id,
                   object_type="pod",
                   tenant_id=pod.tenant_id,
                   site_id=pod.site_id)
        ch.close()
        logger.debug(f"Command Channel - Added msg for pod_id: {pod.pod_id}.")

        pod.db_update(f"'{g.username}' ran start_pod, set to ON and REQUESTED")

    return ok(result=pod.display(), msg = "Updated pod's status_requested to ON and requested pod.")


@router.get(
    "/pods/{pod_id}/restart",
    tags=["Pods"],
    summary="restart_pod",
    operation_id="restart_pod",
    response_model=PodResponse)
async def restart_pod(pod_id):
    """
    Restart a pod.

    Note:
    - Sets status_requested to RESTART. If pod status gets to STOPPED, status_requested will be flipped to ON. Health should then create new pod.

    Returns updated pod object.
    """
    logger.info(f"GET /pods/{pod_id}/restart - Top of restart_pod.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    pod.status_requested = RESTART

    pod.db_update(f"'{g.username}' ran restart_pod, set to RESTART")
                  
    return ok(result=pod.display(), msg = "Updated pod's status_requested to RESTART.")
