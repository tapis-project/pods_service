from datetime import datetime
from fastapi import APIRouter
from models_stacks import (
    Stack, NewStack, UpdateStack, StackActionRequest, JoinStackRequest,
    StacksResponse, StackResponse, StackPermissionsResponse, DeleteStackResponse,
    StackFromTemplateRequest, SaveStackAsTemplateRequest,
    VALID_RESTART_POLICIES,
)
from models_pods import Pod, Password, NewPod
from models_misc import SetPermission
from models_templates_tags import derive_template_info, NewTemplateTag
from utils import check_permissions
from secret_utils import expand_short_secret_references, validate_secret_map_ownership
from models_templates import Template
from errors import PermissionsException, ResourceError
from stack_runner import compute_stack_status
from stack_template_utils import (
    resolve_member_pod_ids, validate_stack_definition, topo_order, compile_member_stack_refs, k8_name,
)
from api_pods_podid import delete_pod_resources
from codes import ON, OFF, RESTART, READ, USER, ADMIN, PERMISSION_LEVELS
from tapisservice.tapisfastapi.utils import g, ok, error
from tapisservice.logs import get_logger
logger = get_logger(__name__)


router = APIRouter()


ACTION_TO_STATUS = {"start": ON, "stop": OFF, "restart": RESTART}


def _stack_log(stack: Stack, msg: str):
    """Append a timestamped audit entry to a stack's action_logs (db_update only auto-logs pods)."""
    entry = f"{datetime.utcnow().strftime('%y/%m/%d %H:%M')}: {msg}"
    stack.action_logs = (stack.action_logs or []) + [entry]


def _get_stack_members(stack_id: str):
    """All pods whose stack_id matches this stack, in the current tenant/site."""
    return Pod.db_get_where(
        where_params=[["stack_id", ".eq", stack_id]],
        tenant=g.request_tenant_id, site=g.site_id,
    )


def _apply_stack_permission(stack: Stack, inp_user: str, inp_level: str):
    """Set or update one user's permission on a stack, keeping at least one ADMIN. Mutates stack.permissions."""
    if inp_level not in PERMISSION_LEVELS:
        raise ValueError(f"Permission level must be one of {PERMISSION_LEVELS}. Got '{inp_level}'.")
    curr_perms = stack.get_permissions()
    curr_perms[inp_user] = inp_level
    if "ADMIN" not in curr_perms.values():
        raise ResourceError("Operation would leave the stack with no ADMIN-capable user. Rolling back.", 400)
    stack.permissions = [f"{user}:{level}" for user, level in curr_perms.items()]


#### /pods/stacks

@router.get(
    "/pods/stacks",
    tags=["Stacks"],
    summary="list_stacks",
    operation_id="list_stacks",
    response_model=StacksResponse)
async def list_stacks():
    """
    Get all stacks in your tenant and site that you have READ or higher access to.

    Returns a list of stacks.
    """
    logger.info("GET /pods/stacks - Top of list_stacks.")

    metadata = {}
    if getattr(g, 'admin_active', False):
        stacks = Stack.db_get_all(tenant=g.request_tenant_id, site=g.site_id)
        read_levels = {'READ', 'USER', 'ADMIN', 'APPROVEDADMIN'}
        user_stack_ids = set()
        for stack in stacks:
            for perm in stack.permissions:
                user, level = perm.split(':', 1)
                if user == g.username and level in read_levels:
                    user_stack_ids.add(stack.stack_id)
                    break
    else:
        stacks = Stack.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)

    stacks_to_show = [stack.display() for stack in stacks]

    if getattr(g, 'admin_active', False):
        admin_only_count = sum(1 for s in stacks_to_show if s.get('stack_id') not in user_stack_ids)
        metadata["admin_context"] = {
            "admin_mode": True,
            "user_accessible_ids": list(user_stack_ids),
            "msg": f"You can access {len(stacks_to_show) - admin_only_count} stacks, admin reveals {admin_only_count}"
        }

    logger.info("Stacks retrieved.")
    return ok(result=stacks_to_show, metadata=metadata, msg="Stacks retrieved successfully.")


@router.post(
    "/pods/stacks",
    tags=["Stacks"],
    summary="create_stack",
    operation_id="create_stack",
    response_model=StackResponse)
