from fastapi import APIRouter
from models import Pod, NewPod, UpdatePod, Password, SetPermission, DeletePermission, getPodReturn
from channels import CommandChannel
from codes import OFF, ON, RESTART, REQUESTED
from tapisservice.tapisfastapi.utils import g, ok

from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods/{pod_id}/functionHere

@router.get("/pods/{pod_id}/credentials", tags=["pods"], summary="get_pod_credentials", operation_id="get_pod_credentials")
async def get_pod_credentials(pod_id):
    # Do more update things.
    password = Password.db_get_with_pk(pod_id, g.request_tenant_id, g.site_id)
    user_cred = {"user_username": password.user_username,
                 "user_password": password.user_password}

    return ok(result=user_cred)


@router.get("/pods/{pod_id}/logs", tags=["pods"], operation_id="get_pod_logs")
async def get_pod_logs(pod_id):

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result={"logs": pod.logs}, msg = "Pod logs retrieved successfully.")


@router.get("/pods/{pod_id}/permissions", tags=["pods"], summary="get_pod_permissions", operation_id="get_pod_permissions")
async def get_pod_permissions(pod_id):

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result={"permissions": pod.permissions}, msg = "Pod permissions retrieved successfully.")


@router.post("/pods/{pod_id}/permissions", tags=["pods"], summary="set_pod_permissions", operation_id="set_pod_permission")
async def set_pod_permission(pod_id, set_permission: SetPermission):
    inp_user = set_permission.user
    inp_level = set_permission.level

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Get formatted perms
    curr_perms = pod.get_permissions()

    # Update variable
    curr_perms[inp_user] = inp_level

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise KeyError(f"Operation would result in pod with no users in 'ADMIN' roll. Rolling back.")

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")

    # Update pod object and commit
    pod.permissions = perm_list
    pod.db_update()

    return ok(result={"permissions": pod.permissions}, msg = "Pod permissions updated successfully.")


@router.delete("/pods/{pod_id}/permissions", tags=["pods"], summary="delete_pod_permissions", operation_id="delete_pod_permission")
async def delete_pod_permission(pod_id, delete_permission: DeletePermission):
    inp_user = delete_permission.user

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Get formatted perms
    curr_perms = pod.get_permissions()

    if inp_user not in curr_perms.keys():
        raise KeyError(f"Could not find permission for pod with username {inp_user} when deleting permission")

    # Delete permission
    del curr_perms[inp_user]

    # Ensure there's still one ADMIN role before finishing.
    if "ADMIN" not in curr_perms.values():
        raise KeyError(f"Operation would result in pod with no users in ADMIN role. Rolling back.")

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")
    
    # Update pod object and commit
    pod.permissions = perm_list
    pod.db_update()

    return ok(result=perm_list, msg = "Pod permissions updated successfully.")


@router.get("/pods/{pod_id}/stop", tags=["pods"], summary="stop_pod", operation_id="stop_pod", response_model=getPodReturn)
async def stop_pod(pod_id):

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    pod.status_requested = OFF
    pod.db_update()

    return ok(result=pod.display(), msg = "Updated pod's status_requested to OFF.")


@router.get("/pods/{pod_id}/start", tags=["pods"], summary="start_pod", operation_id="start_pod", response_model=getPodReturn)
async def start_pod(pod_id):

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    pod.status_requested = ON
    pod.status = REQUESTED
    pod.db_update()

    # Send command to start new pod
    ch = CommandChannel(name=pod.site_id)
    ch.put_cmd(pod_id=pod.pod_id,
                tenant_id=pod.tenant_id,
                site_id=pod.site_id)
    ch.close()
    logger.debug(f"Command Channel - Added msg for pod_id: {pod.pod_id}.")

    return ok(result=pod.display(), msg = "Updated pod's status_requested to ON and requested pod.")


@router.get("/pods/{pod_id}/restart", tags=["pods"], summary="restart_pod", operation_id="restart_pod", response_model=getPodReturn)
async def restart_pod(pod_id):

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    pod.status_requested = RESTART
    pod.db_update()

    return ok(result=pod.display(), msg = "Updated pod's status_requested to RESTART.")
