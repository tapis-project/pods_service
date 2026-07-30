from datetime import datetime
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from models_stacks import (
    Stack, NewStack, UpdateStack, StackActionRequest, JoinStackRequest,
    StacksResponse, StackResponse, StackPermissionsResponse, DeleteStackResponse,
    StackFromTemplateRequest, SaveStackAsTemplateRequest, StackUpdateRequest,
    VALID_RESTART_POLICIES,
)
from models_pods import Pod, Password, NewPod
from models_misc import SetPermission
from models_templates_tags import (
    derive_template_info, NewTemplateTag,
    Networking as NewTemplateTagNetworking, Resources as NewTemplateTagResources,
)
from utils import check_permissions
from secret_utils import expand_short_secret_references, validate_secret_map_ownership
from models_templates import Template
from errors import PermissionsException, ResourceError
from stack_runner import compute_stack_status
from stack_template_utils import (
    resolve_member_pod_ids, validate_stack_definition, topo_order, compile_member_stack_refs, k8_name,
    sanitize_member_networking, placeholderize_secret_value, unbake_host_refs, compute_stack_member_plan,
    minimize_member_networking, minimize_resources, match_live_member_pod_ids,
    compile_refs_in_volume_mounts, find_residual_host_refs,
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

    return ok(
        result=pod.display(),
        msg=(f"Pod '{pod_id}' added to stack '{stack.stack_id}' as an unmanaged member. "
             f"It can reference the stack's shared secrets and be wired into start ordering "
             f"(depends_on works in both directions with any same-stack pod); template-driven "
             f"stack updates leave it alone."))


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

    # Prune dangling depends_on references on the pods left behind. The start
    # gate treats a MISSING dependency as blocking (stack_runner.gate_start:
    # `dep is None` blocks), so a dangling ref doesn't just linger — it wedges
    # the dependent's next start forever in an ordered stack.
    pruned = []
    if old_stack_id:
        from stack_utils import find_dependents
        for dep_pod in (find_dependents(pod_id, tenant=g.request_tenant_id, site=g.site_id) or []):
            if dep_pod.stack_id != old_stack_id:
                continue
            dep_pod.depends_on = [d for d in (dep_pod.depends_on or []) if d != pod_id] or None
            dep_pod.db_update(
                f"depends_on pruned: '{pod_id}' left stack '{old_stack_id}' "
                f"(a missing dependency would block this pod's start)")
            pruned.append(dep_pod.pod_id)
        stack = Stack.db_get_with_pk(old_stack_id, tenant=g.request_tenant_id, site=g.site_id)
        if stack:
            _stack_log(stack, f"'{g.username}' removed pod '{pod_id}' from stack"
                       + (f"; pruned depends_on on: {', '.join(pruned)}" if pruned else ""))
            stack.db_update()

    msg = f"Pod '{pod_id}' removed from stack."
    if pruned:
        msg += (f" Heads up: {', '.join(pruned)} depended on it — those references were removed "
                f"(a missing dependency would have blocked their next start). Re-wire their "
                f"depends_on if the ordering still matters.")
    return ok(result=pod.display(), msg=msg, metadata={"pruned_depends_on": pruned} if pruned else {})


#### /pods/stacks/from-template — instantiate a kind='stack' template tag


@router.post(
    "/pods/stacks/from-template",
    tags=["Stacks"],
    summary="create_stack_from_template",
    operation_id="create_stack_from_template")
# NOTE: no response_model — this endpoint returns error() (result=None) on precheck/rollback
# failures, and a strict StackResponse response_model would reject that with a ResponseValidationError
# (masking the real message as a generic 500).
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
            vm, secret_map = compile_refs_in_volume_mounts(
                merged.get("volume_mounts"), secret_map,
                pod_id_by_name, net_by_name, g.site_id, g.request_tenant_id,
            )
            if vm is not None:
                merged["volume_mounts"] = vm
            dep_ids = [pod_id_by_name[d] for d in (merged.get("depends_on") or [])]

            pod_kwargs = {}
            for k, v in merged.items():
                if k in HANDLED or v is None:
                    continue
                # A nested model left unset in the template serializes to all-None (e.g. an unset
                # `resources` -> {cpu_request: None, ...}), and NewPod rejects explicit None for those
                # int fields. Strip None sub-values and drop a now-empty dict so NewPod uses its own
                # default. (Inner models like networking/healthchecks accept their own None sub-fields.)
                if isinstance(v, dict):
                    v = {sk: sv for sk, sv in v.items() if sv is not None}
                    if not v:
                        continue
                pod_kwargs[k] = v
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

# Template-authoring networking keys. A live pod's networking dict carries runtime-only keys the
# template Networking model forbids (custom_domain, custom_domain_verified, ...), so a verbatim copy
# would fail tag validation. sanitize_member_networking (pure, in stack_template_utils) keeps only
# these template-valid keys when snapshotting.
_TEMPLATE_NET_FIELDS = set(
    getattr(NewTemplateTagNetworking, "model_fields", None) or NewTemplateTagNetworking.__fields__
)


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

    # Service defaults to prune snapshots against (so a captured member reads like an authored one,
    # not the fully-resolved live pod). ${...} references and non-default values are always kept.
    net_entry_defaults = NewTemplateTagNetworking().dict()

    member_defs = []
    for pod in members:
        d = pod.dict()
        member = {"name": pod_id_to_name[pod.pod_id]}
        for f in _MEMBER_POD_FIELDS:
            v = d.get(f)
            if v in (None, {}, [], "", "default"):
                continue
            member[f] = v
        if member.get("networking"):
            member["networking"] = sanitize_member_networking(member["networking"], _TEMPLATE_NET_FIELDS)
            # reset baked tapis_auth_allowed_users → AUTHORIZED_USERS sentinel, drop default fields
            member["networking"] = minimize_member_networking(member["networking"], net_entry_defaults)
        if member.get("resources"):
            member["resources"] = minimize_resources(member["resources"])
            if not member["resources"]:
                member.pop("resources", None)
        # rewrite cross-references + scrub secrets
        if member.get("environment_variables"):
            member["environment_variables"] = {k: unbake_host_refs(v, k8_to_ref) for k, v in member["environment_variables"].items()}
        if member.get("secret_map"):
            member["secret_map"] = {k: placeholderize_secret_value(unbake_host_refs(v, k8_to_ref)) for k, v in member["secret_map"].items()}
        # config_content: un-bake in-cluster hostnames back into ${stack:<member>:host}
        # refs so the saved template stays deployable under any stack_id. (Stored
        # config_content holds ${pods:secrets:...} references, never resolved values,
        # so no secret scrubbing is needed here.)
        if member.get("volume_mounts"):
            member["volume_mounts"] = {
                mp: ({**mnt, "config_content": unbake_host_refs(mnt["config_content"], k8_to_ref)}
                     if isinstance(mnt, dict) and isinstance(mnt.get("config_content"), str)
                     else mnt)
                for mp, mnt in member["volume_mounts"].items()
            }
        # keep ready_condition / depends_on only when non-default / non-empty
        rc = pod.ready_condition or "available"
        if rc != "available":
            member["ready_condition"] = rc
        deps = [pod_id_to_name.get(dep, dep) for dep in (pod.depends_on or [])]
        if deps:
            member["depends_on"] = deps
        member_defs.append(member)

    # Un-bake only rewrites hostnames it can attribute to a CURRENT member —
    # surface anything left literal so the author reviews before reuse.
    unbake_warnings = find_residual_host_refs(member_defs, g.site_id, g.request_tenant_id)

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
    resp = await add_template_tag(body.template_id, new_tag)
    if unbake_warnings and isinstance(resp, dict):
        warn = (
            "Heads up: some in-cluster hostnames could not be un-baked into "
            "${stack:<member>:host} references and were saved literally. This "
            "template will only work where those exact services exist — review "
            "and replace them with refs (or public URLs) if this should be "
            "reusable. Left as-is in: " + "; ".join(unbake_warnings)
        )
        resp["message"] = f"{resp.get('message', '')} {warn}".strip()
        meta = resp.get("metadata") or {}
        meta["unbake_warnings"] = unbake_warnings
        resp["metadata"] = meta
    return resp


#### /pods/stacks/{stack_id}/update — reviewed update from a newer stack-template tag


def _members_from_tag(template_ref):
    """Resolve a stack-template ref -> (template_str, members, secret_map, restart_policy).

    Raises ValueError if the ref does not resolve to a kind='stack' tag.
    """
    template_str, _t, tag = derive_template_info(template_ref, tenant=g.request_tenant_id, site=g.site_id)
    if not tag or getattr(tag, "kind", "pod") != "stack" or not getattr(tag, "stack_definition", None):
        raise ValueError(f"'{template_ref}' is not a stack template (kind='stack').")
    sd_obj = tag.stack_definition
    sd = sd_obj.dict() if hasattr(sd_obj, "dict") else dict(sd_obj or {})
    return template_str, (sd.get("members") or []), dict(sd.get("secret_map") or {}), sd.get("restart_policy", "ordered")


def _build_member_new_pod(merged, pid, stack_id, pod_id_by_name, net_by_name):
    """Compile one member's ${stack:...} refs and build a NewPod — mirrors the from-template block."""
    HANDLED = {"name", "depends_on", "environment_variables", "secret_map"}
    env, secret_map = compile_member_stack_refs(
        merged.get("environment_variables") or {},
        merged.get("secret_map") or {},
        pod_id_by_name, net_by_name, g.site_id, g.request_tenant_id,
    )
    vm, secret_map = compile_refs_in_volume_mounts(
        merged.get("volume_mounts"), secret_map,
        pod_id_by_name, net_by_name, g.site_id, g.request_tenant_id,
    )
    if vm is not None:
        merged["volume_mounts"] = vm
    dep_ids = [pod_id_by_name[d] for d in (merged.get("depends_on") or []) if d in pod_id_by_name]
    pod_kwargs = {}
    for k, v in merged.items():
        if k in HANDLED or v is None:
            continue
        if isinstance(v, dict):
            v = {sk: sv for sk, sv in v.items() if sv is not None}
            if not v:
                continue
        pod_kwargs[k] = v
    pod_kwargs.update({
        "pod_id": pid,
        "stack_id": stack_id,
        "depends_on": dep_ids or None,
        "ready_condition": merged.get("ready_condition") or "available",
        "environment_variables": env,
        "secret_map": secret_map,
    })
    return NewPod(**{k: v for k, v in pod_kwargs.items() if k in NewPod.__fields__})


@router.post(
    "/pods/stacks/{stack_id}/update",
    tags=["Stacks"],
    summary="update_stack_from_template",
    operation_id="update_stack_from_template")
# NOTE: no response_model — dry_run returns a plan dict and failures return error() (result=None),
# which a strict StackResponse response_model would reject (masking the real message as a 500).
async def update_stack_from_template(stack_id, body: StackUpdateRequest, dry_run: bool = False):
    """
    Re-derive a stack from a newer stack-template tag — the L2 *reviewed* update.

    With `?dry_run=true`: returns the per-member plan (add/remove/recreate/patch) + whether a typed
    confirm is required, with **no side effects**. Without dry_run: applies it — create new members,
    patch or recreate changed ones, delete removed ones (gated by `confirm` == stack_id when the plan
    is destructive), then re-pin `from_template` to the target tag.

    User overrides on UNCHANGED fields are preserved (patch only touches changed fields). `recreate`
    rebuilds a member from the template; full per-field 3-way merge on recreate is a documented
    follow-up (see tapis-ui STACK_UPDATE_MODEL.md). Apply order is create -> patch -> remove so a
    mid-apply failure never destroys data before additions succeed; re-run to converge.
    """
    logger.info(f"POST /pods/stacks/{stack_id}/update - Top (template={body.template}, dry_run={dry_run}).")
    from api_pods import create_pod  # lazy import to avoid an import cycle

    stack = Stack.db_get_with_pk(stack_id, tenant=g.request_tenant_id, site=g.site_id)
    if not stack:
        return error(msg=f"Stack '{stack_id}' not found.")
    if not stack.from_template:
        return error(msg=f"Stack '{stack_id}' was not created from a template; nothing to update from.")

    prev_ref = stack.from_template
    try:
        _old_str, old_members, _old_secrets, _old_rp = _members_from_tag(prev_ref)
    except Exception as e:
        return error(msg=f"Could not resolve the stack's pinned template '{prev_ref}': {e}")

    target_ref = body.template or prev_ref
    try:
        target_str, new_members, new_secret_map, new_restart_policy = _members_from_tag(target_ref)
    except Exception as e:
        return error(msg=f"Could not resolve target template '{target_ref}': {e}")

    plan = compute_stack_member_plan(old_members, new_members)
    requires_confirm = any(p["destructive"] for p in plan)
    has_changes = any(p["kind"] != "unchanged" for p in plan)

    # name -> pod_id: existing members keep their live id (matched robustly, tolerating
    # legacy pod-id drift so a member whose pod_id doesn't follow '{stack_id}{name}' is
    # reused rather than duplicated+orphaned); new members derive '{stack_id}{name}' or override.
    live = _get_stack_members(stack_id)
    member_images = {}
    for m in (old_members + new_members):
        nm = m.get("name")
        if nm and nm not in member_images:
            member_images[nm] = m.get("image") or ""
    kind_by_name = {p["name"]: p["kind"] for p in plan}
    name_to_pid = match_live_member_pod_ids(stack_id, member_images, kind_by_name, live)
    new_by_name = {m["name"]: m for m in new_members if m.get("name")}
    pod_ids_override = body.pod_ids or {}
    pod_id_by_name = dict(name_to_pid)
    for nm in new_by_name:
        pod_id_by_name.setdefault(nm, pod_ids_override.get(nm) or f"{stack_id}{nm}")
    net_by_name = {m["name"]: (m.get("networking") or {}) for m in new_members}

    for p in plan:
        p["pod_id"] = pod_id_by_name.get(p["name"])

    if dry_run:
        # Required secret placeholders the target tag introduces that the stack doesn't have
        # yet — the caller must supply these in `secrets` or the apply can't resolve members.
        existing_secret_keys = set((stack.secret_map or {}).keys())
        new_secrets = []
        for k, v in (new_secret_map or {}).items():
            sv = str(v or "").strip()
            if k not in existing_secret_keys and sv.startswith("${:?"):
                desc = sv[4:-1] if sv.endswith("}") else sv[4:]
                new_secrets.append({"key": k, "description": desc})
        return ok(
            result={
                "from": prev_ref,
                "to": target_str,
                "members": plan,
                "has_destructive": requires_confirm,
                "requires_confirm": requires_confirm,
                "new_secrets": new_secrets,
            },
            msg=f"Update plan for '{stack_id}': {prev_ref} -> {target_str}.",
        )

    # These are apply-blockers the client can act on — return a real 4xx (not a 200 with an
    # error envelope) so callers don't mistake them for success. Body keeps the Tapis envelope.
    if not has_changes:
        return JSONResponse(status_code=400, content=error(
            msg="Target template is identical to the current one; nothing to apply."))
    if requires_confirm and (body.confirm or "") != stack_id:
        return JSONResponse(status_code=409, content=error(
            msg=f"This update is destructive; pass confirm='{stack_id}' to apply."))
    # A destructive plan deletes/recreates member pods — require stack ADMIN, consistent with
    # delete_stack and DELETE /pods/{id} (route-level USER is enough for additive/patch updates).
    if requires_confirm and not getattr(g, 'admin', False) and not check_permissions(g.username, ADMIN, stack, "stack", roles=g.roles):
        return JSONResponse(status_code=403, content=error(
            msg=f"This update deletes/recreates member pods, which requires ADMIN permission on stack '{stack_id}'."))

    # Merge new + supplied shared secrets into the stack BEFORE building members, so member
    # refs like ${stack:secrets:TAPIS_USERNAME} that the target tag introduces can resolve
    # during create/recreate. (Persisting early is safe + idempotent: on a mid-apply failure
    # the secrets are simply already present when you re-run to converge.)
    merged_secret_map = dict(stack.secret_map or {})
    for k, v in new_secret_map.items():
        merged_secret_map.setdefault(k, v)
    # Same write-time ownership gate as create (pin short refs, reject cross-user explicit refs)
    # on the caller-supplied secrets before they enter the shared, actor=None-resolved map.
    user_secrets = expand_short_secret_references(body.secrets or {}, g.username)
    own_errors = validate_secret_map_ownership(user_secrets, g.username)
    if own_errors:
        return JSONResponse(status_code=400, content=error(msg="; ".join(own_errors)))
    merged_secret_map.update(user_secrets)
    if merged_secret_map != (stack.secret_map or {}):
        stack.secret_map = merged_secret_map
        stack.db_update()

    by_kind = {}
    for p in plan:
        by_kind.setdefault(p["kind"], []).append(p)
    applied = {"add": [], "patch": [], "recreate": [], "remove": []}

    try:
        # create + recreate first (recreate = delete old, then create fresh), in dependency order.
        for kind in ("add", "recreate"):
            affected = {p["name"] for p in by_kind.get(kind, [])}
            for m in topo_order([new_by_name[n] for n in affected if n in new_by_name]):
                name = m["name"]
                pid = pod_id_by_name[name]
                if kind == "recreate":
                    old_pod = Pod.db_get_with_pk(pid, tenant=g.request_tenant_id, site=g.site_id)
                    old_pw = Password.db_get_with_pk(pid, tenant=g.request_tenant_id, site=g.site_id)
                    if old_pod:
                        delete_pod_resources(old_pod, old_pw)
                await create_pod(_build_member_new_pod(dict(m), pid, stack_id, pod_id_by_name, net_by_name))
                applied[kind].append(pid)

        # patch: apply only the changed PATCH fields to the live pod, then restart it.
        for p in by_kind.get("patch", []):
            name = p["name"]
            pid = pod_id_by_name[name]
            pod = Pod.db_get_with_pk(pid, tenant=g.request_tenant_id, site=g.site_id)
            if not pod:
                continue
            m = new_by_name[name]
            env, secret_map = compile_member_stack_refs(
                m.get("environment_variables") or {}, m.get("secret_map") or {},
                pod_id_by_name, net_by_name, g.site_id, g.request_tenant_id,
            )
            vm, secret_map = compile_refs_in_volume_mounts(
                m.get("volume_mounts"), secret_map,
                pod_id_by_name, net_by_name, g.site_id, g.request_tenant_id,
            )
            for f in p["changed_fields"]:
                if f == "environment_variables":
                    pod.environment_variables = env
                elif f == "secret_map":
                    pod.secret_map = secret_map
                elif f == "volume_mounts":
                    pod.volume_mounts = vm if vm is not None else m.get("volume_mounts")
                elif f == "depends_on":
                    # Rebuild from the template, but PRESERVE manual edges to
                    # non-template pods (adopted/unmanaged members) — the template
                    # can't name them, and dropping them would silently sever
                    # start-ordering the user wired by hand.
                    template_deps = [pod_id_by_name[d] for d in (m.get("depends_on") or [])
                                     if d in pod_id_by_name]
                    template_ids = set(pod_id_by_name.values())
                    manual_deps = [d for d in (pod.depends_on or [])
                                   if d not in template_ids and d not in template_deps]
                    pod.depends_on = (template_deps + manual_deps) or None
                elif f == "ready_condition":
                    pod.ready_condition = m.get("ready_condition") or "available"
                elif hasattr(pod, f) and m.get(f) is not None:
                    setattr(pod, f, m.get(f))
            pod.status_requested = RESTART
            pod.db_update(f"'{g.username}' updated member '{name}' from {target_str} (patch: {p['changed_fields']})")
            applied["patch"].append(pid)

        # remove last (destructive — confirmed above).
        for p in by_kind.get("remove", []):
            pid = pod_id_by_name.get(p["name"]) or name_to_pid.get(p["name"])
            if not pid:
                continue
            rp = Pod.db_get_with_pk(pid, tenant=g.request_tenant_id, site=g.site_id)
            rpw = Password.db_get_with_pk(pid, tenant=g.request_tenant_id, site=g.site_id)
            if rp:
                delete_pod_resources(rp, rpw)
            applied["remove"].append(pid)
    except Exception as e:
        logger.error(f"stack '{stack_id}' update partially applied then failed: {e}. Applied: {applied}")
        return error(msg=f"Update failed mid-apply ({e}). Applied so far: {applied}. Re-run to converge.")

    # Re-pin provenance (shared secrets were already merged + persisted before the apply loop).
    stack.from_template = target_str
    if new_restart_policy in VALID_RESTART_POLICIES:
        stack.restart_policy = new_restart_policy
    _stack_log(stack, f"'{g.username}' updated stack {prev_ref} -> {target_str} "
                      f"(+{len(applied['add'])} ~{len(applied['patch'])} "
                      f"recreate{len(applied['recreate'])} -{len(applied['remove'])})")
    stack.db_update()

    members_out = _get_stack_members(stack_id)
    result = stack.display()
    result["pods"] = [pp.display() for pp in members_out]
    return ok(result=result, metadata={"applied": applied, "to": target_str},
              msg=f"Stack '{stack_id}' updated to '{target_str}'.")