async def create_stack(new_stack: NewStack):
    """
    Create a stack with the inputted information.

    Notes:
    - Author is given ADMIN permission. Member pods inherit max(pod, stack) permission.
    - stack_id is flat and unique per tenant (like pod_id/volume_id).

    Returns the new stack object.
    """
    logger.info("POST /pods/stacks - Top of create_stack.")

    # Create full Stack object. Validates as well.
    stack = Stack(**new_stack.dict())

    # Enforce tenant-unique stack_id (clear 409-style message rather than a raw integrity error).
    existing = Stack.db_get_with_pk(stack.stack_id, tenant=g.request_tenant_id, site=g.site_id)
    if existing:
        return error(msg=f"Stack with stack_id '{stack.stack_id}' already exists in this tenant.")

    stack.db_create()
    logger.debug(f"New stack saved in db. stack_id: {stack.stack_id}; tenant: {g.request_tenant_id}.")

    return ok(result=stack.display(), msg="Stack created successfully.")


@router.get(
    "/pods/stacks/{stack_id}",
    tags=["Stacks"],
    summary="get_stack",
    operation_id="get_stack",
    response_model=StackResponse)
async def get_stack(stack_id):
    """
    Get a stack and its member pods.

    Returns the stack object with a `pods` list of member pod displays.
    """
    logger.info(f"GET /pods/stacks/{stack_id} - Top of get_stack.")

    stack = Stack.db_get_with_pk(stack_id, tenant=g.request_tenant_id, site=g.site_id)
    members = _get_stack_members(stack_id)

    # Refresh the cached aggregate status so list_stacks stays reasonably current.
    new_status = compute_stack_status(members)
    if new_status != (stack.status or ""):
        stack.status = new_status
        stack.db_update()

    result = stack.display()
    result["pods"] = [pod.display() for pod in members]

    return ok(result=result, msg="Stack retrieved successfully.")


@router.put(
    "/pods/stacks/{stack_id}",
    tags=["Stacks"],
    summary="update_stack",
    operation_id="update_stack",
    response_model=StackResponse)
async def update_stack(stack_id, update_stack: UpdateStack):
    """
    Update a stack's description and/or restart_policy.

    Returns the updated stack object.
    """
    logger.info(f"PUT /pods/stacks/{stack_id} - Top of update_stack.")

    stack = Stack.db_get_with_pk(stack_id, tenant=g.request_tenant_id, site=g.site_id)
    input_data = update_stack.dict(exclude_unset=True)

    if "restart_policy" in input_data and input_data["restart_policy"] is not None:
        if input_data["restart_policy"] not in VALID_RESTART_POLICIES:
            return error(msg=f"restart_policy must be one of {VALID_RESTART_POLICIES}. Got '{input_data['restart_policy']}'.")
        stack.restart_policy = input_data["restart_policy"]
    if "description" in input_data and input_data["description"] is not None:
        stack.description = input_data["description"]

    _stack_log(stack, f"'{g.username}' updated stack ({', '.join(input_data.keys()) or 'no changes'})")
    stack.db_update()

    return ok(result=stack.display(), msg="Stack updated successfully.")


@router.delete(
    "/pods/stacks/{stack_id}",
    tags=["Stacks"],
    summary="delete_stack",
    operation_id="delete_stack",
    response_model=DeleteStackResponse)
