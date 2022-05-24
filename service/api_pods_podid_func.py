from fastapi import APIRouter
from models import Pod, NewPod, UpdatePod, Password, SetPermission, DeletePermission
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok

from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods/{pod_id}/functionHere

@router.get("/pods/{pod_id}/credentials", tags=["pods"])
async def get_pod_credentials(pod_id):
    # Do more update things.
    password = Password.db_get_with_pk(pod_id, g.request_tenant_id, g.site_id)
    user_cred = {"user_username": password.user_username,
                 "user_password": password.user_password}

    return ok(result=user_cred)


@router.get("/pods/{pod_id}/logs", tags=["pods"])
async def get_pod_logs(pod_id):

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result={"logs": pod.logs}, msg = "Pod logs retrieved successfully.")


@router.get("/pods/{pod_id}/permissions", tags=["pods"])
async def get_pod_permissions(pod_id):

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    return ok(result={"permissions": pod.permissions}, msg = "Pod permissions retrieved successfully.")


@router.post("/pods/{pod_id}/permissions", tags=["pods"])
async def set_permission(pod_id, set_permission: SetPermission):
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

    return ok(result=pod, msg = "Pod permissions updated successfully.")


@router.delete("/pods/{pod_id}/permissions", tags=["pods"])
async def delete_permission(pod_id, delete_permission: DeletePermission):
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