async def delete_stack(stack_id, force: bool = False, delete_pods: bool = False, confirm: str = ""):
    """
    Delete a stack.

    Three modes:
    - Default: if the stack still has member pods, returns an error listing them (nothing deleted).
    - `?force=true`: detaches members (clears their stack_id + depends_on) then deletes the stack.
      **Member pods are kept** — this is the safe default for cleaning up a stack.
    - `?delete_pods=true&confirm=<stack_id>`: **destructive** — deletes the stack AND every member
      pod (and their k8s resources). Requires `confirm` to exactly equal the stack_id, so it cannot
      happen by accident.

    Returns the deleted stack_id.
    """
    logger.info(f"DELETE /pods/stacks/{stack_id} - Top of delete_stack. force={force}, delete_pods={delete_pods}")

    stack = Stack.db_get_with_pk(stack_id, tenant=g.request_tenant_id, site=g.site_id)
    members = _get_stack_members(stack_id)

    # Destructive path: delete the stack AND all member pods. Requires a typed confirmation string.
    if delete_pods:
        if confirm != stack_id:
            return error(
                result=[pod.pod_id for pod in members],
                msg=f"This will permanently delete stack '{stack_id}' AND its {len(members)} member "
                    f"pod(s). To confirm, pass ?delete_pods=true&confirm={stack_id} "
                    f"(confirm must exactly equal the stack_id).",
            )
        for pod in members:
            password = Password.db_get_with_pk(pod.pod_id, tenant=g.request_tenant_id, site=g.site_id)
            delete_pod_resources(pod, password)
            logger.info(f"Deleted member pod '{pod.pod_id}' as part of stack '{stack_id}' cascade delete.")
        stack.db_delete()
        logger.info(f"Stack {stack_id} and {len(members)} member pod(s) deleted.")
        return ok(result=stack_id, msg=f"Stack '{stack_id}' and {len(members)} member pod(s) deleted.")

    # Non-destructive default: members must be empty, or detached via ?force=true (pods kept).
    if members and not force:
        return error(
            result=[pod.pod_id for pod in members],
            msg=f"Stack '{stack_id}' has {len(members)} member pod(s). Use ?force=true to detach them "
                f"(pods kept), or ?delete_pods=true&confirm={stack_id} to delete the stack and the pods.",
        )

    if members and force:
        for pod in members:
            pod.stack_id = ""
            pod.depends_on = None
            pod.db_update(f"'{g.username}' detached pod from stack '{stack_id}' (stack deleted)")

    stack.db_delete()
    logger.info(f"Stack {stack_id} deleted.")
    return ok(result=stack_id, msg="Stack deleted successfully.")


@router.post(
    "/pods/stacks/{stack_id}/action",
    tags=["Stacks"],
    summary="stack_action",
    operation_id="stack_action",
    response_model=StackResponse)
async def stack_action(stack_id, stack_action: StackActionRequest):
    """
    Apply a lifecycle action to every member pod of a stack.

    Notes:
    - action is one of 'start', 'stop', 'restart'.
    - Non-blocking: this sets each member pod's status_requested and returns immediately. The health
      loop's Stack Action Runner performs the ordering (respecting each pod's depends_on/ready_condition)
      when restart_policy is 'ordered'. To change ordering, use PUT /pods/stacks/{stack_id}.
    - `force` (stop/restart only) bypasses reverse-order teardown — members stop immediately, ignoring
      dependency order during shutdown. Startup ordering is still honored.
    - Requires stack ADMIN (parity with the direct per-pod stop/start/restart routes, which
      require pod ADMIN); the grant inherits to all members, so no per-pod checks are needed.

    Returns the stack object; metadata.acted lists the pods affected.
    """
    logger.info(f"POST /pods/stacks/{stack_id}/action - Top of stack_action.")

    stack = Stack.db_get_with_pk(stack_id, tenant=g.request_tenant_id, site=g.site_id)
    action = (stack_action.action or "").lower()
    if action not in ACTION_TO_STATUS:
        return error(msg=f"action must be one of {list(ACTION_TO_STATUS)}. Got '{stack_action.action}'.")
    new_status = ACTION_TO_STATUS[action]
    force = bool(stack_action.force) and action in ("stop", "restart")

    members = _get_stack_members(stack_id)
    acted = []
    for pod in members:
        pod.status_requested = new_status
        if force:
            pod.force_stop = True
        pod.db_update(f"'{g.username}' ran stack '{stack_id}' {action}{' (force)' if force else ''} (status_requested={new_status})")
        acted.append(pod.pod_id)

    _stack_log(stack, f"'{g.username}' requested {action}{' force' if force else ''} ({stack.restart_policy}) on {len(acted)} pod(s): {acted}")
    stack.db_update()

    return ok(
        result=stack.display(),
        metadata={"acted": acted, "action": action, "force": force},
        msg=f"Stack '{stack_id}' {action} requested on {len(acted)} pod(s).",
    )


#### /pods/stacks/{stack_id}/permissions

@router.get(
    "/pods/stacks/{stack_id}/permissions",
    tags=["Stacks"],
    summary="get_stack_permissions",
    operation_id="get_stack_permissions",
    response_model=StackPermissionsResponse)
async def get_stack_permissions(stack_id):
    """Get all permissions for a stack."""
    logger.info(f"GET /pods/stacks/{stack_id}/permissions - Top of get_stack_permissions.")
    stack = Stack.db_get_with_pk(stack_id, tenant=g.request_tenant_id, site=g.site_id)
    return ok(result={"permissions": stack.permissions}, msg="Stack permissions retrieved successfully.")


@router.post(
    "/pods/stacks/{stack_id}/permissions",
    tags=["Stacks"],
    summary="set_stack_permission",
    operation_id="set_stack_permission",
    response_model=StackPermissionsResponse)
async def set_stack_permission(stack_id, set_permission: SetPermission):
    """
    Set or update a user's permission on a stack. Member pods inherit max(pod, stack) permission,
    so granting here grants access to every member pod in one write — including pods added later.

    Returns the updated stack permissions.
    """
    logger.info(f"POST /pods/stacks/{stack_id}/permissions - Top of set_stack_permission.")

    # Admin-only check for tenant-wide/global grants — parity with the pod and
    # template permission endpoints (a stack grant fans out to every member pod)
    if (set_permission.user.startswith("tenant.") or set_permission.user == "**") and not g.admin:
        raise PermissionsException("Only admins can set 'tenant.*' or '**' permissions on stacks.")

    stack = Stack.db_get_with_pk(stack_id, tenant=g.request_tenant_id, site=g.site_id)
    _apply_stack_permission(stack, set_permission.user, set_permission.level)

    _stack_log(stack, f"'{g.username}' set permission for '{set_permission.user}' to {set_permission.level}")
    stack.db_update()

    return ok(result={"permissions": stack.permissions}, msg="Stack permissions updated successfully.")


@router.delete(
    "/pods/stacks/{stack_id}/permissions/{user}",
    tags=["Stacks"],
    summary="delete_stack_permission",
    operation_id="delete_stack_permission",
    response_model=StackPermissionsResponse)
async def delete_stack_permission(stack_id, user):
    """
    Delete a user's permission from a stack. At least one ADMIN must remain.

    Returns the updated stack permissions.
    """
    logger.info(f"DELETE /pods/stacks/{stack_id}/permissions/{user} - Top of delete_stack_permission.")

    stack = Stack.db_get_with_pk(stack_id, tenant=g.request_tenant_id, site=g.site_id)
    curr_perms = stack.get_permissions()
    if user not in curr_perms:
        return error(msg=f"User '{user}' has no permissions on stack '{stack_id}'.")
    del curr_perms[user]
    if "ADMIN" not in curr_perms.values():
        return error(msg="Operation would result in stack with no users in ADMIN role. Rolling back.")
    stack.permissions = [f"{u}:{l}" for u, l in curr_perms.items()]

    _stack_log(stack, f"'{g.username}' deleted permission for '{user}'")
    stack.db_update()

    return ok(result={"permissions": stack.permissions}, msg="Stack permission deleted successfully.")


#### /pods/{pod_id}/stack — membership (join/leave)

@router.post(
    "/pods/{pod_id}/stack",
    tags=["Stacks"],
    summary="pod_join_stack",
    operation_id="pod_join_stack")
async def pod_join_stack(pod_id, join_stack: JoinStackRequest):
    """
    Add a pod to a stack. Requires ADMIN on the pod (route-gated) and USER+ on the target stack.

    Returns the updated pod object.
    """
    logger.info(f"POST /pods/{pod_id}/stack - Top of pod_join_stack.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    stack = Stack.db_get_with_pk(join_stack.stack_id, tenant=g.request_tenant_id, site=g.site_id)
    if not stack:
        return error(msg=f"Stack '{join_stack.stack_id}' not found. Create it first with POST /pods/stacks.")

    # Joining a stack requires USER+ on it (admins bypass, mirroring route-level behavior).
    if not getattr(g, 'admin', False) and not check_permissions(g.username, USER, stack, "stack", roles=g.roles):
        return error(msg=f"You need USER+ permission on stack '{join_stack.stack_id}' to add a pod to it.")

    pod.stack_id = stack.stack_id
    pod.db_update(f"'{g.username}' added pod to stack '{stack.stack_id}'")
    _stack_log(stack, f"'{g.username}' added pod '{pod_id}' to stack")
    stack.db_update()

    return ok(result=pod.display(), msg=f"Pod '{pod_id}' added to stack '{stack.stack_id}'.")


@router.delete(
    "/pods/{pod_id}/stack",
    tags=["Stacks"],
    summary="pod_leave_stack",
    operation_id="pod_leave_stack")
async def pod_leave_stack(pod_id):
    """
    Remove a pod from its stack. Clears stack_id and depends_on (dependencies are stack-scoped).

    Returns the updated pod object.
    """
    logger.info(f"DELETE /pods/{pod_id}/stack - Top of pod_leave_stack.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    old_stack_id = pod.stack_id
    pod.stack_id = ""
    pod.depends_on = None
    pod.db_update(f"'{g.username}' removed pod from stack '{old_stack_id}'")

    if old_stack_id:
        stack = Stack.db_get_with_pk(old_stack_id, tenant=g.request_tenant_id, site=g.site_id)
        if stack:
            _stack_log(stack, f"'{g.username}' removed pod '{pod_id}' from stack")
            stack.db_update()

    return ok(result=pod.display(), msg=f"Pod '{pod_id}' removed from stack.")


#### /pods/stacks/from-template — instantiate a kind='stack' template tag


@router.post(
    "/pods/stacks/from-template",
    tags=["Stacks"],
    summary="create_stack_from_template",
    operation_id="create_stack_from_template",
    response_model=StackResponse)
async def create_stack_from_template(body: StackFromTemplateRequest):
    """
    Instantiate a whole stack from a kind='stack' template tag in one call.

    Expands the template's members into a Stack + member pods:
    - external pod_id per member = pod_ids[name] override, else '{stack_id}{name}';
    - depends_on member names -> pod_ids; ${stack:<member>:host} -> in-cluster DNS literal;
    - ${stack:secrets:KEY} stays a *reference* on each pod, resolved at start from the stack's shared
      secret_map (template secret_map + any values supplied in `secrets`).

    The full set is validated up front (ids, cycles, ready->probe, collisions); if any member fails
    to create, the stack and every member created so far are rolled back.

    Returns the new stack and its member pods.
    """
    logger.info(f"POST /pods/stacks/from-template - Top (stack_id={body.stack_id}, template={body.template}).")
    from api_pods import create_pod  # lazy import to avoid an import cycle at module load

    # 1. Resolve the stack template tag.
    try:
        template_str, _template, tag = derive_template_info(body.template, tenant=g.request_tenant_id, site=g.site_id)
    except Exception as e:
        return error(msg=f"Could not resolve template '{body.template}': {e}")
    if not tag or getattr(tag, "kind", "pod") != "stack" or not getattr(tag, "stack_definition", None):
        return error(msg=f"Template '{body.template}' is not a stack template (kind='stack').")

    # stack_definition may come back from the JSON column as a model or a plain dict — normalize.
    stack_def = tag.stack_definition
    sd = stack_def.dict() if hasattr(stack_def, "dict") else dict(stack_def or {})
    member_dicts = sd.get("members") or []
    if not member_dicts:
        return error(msg="Stack template has no members.")
    member_names = [m["name"] for m in member_dicts]
    sd_secret_map = dict(sd.get("secret_map") or {})
    sd_restart_policy = sd.get("restart_policy", "ordered")
    stack_secret_keys = list(sd_secret_map.keys())

    # 2 + 3. Resolve external pod_ids and structurally validate — atomic precheck, nothing created yet.
    pod_id_by_name, id_errors = resolve_member_pod_ids(member_names, body.stack_id, body.pod_ids)
    struct_errors = validate_stack_definition(member_dicts, stack_secret_keys)
    precheck_errors = id_errors + struct_errors
    if precheck_errors:
        return error(result=precheck_errors, msg="Stack template validation failed; nothing was created.")

    # 4. Existence precheck: stack_id free, and no resolved pod_id already taken in this tenant.
    if Stack.db_get_with_pk(body.stack_id, tenant=g.request_tenant_id, site=g.site_id):
        return error(msg=f"Stack '{body.stack_id}' already exists in this tenant.")
    taken = [f"{pid} (member '{name}')" for name, pid in pod_id_by_name.items()
             if Pod.db_get_with_pk(pid, tenant=g.request_tenant_id, site=g.site_id)]
    if taken:
        return error(result=taken, msg=f"Pod id(s) already exist: {taken}. Override them with pod_ids.")

    # 5. Create the Stack with its shared secret_map (template secrets + user-supplied values).
    # Pin short refs to the caller and reject cross-user ${secret:other:id} in USER-supplied
    # values — member pods resolve with actor=None at start, so ownership must be gated here.
    user_secrets = expand_short_secret_references(body.secrets or {}, g.username)
    own_errors = validate_secret_map_ownership(user_secrets, g.username)
    if own_errors:
        return error(msg="; ".join(own_errors))
    stack_secret_map = dict(sd_secret_map)
    stack_secret_map.update(user_secrets)
    stack = Stack(stack_id=body.stack_id, description=body.description or "", restart_policy=sd_restart_policy)
    stack.secret_map = stack_secret_map
    stack.from_template = template_str
    stack.db_create()
    _stack_log(stack, f"'{g.username}' created stack from template '{template_str}' ({len(member_dicts)} members)")
    stack.db_update()

    # 6. Create members in dependency order; roll the whole stack back on any failure.
    net_by_name = {m["name"]: (m.get("networking") or {}) for m in member_dicts}
    overrides = body.overrides or {}
    HANDLED = {"name", "depends_on", "environment_variables", "secret_map"}
    created_pod_ids = []
    try:
        for m in topo_order(member_dicts):
            name = m["name"]
            pid = pod_id_by_name[name]
            merged = dict(m)
            merged.update(overrides.get(name, {}) or {})

            env, secret_map = compile_member_stack_refs(
                merged.get("environment_variables") or {},
                merged.get("secret_map") or {},
                pod_id_by_name, net_by_name, g.site_id, g.request_tenant_id,
            )
            dep_ids = [pod_id_by_name[d] for d in (merged.get("depends_on") or [])]

            pod_kwargs = {k: v for k, v in merged.items() if k not in HANDLED and v is not None}
            pod_kwargs.update({
                "pod_id": pid,
                "stack_id": body.stack_id,
                "depends_on": dep_ids or None,
                "ready_condition": merged.get("ready_condition") or "available",
                "environment_variables": env,
                "secret_map": secret_map,
            })
            new_pod = NewPod(**{k: v for k, v in pod_kwargs.items() if k in NewPod.__fields__})
            await create_pod(new_pod)
            created_pod_ids.append(pid)
            logger.info(f"Stack '{body.stack_id}': created member '{name}' as pod '{pid}'.")
    except Exception as e:
        logger.error(f"from-template failed creating members for '{body.stack_id}'; rolling back. Error: {e}")
        for pid in created_pod_ids:
            try:
                p = Pod.db_get_with_pk(pid, tenant=g.request_tenant_id, site=g.site_id)
                pw = Password.db_get_with_pk(pid, tenant=g.request_tenant_id, site=g.site_id)
                if p:
                    delete_pod_resources(p, pw)
            except Exception as ce:
                logger.warning(f"rollback: failed to delete pod '{pid}': {ce}")
        try:
            stack.db_delete()
        except Exception as ce:
            logger.warning(f"rollback: failed to delete stack '{body.stack_id}': {ce}")
        return error(msg=f"Failed to instantiate stack from template; rolled back all changes. Error: {e}")

    # 7. Return the new stack + members.
    members_out = _get_stack_members(body.stack_id)
    result = stack.display()
    result["pods"] = [p.display() for p in members_out]
    return ok(
        result=result,
        metadata={"members": created_pod_ids, "from_template": template_str},
        msg=f"Stack '{body.stack_id}' created from template '{template_str}' with {len(created_pod_ids)} member(s).",
    )


#### /pods/stacks/{stack_id}/save_as_template — snapshot a live stack into a kind='stack' tag


# Pod-definition fields that carry over from a live member pod into a StackMemberDefinition.
_MEMBER_POD_FIELDS = [
    "image", "template", "description", "command", "arguments", "environment_variables",
    "secret_map", "volume_mounts", "networking", "resources", "compute_queue", "healthchecks",
    "time_to_stop_default", "time_to_stop_instance",
]


def _placeholderize_secret_value(val: str) -> str:
    """For snapshots: keep shared-stack references and existing placeholders; replace any concrete
    secret reference/value with a required placeholder so the template never embeds a real secret."""
    if not isinstance(val, str):
        return val
    if val.startswith("${stack:secrets:") or val.startswith("${:?") or val.startswith("${pods:default:"):
        return val
    return "${:?provide this secret value}"


@router.post(
    "/pods/stacks/{stack_id}/save_as_template",
    tags=["Stacks"],
    summary="save_stack_as_template",
    operation_id="save_stack_as_template")
async def save_stack_as_template(stack_id, body: SaveStackAsTemplateRequest):
    """
    Snapshot a live stack into a new kind='stack' template tag.

    Member pod_ids become member names (the '{stack_id}' prefix is stripped where present), depends_on
    pod_ids become member names, this stack's member k8 hostnames are reversed back to
    ${stack:<name>:host} references, and all concrete secret values are replaced with ${:?...}
    placeholders so the template embeds no real secrets (mirrors save_pod_as_template hygiene).

    Returns the new template tag.
    """
    logger.info(f"POST /pods/stacks/{stack_id}/save_as_template - Top (template_id={body.template_id}).")
    from api_templates_templateid_tags import add_template_tag  # lazy import to avoid import cycle

    stack = Stack.db_get_with_pk(stack_id, tenant=g.request_tenant_id, site=g.site_id)
    members = _get_stack_members(stack_id)
    if not members:
        return error(msg=f"Stack '{stack_id}' has no member pods to snapshot.")

    # Writing a tag under a template requires template-level USER — the same gate the real
    # POST /pods/templates/{id}/tags endpoint enforces. add_template_tag() has no internal
    # permission check, so calling it directly here would otherwise let any stack USER write
    # a tag into ANY template.
    template = Template.db_get_with_pk(body.template_id, tenant="siteadmintable", site=g.site_id)
    if not template:
        return error(msg=f"Template '{body.template_id}' not found. Create it before saving a stack tag under it.")
    if not getattr(g, 'admin', False) and not check_permissions(g.username, USER, template, "template", roles=g.roles):
        raise PermissionsException(f"Not authorized — you need USER permission on template '{body.template_id}' to save a stack as a tag under it.")

    # pod_id -> member name (strip the stack_id prefix when present).
    pod_id_to_name = {}
    for pod in members:
        pid = pod.pod_id
        name = pid[len(stack_id):] if (pid.startswith(stack_id) and len(pid) > len(stack_id)) else pid
        pod_id_to_name[pid] = name

    # Reverse this stack's member k8 hostnames back to ${stack:<name>:host} references.
    k8_to_ref = {k8_name(g.site_id, g.request_tenant_id, pid): "${stack:" + nm + ":host}"
                 for pid, nm in pod_id_to_name.items()}

    def _unbake_hosts(s):
        if not isinstance(s, str):
            return s
        for k8, ref in k8_to_ref.items():
            s = s.replace(k8, ref)
        return s

    member_defs = []
    for pod in members:
        d = pod.dict()
        member = {"name": pod_id_to_name[pod.pod_id]}
        for f in _MEMBER_POD_FIELDS:
            v = d.get(f)
            if v in (None, {}, []):
                continue
            member[f] = v
        # rewrite cross-references + scrub secrets
        if member.get("environment_variables"):
            member["environment_variables"] = {k: _unbake_hosts(v) for k, v in member["environment_variables"].items()}
        if member.get("secret_map"):
            member["secret_map"] = {k: _placeholderize_secret_value(_unbake_hosts(v)) for k, v in member["secret_map"].items()}
        member["ready_condition"] = pod.ready_condition or "available"
        member["depends_on"] = [pod_id_to_name.get(dep, dep) for dep in (pod.depends_on or [])]
        member_defs.append(member)

    # Stack-level shared secrets become required placeholders (keys preserved, values scrubbed).
    stack_secret_map = {k: "${:?provide shared secret '" + k + "'}" for k in (stack.secret_map or {}).keys()}

    stack_definition = {
        "restart_policy": stack.restart_policy,
        "secret_map": stack_secret_map,
        "members": member_defs,
    }
    new_tag = NewTemplateTag(
        kind="stack",
        stack_definition=stack_definition,
        commit_message=body.commit_message or f"Snapshot of stack '{stack_id}'",
        tag=body.tag,
    )
    return await add_template_tag(body.template_id, new_tag)
