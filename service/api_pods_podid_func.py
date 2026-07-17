from fastapi import APIRouter, Request, UploadFile, File, Form, Body, Path, Query
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, HTMLResponse
from models_pods import Pod, Password, PodResponse, PodPermissionsResponse, PodCredentialsResponse, PodLogsResponse, ExecutePodCommands, PodBaseFull
from models_traffic import TrafficLog, TrafficLogsResponse
from models_pod_log_runs import PodLogRun, PodLogRunsResponse, PodLogRunResponse
from models_pod_access_tokens import (
    PodAccessToken, PodAccessTokensResponse, PodAccessTokenMintResponse, AccessTokenMintRequest,
)
from log_archive_utils import read_archive
from models_templates_tags import Template, TemplateTag, TemplateTagResponse, NewTemplateTagFromPod, Networking as TemplateTagNetworking
from models_templates_utils import combine_pod_and_template_recursively
from models_misc import SetPermission
from channels import CommandChannel
from codes import OFF, ON, RESTART, REQUESTED, STOPPED, USER, ADMIN, READ, APPROVEDADMIN, PermissionLevel
from secret_utils import resolve_secret_map
import requests
from tapisservice.tapisfastapi.utils import g, ok, error
from tapisservice.config import conf
from __init__ import t, BadRequestError
from typing import List, Any
from kubernetes_utils import run_k8_exec, k8s_copy_bytes_to_pod, NAMESPACE, k8, configmap_exists, get_pod_k8s_metrics
from utils import check_permissions
from errors import ResourceError, PermissionsException
from models_volume_mounts_utils import validate_volume_mounts_on_start
from stack_template_utils import sanitize_member_networking, placeholderize_secret_value
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, quote
import time
import re
import html as _html
import asyncio
import io
import jwt

# Special group strings for tapis_auth_allowed_users.
# These resolve against the pod's permissions list at auth time.
# Maps each group string to the minimum PermissionLevel required.
AUTH_GROUP_LEVEL_MAP = {
    "AUTHORIZED_READS": READ,         # READ, USER, ADMIN, APPROVEDADMIN
    "AUTHORIZED_USERS": USER,         # USER, ADMIN, APPROVEDADMIN
    "AUTHORIZED_ADMINS": ADMIN,       # ADMIN, APPROVEDADMIN
}


def check_tapis_auth_allowed(username: str, tapis_auth_allowed_users: list, pod_permissions: dict) -> bool:
    """Check if a username is allowed by tapis_auth_allowed_users.

    Supports:
    - "*" wildcard: all authenticated users allowed.
    - Literal usernames: exact match (case-insensitive).
    - Special group strings that resolve against pod permissions:
        AUTHORIZED_READS  -> users with READ or higher permission on the pod
        AUTHORIZED_USERS  -> users with USER or higher permission on the pod
        AUTHORIZED_ADMINS -> users with ADMIN or higher (including APPROVEDADMIN) permission on the pod

    Args:
        username: The authenticated Tapis username.
        tapis_auth_allowed_users: The networking.tapis_auth_allowed_users list.
        pod_permissions: Dict from pod.get_permissions(), e.g. {"user1": "ADMIN", "user2": "READ"}.

    Returns:
        True if the user is allowed, False otherwise.
    """
    if not tapis_auth_allowed_users:
        return True  # empty list = no restriction

    username_lower = username.lower()

    # Check for wildcard
    if "*" in tapis_auth_allowed_users:
        return True

    # Check for literal username match
    if username_lower in [u.lower() for u in tapis_auth_allowed_users if u not in AUTH_GROUP_LEVEL_MAP and u != "*"]:
        return True

    # Check special group strings against pod permissions
    user_perm_str = pod_permissions.get(username_lower) or pod_permissions.get(username)
    if user_perm_str:
        user_level = PermissionLevel(user_perm_str)
        for group_str in tapis_auth_allowed_users:
            required_level = AUTH_GROUP_LEVEL_MAP.get(group_str)
            if required_level is not None and user_level >= required_level:
                return True

    return False


def get_allowed_tenants_from_permissions(pod_permissions: dict) -> list:
    """Extract allowed tenant IDs from pod permissions.

    Scans pod permissions for entries matching the 'tenant.<tenant_id>' pattern
    and returns a list of the tenant IDs.

    Args:
        pod_permissions: Dict from pod.get_permissions(), e.g. {"user1": "ADMIN", "tenant.public": "USER"}.

    Returns:
        List of tenant ID strings, e.g. ["public", "dev"].
    """
    tenants = []
    for key in pod_permissions:
        if key.startswith("tenant."):
            tenant_id = key[len("tenant."):]
            if tenant_id:
                tenants.append(tenant_id)
    return tenants


def check_tapis_auth_tenant_allowed(token_tenant_id: str, request_tenant_id: str, tapis_auth_allowed_tenants: list) -> bool:
    """Check if a token's tenant is allowed to access a pod.

    The pod's own tenant (request_tenant_id) is always allowed.
    Additional tenants can be allowed via the pod's permissions list
    (extracted by get_allowed_tenants_from_permissions()).

    Args:
        token_tenant_id: The tenant_id from the JWT token (e.g. 'public').
        request_tenant_id: The pod's host tenant from the URL (e.g. 'tacc').
        tapis_auth_allowed_tenants: List of additional tenant IDs allowed (from pod permissions).

    Returns:
        True if the token's tenant is allowed, False otherwise.
    """
    if not token_tenant_id:
        return True  # no token tenant info = allow (will be caught by token validation)
    # Pod's own tenant is always allowed
    if token_tenant_id == request_tenant_id:
        return True
    # Check against allowed tenants list
    if token_tenant_id in tapis_auth_allowed_tenants:
        return True
    return False


CHUNK_TIMEOUT = 60  # seconds per chunk
CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunkscv

from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods/{pod_id}/functionHere

@router.get(
    "/pods/{pod_id}/credentials",
    tags=["Pods"],
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
    tags=["Pods"],
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
    "/pods/{pod_id}/traffic",
    tags=["Pods"],
    summary="get_pod_traffic",
    operation_id="get_pod_traffic",
    response_model=TrafficLogsResponse)
async def get_pod_traffic(
    pod_id,
    limit: int = Query(default=100, ge=1, le=1000, description="Max rows to return."),
    method: str = Query(default=None, description="Filter by HTTP method (GET, POST, ...)."),
    status_class: str = Query(default=None, description="Filter by status class: 2xx, 3xx, 4xx, 5xx."),
    status_code: int = Query(default=None, description="Filter by exact HTTP status code."),
    username: str = Query(default=None, description="Filter by authenticated Tapis username."),
    since: datetime = Query(default=None, description="Only return entries at or after this UTC ISO8601 timestamp."),
    until: datetime = Query(default=None, description="Only return entries at or before this UTC ISO8601 timestamp."),
):
    """
    Get recent Traefik network traffic for a pod.

    Traffic entries are collected from Traefik access logs on each health tick.
    When tapis_auth is active on the pod, the username is extracted from the X-Tapis-User header.
    Otherwise source IP and raw headers are recorded.

    Returns up to `limit` most-recent matching entries in reverse chronological order.
    """
    logger.info(f"GET /pods/{pod_id}/traffic - Top of get_pod_traffic.")
    Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)  # 404 if not found

    rows = TrafficLog.get_recent(
        pod_id=pod_id,
        tenant=g.request_tenant_id,
        site=g.site_id,
        limit=limit,
        method=method,
        status_class=status_class,
        status_code=status_code,
        username=username,
        since=since,
        until=until,
    )
    return ok(result=[r.dict() for r in rows], msg="Pod traffic retrieved successfully.")


@router.get(
    "/pods/{pod_id}/log-runs",
    tags=["Pods"],
    summary="list_pod_log_runs",
    operation_id="list_pod_log_runs",
    response_model=PodLogRunsResponse)
async def list_pod_log_runs(pod_id):
    """
    List metadata for all persisted log runs of a pod.

    Returns run index, start/stop times, size, and archive status.
    Does NOT return log content — use GET /pods/{pod_id}/log-runs/{run_index} for that.
    """
    logger.info(f"GET /pods/{pod_id}/log-runs - Top of list_pod_log_runs.")
    Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    runs = PodLogRun.list_runs(pod_id, g.request_tenant_id, g.site_id)
    return ok(result=[r.display_meta() for r in runs], msg="Pod log runs retrieved successfully.")


@router.get(
    "/pods/{pod_id}/log-runs/{run_index}",
    tags=["Pods"],
    summary="get_pod_log_run",
    operation_id="get_pod_log_run",
    response_model=PodLogRunResponse)
async def get_pod_log_run(pod_id, run_index: int):
    """
    Get the full log content for a specific pod run.

    Active runs return live log content from the database.
    Archived runs read from the on-disk gzip archive. Returns 404 if the archive
    file is missing.
    """
    logger.info(f"GET /pods/{pod_id}/log-runs/{run_index} - Top of get_pod_log_run.")
    Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    run = PodLogRun.get_run_by_index(pod_id, run_index, g.request_tenant_id, g.site_id)
    if not run:
        raise ResourceError(f"Log run #{run_index} not found for pod '{pod_id}'.", 404)

    result = run.display()
    if run.is_archived and not run.logs:
        if not run.archive_path:
            raise ResourceError(f"Log run #{run_index} is archived but archive_path is not set.", 400)
        import os
        if not os.path.exists(run.archive_path):
            raise ResourceError(f"Archive file for run #{run_index} not found on disk: {run.archive_path}", 404)
        result['logs'] = read_archive(run.archive_path)

    return ok(result=result, msg=f"Pod log run #{run_index} retrieved successfully.")


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

    Permission formats:
    - username:LEVEL - Standard user permission (e.g., 'jsmith:READ')
    - tenant.<tenant_id>:READ - Cross-tenant auth permission. Allows users from <tenant_id> to authenticate
      to this pod via tapis_auth. Only settable by admins. Must use READ level. ex. tenant.public, tenant.dev

    Notes:
    - 'tenant.*' permissions require admin privileges (like '**' on templates)
    - 'tenant.*' permissions only support READ level (they gate cross-tenant auth access, not authorization)
    - There are 3 levels of permissions, READ, USER, and ADMIN.

    Returns updated pod permissions.
    """
    logger.info(f"POST /pods/{pod_id}/permissions - Top of set_pod_permissions.")

    inp_user = set_permission.user
    inp_level = set_permission.level

    # Admin-only check for tenant-wide 'tenant.*' permissions
    if inp_user.startswith("tenant.") and not g.admin:
        raise PermissionsException("Only admins can set cross-tenant 'tenant.*' permissions on pods.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Validate tenant permission format
    if inp_user.startswith("tenant."):
        tenant_id = inp_user[len("tenant."):]
        if not tenant_id:
            raise ValueError("tenant. permission must include a tenant ID. e.g. 'tenant.public'")
        if not tenant_id.isascii():
            raise ValueError(f"tenant. permission tenant ID must be ASCII. Got '{tenant_id}'.")
        res = re.fullmatch(r'[a-z][a-z0-9-]*', tenant_id)
        if not res:
            raise ValueError(f"tenant. permission tenant ID must be lowercase alphanumeric (with hyphens). Got '{tenant_id}'.")
        if len(tenant_id) > 64:
            raise ValueError(f"tenant. permission tenant ID must be less than 64 characters. Got length {len(tenant_id)}.")
        if inp_level != "READ":
            raise ValueError(f"tenant.* permissions only support READ level (cross-tenant auth gate). Got '{inp_level}'.")

    # Get formatted perms
    curr_perms = pod.get_permissions()

    # Update variable
    curr_perms[inp_user] = inp_level

    # Ensure there's still an admin-capable user before finishing. APPROVEDADMIN is ADMIN+
    # (see tapis_auth_allowed_users AUTHORIZED_ADMINS), so it satisfies the invariant too —
    # otherwise a sole owner could never promote themselves to APPROVEDADMIN (needed to set
    # TAPIS_PODS_IMAGEPULLSECRET for private images).
    if not any(level in ("ADMIN", "APPROVEDADMIN") for level in curr_perms.values()):
        raise ResourceError("Operation would leave the pod with no ADMIN-capable user. Rolling back.", 400)

    # Convert back to db format
    perm_list = []
    for user, level in curr_perms.items():
        perm_list.append(f"{user}:{level}")

    # Update pod object and commit
    pod.permissions = perm_list
    pod.db_update(f"'{g.username}' set permission for '{inp_user}' to {inp_level}")

    return ok(result={"permissions": pod.permissions}, msg = "Pod permissions updated successfully.")


@router.post(
    "/pods/{pod_id}/exec",
    tags=["Pods"],
    summary="exec_pod_commands",
    operation_id="exec_pod_commands")
async def exec_pod_commands(pod_id, command: ExecutePodCommands):
    """
    Execute one or more commands in a pod.
    
    Accepts either:
    - Single command: ["sleep", "5"]
    - Multiple commands: [["sleep", "5"], ["echo", "hello"]]
    
    Executes commands synchronously in the pod:
    - Each command runs sequentially
    - Total request time = sum of all command execution times
    - Request remains open until all commands complete
    - Returns consolidated results for all commands

    Response includes:
    - Individual command outputs
    - Success/failure status
    - Execution duration
    """
    logger.info(f"POST /pods/{pod_id}/exec - Top of exec_pod_command.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    metadata = {}
    # Normalize input to list of commands
    commands = command.commands if isinstance(command.commands[0], list) else [command.commands]
    results = []
    start_time = time.time()
    fail_on_non_success = command.fail_on_non_success
    custom_msg = None

    for n, cmd in enumerate(commands):
        try:
            logger.debug(f"Running command ({n+1}/{len(commands)}) in pod {pod_id}: {cmd}")
            ## each command can use <<podssecret_*>> or <<TAPIS_*>>(legacy) variables, so we need to replace them with the values from the pod_env
            pods_env = Password.db_get_with_pk(pod.pod_id, pod.tenant_id, pod.site_id).dict()

            # Replace <<TAPIS_*>> variables in command
            if isinstance(cmd, list):
                cmd_before_replace = cmd
                new_cmd = []
                for item in cmd:
                    if isinstance(item, str):
                        # Find both TAPIS_ and tapissecret_ patterns
                        tapis_matches = re.findall(r'<<TAPIS_(.*?)>>', item)
                        tapissecret_matches = re.findall(r'<<tapissecret_(.*?)>>', item)
                        
                        new_item = item
                        # Handle TAPIS_ replacements
                        for match in tapis_matches:
                            new_item = new_item.replace(f"<<TAPIS_{match}>>", pods_env.get(match, ""))
                        
                        # Handle tapissecret_ replacements
                        for match in tapissecret_matches:
                            new_item = new_item.replace(f"<<tapissecret_{match}>>", pods_env.get(match, ""))
                            
                        new_cmd.append(new_item)
                    else:
                        new_cmd.append(item)
                cmd = new_cmd

            cmd_start_time = time.time()
            stdout, stderr, duration, status, success, exit_code = run_k8_exec(pod.k8_name, cmd, timeout=command.command_timeout)

            results.append({
                "command": cmd,
                "stdout": stdout,
                "stderr": stderr,
                "success": success if success else (status if status else False),
                "exit_code": exit_code,
                "duration_sec": round(duration, 3),
                "timestamp": datetime.utcnow().isoformat()
            })
        except Exception as e:
            logger.error(f"Error executing command {cmd} in pod {pod_id}: {e}")
            results.append({
                "command": cmd,
                "stdout": "",
                "stderr": str(e),
                "success": False,
                "duration_sec": 0,
                "timestamp": datetime.utcnow().isoformat()
            })

        # Check total timeout
        if time.time() - start_time > command.total_timeout:
            custom_msg = f"Execution stopped due to total timeout. Consider increasing the total_timeout parameter."

        if not type(results[-1]["success"]) == bool and "Timeout" in results[-1]["success"]:
            custom_msg = f"Execution stopped due to command timeout on latest command. Consider increasing the command_timeout parameter."
        elif fail_on_non_success and not results[-1]["success"]:
            custom_msg = f"Execution stopped due to command failure on latest command. Consider setting fail_on_non_success=False to continue through errors."

    # Build audit log entry.
    # Security: only log the executable name (r["command"][0]) — never
    # arguments, which may contain secret values after <<tapissecret_*>>
    # substitution.  stdout/stderr are also never stored here.
    success_count = sum(1 for r in results if r["success"] is True)
    total_duration = round(sum(r.get("duration_sec", 0) for r in results), 2)

    def _cmd_label(r: dict) -> str:
        cmd = r.get("command", [])
        exe = (cmd[0] if isinstance(cmd, list) and cmd else "exec")
        # Truncate very long binary paths to the basename
        exe = exe.split("/")[-1][:24]
        result_str = "ok" if r["success"] is True else f"exit {r.get('exit_code', '?')}"
        dur = round(r.get("duration_sec", 0), 2)
        return f"{exe}({result_str}, {dur}s)"

    cmd_parts = ", ".join(_cmd_label(r) for r in results)
    summary = f"'{g.username}' exec {success_count}/{len(results)} ok  [{cmd_parts}]  {total_duration}s total"
    if custom_msg:
        # Strip the "Consider …" advice — action log is for auditors, not users
        summary += f"  ({custom_msg.split(' Consider')[0]})"
    pod.db_update(summary)

    total_commands = len(commands)
    successful_commands = sum(1 for r in results if r["success"])

    # Create a more descriptive message about command success
    if successful_commands == total_commands:
        success_msg = "All Commands Successful"
    else:
        success_msg = f"{successful_commands} of {total_commands} commands successful"
    
    # Append custom message if exists
    if custom_msg:
        final_msg = f"{success_msg} - {custom_msg}"
    else:
        final_msg = success_msg

    return ok(
        result={
            "execution_results": results,
            "total_commands": total_commands,
            "successful_commands": successful_commands,
        },
        msg=final_msg
    )


@router.post(
    "/pods/{pod_id}/upload_to_pod",
    tags=["Pods"],
    summary="Upload a file directly into the pod's filesystem",
    operation_id="upload_to_pod",
)
async def upload_to_pod(
    pod_id: str,
    file: UploadFile = File(...),
    dest_path: str = Form(...)
):
    """
    Upload a file to a specific path inside the pod using Kubernetes exec.
    
    Notes:
    - Pod must have /bin/sh available (most standard images include this)
    - Distroless or minimal images without a shell will not work with this endpoint.
    """
    logger.info(f"POST /pods/{pod_id}/upload_to_pod - Top of upload_to_pod.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    if not pod or getattr(pod, "status_requested", None) != "ON":
        return JSONResponse(content="Can't find suitable running pod for upload.", status_code=404)

    if not dest_path:
        return JSONResponse(content=f"Destination path is required, got: {dest_path}", status_code=400)

    # Open exec session to pod
    from kubernetes.stream import stream
    from kubernetes import client as k8s_client
    exec_command = ['/bin/sh', '-c', f'cat > {dest_path}']
    api = k8s_client.CoreV1Api()
    try:
        resp = stream(
            api.connect_get_namespaced_pod_exec,
            pod.k8_name,
            NAMESPACE,
            command=exec_command,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
            container=None,  # or pod.k8_container if needed
            _preload_content=False
        )

        # Read and send file in chunks
        while True:
            chunk = await file.read(2 * 1024 * 1024)  # 2MB chunks - not too worried about memory quite yet
            if not chunk:
                break
            resp.write_stdin(chunk)
        resp.close()
    except Exception as e:
        logger.error(f"Error uploading file to pod: {e}")
        return JSONResponse(content=f"Failed to upload file to pod: {e}", status_code=500)

    return ok(result={"uploaded": dest_path, "pod_name": pod.pod_id}, msg="File uploaded to pod successfully.")

@router.get(
    "/pods/{pod_id}/list_files{url_path:path}",
    tags=["Pods"],
    summary="List files in the pod's filesystem",
    operation_id="list_files_in_pod",
)
async def list_files_in_pod(
    pod_id: str = Path(..., description="Unique identifier for the pod."),
    url_path: str = Path(..., description="Path to list files from inside the pod."),
    path: str = Query(None, description="Alternative query parameter for path.")
):
    """
    List files and directories at a specific path inside the pod using Kubernetes exec.
    
    Path options (use one, not both):
    - URL path: Relative paths only (e.g., /list_files/mydir -> "mydir")
    - Query parameter: Absolute paths allowed (e.g., ?path=/tmp -> "/tmp")
    
    Notes:
    - Pod must have /bin/sh and ls available (most standard images include these)
    - Distroless or minimal images without a shell will return a 500 error.
    """
    logger.info(f"GET /pods/{pod_id}/list_files - Top of list_files_in_pod.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    if not pod or getattr(pod, "status_requested", None) != "ON":
        return JSONResponse(content="Can't find suitable running pod for listing files.", status_code=404)

    # Check that exactly one of url_path or path is provided
    if url_path and path:
        return JSONResponse(
            content="Error: Provide path either in URL path or as query parameter, not both.",
            status_code=400
        )
    elif not url_path and not path:
        return JSONResponse(
            content=f"Error: Path is required. Provide path either in URL path or as query parameter. path {path} ",
            status_code=400
        )

    # Set source_path from whichever was provided
    source_path = url_path.lstrip('/') if url_path else path
    
    # Default to current directory if empty
    if not source_path:
        source_path = "."

    try:
        from kubernetes.stream import stream
        from kubernetes import client as k8s_client
        from dateutil import parser as date_parser
        
        api = k8s_client.CoreV1Api()
        
        # First check if path exists and is accessible
        check_command = ['/bin/sh', '-c', f'test -e "{source_path}" && echo "EXISTS" || echo "NOT_FOUND"']
        
        check_resp = stream(
            api.connect_get_namespaced_pod_exec,
            pod.k8_name,
            NAMESPACE,
            command=check_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            container=None,
            _preload_content=False
        )
        
        check_output = ""
        check_error = ""
        while check_resp.is_open():
            check_resp.update(timeout=1)
            if check_resp.peek_stdout():
                check_output += check_resp.read_stdout()
            if check_resp.peek_stderr():
                check_error += check_resp.read_stderr()
        check_resp.close()
        
        # Check if /bin/sh is not available
        if check_error and ("not found" in check_error.lower() or "no such file" in check_error.lower()):
            return JSONResponse(
                content={
                    "error": "Shell (/bin/sh) is not available in this pod",
                    "details": check_error,
                    "suggestion": "This pod may be using a minimal or distroless base image without a shell. Consider using a pod with /bin/sh or /bin/bash available, use /exec endpoint rather than list_files_in_pod."
                },
                status_code=500
            )
        
        if "NOT_FOUND" in check_output.strip():
            return JSONResponse(
                content=f"Path not found or not accessible: {source_path}", 
                status_code=404
            )
        
        # List files with full details using ls with specific format
        # Using --full-time for ISO 8601 timestamps
        list_command = ['/bin/sh', '-c', f'ls -lAh --full-time "{source_path}" 2>&1']
        
        list_resp = stream(
            api.connect_get_namespaced_pod_exec,
            pod.k8_name,
            NAMESPACE,
            command=list_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            container=None,
            _preload_content=False
        )
        
        listing_output = ""
        error_output = ""
        while list_resp.is_open():
            list_resp.update(timeout=5)
            if list_resp.peek_stdout():
                listing_output += list_resp.read_stdout()
            if list_resp.peek_stderr():
                error_output += list_resp.read_stderr()
        list_resp.close()
        
        if error_output or "cannot access" in listing_output.lower():
            logger.error(f"Error listing files: {error_output or listing_output}")
            return JSONResponse(
                content=f"Error listing files: {error_output or listing_output}",
                status_code=500
            )
        
        # Parse the ls output into a structured format
        files = []
        lines = listing_output.strip().split('\n')
        
        # Skip the first line if it's "total X"
        start_idx = 1 if lines and lines[0].startswith('total') else 0
        
        for line in lines[start_idx:]:
            if not line.strip():
                continue
            
            # Parse ls -lAh --full-time output
            # Format: permissions links owner group size date time timezone name
            parts = line.split(None, 8)
            if len(parts) >= 9:
                permissions = parts[0]
                owner = parts[2]
                group = parts[3]
                size_str = parts[4]
                
                # Parse timestamp (parts[5] is date, parts[6] is time with timezone)
                try:
                    datetime_str = f"{parts[5]} {parts[6]}"
                    dt = date_parser.parse(datetime_str)
                    last_modified = dt.isoformat()
                except:
                    last_modified = f"{parts[5]}T{parts[6]}"
                
                name = parts[8]
                
                # Determine file type
                if permissions.startswith('l'):
                    file_type = "symbolic_link"
                    # Extract actual name from "name -> target"
                    if ' -> ' in name:
                        name = name.split(' -> ')[0]
                elif permissions.startswith('d'):
                    file_type = "dir"
                elif permissions.startswith('-'):
                    file_type = "file"
                else:
                    file_type = "other"
                
                # Convert size to bytes if possible
                try:
                    size = int(size_str) if size_str.isdigit() else size_str
                except:
                    size = size_str
                
                # Construct proper path
                if source_path == "/":
                    file_path = f"/{name}"
                elif source_path == ".":
                    file_path = f"./{name}"
                else:
                    # Remove trailing slash from source_path to prevent double slashes
                    clean_source_path = source_path.rstrip('/')
                    if clean_source_path.startswith("/"):
                        file_path = f"{clean_source_path}/{name}"
                    else:
                        file_path = f"{clean_source_path}/{name}"
                
                files.append({
                    "name": name,
                    "path": file_path,
                    "type": file_type,
                    "size": size,
                    "owner": owner,
                    "group": group,
                    "nativePermissions": permissions,
                    "lastModified": last_modified,
                    "mimeType": None
                })
            
        logger.info(f"Successfully listed {len(files)} items in {source_path} from pod {pod_id}")
        
        return ok(
            result={
                "path": source_path,
                "files": files,
                "count": len(files)
            },
            msg=f"Successfully listed files in {source_path}"
        )
        
    except Exception as e:
        logger.error(f"Error listing files in pod: {e}")
        return JSONResponse(
            content=f"Failed to list files in pod: {str(e)}", 
            status_code=500
        )

@router.get(
    "/pods/{pod_id}/download_from_pod{url_path:path}",
    tags=["Pods"],
    summary="Download a file from the pod's filesystem",
    operation_id="download_from_pod",
)
async def download_from_pod(
    pod_id: str = Path(..., description="Unique identifier for the pod."),
    url_path: str = Path(..., description="Path to the file inside the pod to download."),
    path: str = None
):
    """
    Download a file from a specific path inside the pod using Kubernetes exec.
    
    Path options (use one, not both):
    - URL path: Relative paths only (e.g., /download_from_pod/myfile.txt -> "myfile.txt")
    - Query parameter: Absolute paths allowed (e.g., ?path=/tmp/myfile.txt -> "/tmp/myfile.txt")
    
    Notes:
    - Pod must have /bin/sh and base64 available (most standard images include these)
    - Distroless or minimal images without a shell or base64 will not work with this endpoint.
    """
    logger.info(f"GET /pods/{pod_id}/download_from_pod - Top of download_from_pod.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    if not pod or getattr(pod, "status_requested", None) != "ON":
        return JSONResponse(content="Can't find suitable running pod for download.", status_code=404)

    # Check that exactly one of url_path or path is provided
    if url_path and path:
        return JSONResponse(
            content=f"Error: Provide source_path either in URL path or as query parameter, not both, url path: {url_path}, query param path: {path}",
            status_code=400
        )
    elif not url_path and not path:
        return JSONResponse(
            content="Error: Path is required. Provide source_path either in URL path or as query parameter.",
            status_code=400
        )

    # Set source_path from whichever was provided
    source_path = url_path.lstrip('/') if url_path else path

    # Extract filename from path for Content-Disposition header
    filename = source_path.split('/')[-1] if '/' in source_path else source_path

    try:
        from kubernetes.stream import stream
        from kubernetes import client as k8s_client
        
        # First check if file exists and get its size
        check_command = ['/bin/sh', '-c', f'test -f {source_path} && stat -c%s {source_path} || echo "FILE_NOT_FOUND"']
        api = k8s_client.CoreV1Api()
        
        check_resp = stream(
            api.connect_get_namespaced_pod_exec,
            pod.k8_name,
            NAMESPACE,
            command=check_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            container=None,
            _preload_content=False
        )
        
        size_output = ""
        while check_resp.is_open():
            check_resp.update(timeout=1)
            if check_resp.peek_stdout():
                size_output += check_resp.read_stdout()
            if check_resp.peek_stderr():
                error = check_resp.read_stderr()
                logger.error(f"Error checking file: {error}")
        check_resp.close()
        
        size_output = size_output.strip()
        if size_output == "FILE_NOT_FOUND" or not size_output.isdigit():
            return JSONResponse(
                content=f"File not found or not accessible: {source_path}", 
                status_code=404
            )
        
        file_size = int(size_output)
        logger.info(f"Downloading file {source_path} of size {file_size} bytes from pod {pod_id}")
        
        # Stream the file content from pod using base64 to preserve binary data
        # K8s exec stream decodes output as UTF-8 which corrupts binary; base64 ensures safe transfer
        exec_command = ['/bin/sh', '-c', f'base64 {source_path}']
        
        async def file_stream_generator():
            """Generator that streams file content from the pod in chunks"""
            import base64
            
            resp = stream(
                api.connect_get_namespaced_pod_exec,
                pod.k8_name,
                NAMESPACE,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                container=None,
                _preload_content=False
            )
            
            try:
                start_time = time.time()
                
                # Collect all base64 data first (k8s stream may split at arbitrary points)
                base64_chunks = []
                while resp.is_open():
                    resp.update(timeout=CHUNK_TIMEOUT)
                    
                    if resp.peek_stdout():
                        chunk = resp.read_stdout()
                        if chunk:
                            # Remove any newlines that base64 command adds
                            base64_chunks.append(chunk.replace('\n', '').replace('\r', ''))
                    
                    if resp.peek_stderr():
                        error = resp.read_stderr()
                        if error:
                            logger.error(f"Error during download: {error}")
                            raise Exception(f"Error reading file from pod: {error}")
                    
                    # Check for timeout
                    if time.time() - start_time > CHUNK_TIMEOUT * 10:  # Overall timeout
                        raise Exception("Download timeout exceeded")
                
                # Decode the complete base64 string
                base64_data = ''.join(base64_chunks)
                decoded_bytes = base64.b64decode(base64_data)
                
                logger.info(f"Successfully downloaded {len(decoded_bytes)} bytes from {source_path}")
                yield decoded_bytes
                
            finally:
                resp.close()
        
        return StreamingResponse(
            file_stream_generator(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Length": str(file_size),
                "X-Pod-Id": pod.pod_id,
                "X-Source-Path": source_path
            }
        )
        
    except Exception as e:
        logger.error(f"Error downloading file from pod: {e}")
        return JSONResponse(
            content=f"Failed to download file from pod: {str(e)}", 
            status_code=500
        )


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
        # Client asked to remove a permission that isn't set — a clean 400, not a server
        # error (bare KeyError surfaced as an opaque 500). NOTE: ResourceError(msg, code) is
        # a BaseTapisError the handler maps to `code`; tapipy's BadRequestError is NOT a
        # BaseTapisError and falls through to 500 — do not use it to signal a 4xx here.
        raise ResourceError(f"No permission found for user '{user}' on pod '{pod_id}'.", 400)

    # Delete permission
    del curr_perms[user]

    # Ensure there's still an admin-capable user before finishing (APPROVEDADMIN is ADMIN+).
    if not any(level in ("ADMIN", "APPROVEDADMIN") for level in curr_perms.values()):
        raise ResourceError("Operation would leave the pod with no ADMIN-capable user. Rolling back.", 400)

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
async def stop_pod(pod_id, force: bool = False):
    """
    Stop a pod.

    Note:
    - Sets status_requested to OFF. Pod will attempt to get to STOPPED status unless start_pod is ran.
    - If this pod is in a stack with restart_policy='ordered', teardown normally waits until pods that
      depend_on it are STOPPED (reverse order). Pass ?force=true to stop it immediately, bypassing that
      ordering — useful when the dependency order doesn't matter for this stop.

    Returns updated pod object.
    """
    logger.info(f"GET /pods/{pod_id}/stop - Top of stop_pod. force={force}")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    if not pod:
        # Guard: admins bypass the auth-layer 404 (check_object_id), so a bad/nonexistent
        # pod_id would otherwise 500 on the .status_requested deref below.
        raise ResourceError(f"Pod with id '{pod_id}' not found in tenant '{g.request_tenant_id}', site '{g.site_id}'.", 404)
    pod.status_requested = OFF
    pod.force_stop = force
    pod.db_update(f"'{g.username}' ran stop_pod, set to OFF{' (force, bypassing stack order)' if force else ''}")

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
    if not pod:
        # Guard: admins bypass the auth-layer 404 (check_object_id), so a bad/nonexistent
        # pod_id would otherwise 500 on the .status deref below.
        raise ResourceError(f"Pod with id '{pod_id}' not found in tenant '{g.request_tenant_id}', site '{g.site_id}'.", 404)

    # Only run start_pod from status=STOPPED
    if not pod.status in [STOPPED]:
        raise RuntimeError(f"Pod must be in 'STOPPED' status to run 'start_pod'. Please run 'stop_pod' or 'restart_pod' instead.")
    else:
        # Validate volume mounts before starting:
        # - Check mounted_by users still have permission on the pod
        # - Check mounted_by users still have READ permission on volumes/snapshots
        if pod.template:
            pod_copy = PodBaseFull(**pod.dict().copy())
            derived_pod = combine_pod_and_template_recursively(
                pod_copy, pod.template, tenant=g.request_tenant_id, site=g.site_id
            )
            derived_volume_mounts = getattr(derived_pod, 'volume_mounts', {}) or {}
        else:
            derived_volume_mounts = pod.volume_mounts or {}
        
        if derived_volume_mounts:
            vm_errors = validate_volume_mounts_on_start(
                volume_mounts=derived_volume_mounts,
                pod_permissions=pod.get_permissions(),
                tenant=g.request_tenant_id,
                site=g.site_id
            )
            if vm_errors:
                return error(
                    result=pod.display(),
                    msg=f"Cannot start pod: {'; '.join(vm_errors)}"
                )

        # Resolve secrets before starting the pod
        # IMPORTANT: If pod uses a template, merge template's secret_map first
        # so template-defined secrets get resolved and sent to spawner
        resolved_secrets = {}
        
        # Derive merged secret_map if pod uses a template (reuse derived_pod if already computed)
        if pod.template:
            merged_secret_map = getattr(derived_pod, 'secret_map', {}) or {}
        else:
            merged_secret_map = pod.secret_map or {}
        
        if merged_secret_map:
            resolved_secrets, secret_errors = resolve_secret_map(
                merged_secret_map,
                site_id=g.site_id,
                tenant_id=g.request_tenant_id,
                actor=g.username,
                pod_id=pod.pod_id,
                pod=pod
            )
            if secret_errors:
                # Required secrets missing - fail the start
                return error(
                    result=pod.display(),
                    msg=f"Failed to start pod: {'; '.join(secret_errors)}"
                )

        pod.status_requested = ON
        pod.status = REQUESTED

        # Send command to start new pod with resolved secrets
        ch = CommandChannel(name=pod.site_id)
        ch.put_cmd(object_id=pod.pod_id,
                   object_type="pod",
                   tenant_id=pod.tenant_id,
                   site_id=pod.site_id,
                   resolved_secrets=resolved_secrets)
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
async def restart_pod(pod_id, grab_latest_template_tag: bool = False):
    """
    Restart a pod.

    Note:
    - Sets status_requested to RESTART. If pod status gets to STOPPED, status_requested will be flipped to ON. Health should then create new pod.
    - If grab_latest_template_tag is True, attempts to grab the latest version of the template tag if the pod has a template.

    Returns updated pod object.
    """
    logger.info(f"GET /pods/{pod_id}/restart - Top of restart_pod.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    if not pod:
        # Guard: admins bypass the auth-layer 404 (check_object_id), so a bad/nonexistent
        # pod_id (e.g. a malformed MCP arg serialized into the path) would otherwise 500
        # on the .status_requested deref below.
        raise ResourceError(f"Pod with id '{pod_id}' not found in tenant '{g.request_tenant_id}', site '{g.site_id}'.", 404)

    if grab_latest_template_tag:
        if pod.template:
            logger.info(f"Attempting to grab the latest version of the template tag for pod {pod_id}.")
            # get rid of timestamp so code can grab latest with current tag.
            pod.template = pod.template.split("@")[0]
        else:
            logger.info(f"Pod {pod_id} does not have a template. No action taken.")

    pod.status_requested = RESTART
    pod.db_update(f"'{g.username}' ran restart_pod, set to RESTART")
                  
    return ok(result=pod.display(), msg="Updated pod's status_requested to RESTART.")


def get_token_tenant_id(token: str) -> str:
    """
    Extract the tenant_id from a Tapis JWT without full validation.
    Returns the tenant_id string or None if extraction fails.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
        return claims.get('tapis/tenant_id')
    except Exception as e:
        logger.debug(f"Could not extract tenant_id from token: {e}")
        return None


def validate_token(request: Request, token: str = None):
    """
    Validate a Tapis JWT from cookies or headers by making a call to the get_userinfo endpoint.
    For cross-tenant tokens, the userinfo call is made to the token's tenant, not the request tenant.
    Returns authorized:bool, username:str, roles:List[str]
    """
    logger.debug(f"Validating token from request: cookies={request.cookies}, headers={request.headers}")
    token = token or request.cookies.get('X-Tapis-Token') or request.headers.get('X-Tapis-Token') or request.headers.get('x-tapis-token') or request.headers.get('X-TAPIS-TOKEN')
    if not token:
        logger.debug("Token not found in cookies or headers.")
        return False, None, None

    # Determine the correct base URL for userinfo.
    # The token may be from a different tenant than the pod (cross-tenant auth),
    # so we must call userinfo on the token's tenant, not the request base_url.
    token_tenant = get_token_tenant_id(token)
    if token_tenant:
        try:
            tenant_base_url = t.tenant_cache.get_tenant_config(tenant_id=token_tenant).base_url
            url = f"{tenant_base_url}/v3/oauth2/userinfo"
        except Exception as e:
            logger.warning(f"Could not resolve base_url for token tenant '{token_tenant}', falling back to request base_url: {e}")
            url = f"{request.base_url}v3/oauth2/userinfo".replace('http://', 'https://')
    else:
        url = f"{request.base_url}v3/oauth2/userinfo".replace('http://', 'https://')

    logger.debug(f"Running get_userinfo with url: {url} (token_tenant: {token_tenant})")
    headers = {'X-Tapis-Token': token}
    try:
        rsp = requests.get(url, headers=headers)
        rsp.raise_for_status()
        username = rsp.json()['result'].get('username')
        email = rsp.json()['result'].get('email')
        name = rsp.json()['result'].get('name')
        roles = rsp.json()['result'].get('roles', [])
        logger.info(f"Token validated successfully. Username: {username}, Email: {email}, Name: {name}")
        return True, username, roles
    except Exception as e:
        logger.error(f"Error with request to userinfo and parsing: {e}")
        return False, None, None


# Template-authoring networking keys. A live pod's networking dict carries runtime-only keys the
# template Networking model forbids (custom_domain, custom_domain_verified, cert_ready, ...), so a
# verbatim copy fails tag validation. sanitize_member_networking (pure, in stack_template_utils)
# keeps only these template-valid keys when snapshotting — mirrors the stack save path.
_TEMPLATE_NET_FIELDS = set(
    getattr(TemplateTagNetworking, "model_fields", None) or TemplateTagNetworking.__fields__
)


@router.post(
    "/pods/{pod_id_net}/save_pod_as_template_tag",
    tags=["Pods"],
    summary="save_pod_as_template_tag",
    operation_id="save_pod_as_template_tag",
    response_model=TemplateTagResponse)
async def save_pod_as_template_tag(pod_id_net, new_template_tag_from_pod: NewTemplateTagFromPod):
    """
    Endpoint takes pod_id and derives a pod_definition to create a template tag from it.
    Allows users to save the configuration of a particular pod as a template tag.

    POST data contains location to save the tag and tag creation data

    Return the template tag object.
    """
    logger.info(f"POST /pods/{pod_id_net}/save_pod_as_template_tag - Top of save_pod_as_template_tag.")
    
    pod = Pod.db_get_with_pk(pod_id_net, tenant=g.request_tenant_id, site=g.site_id)
    if not pod:
        # Guard: without this, the .get_pod_definition_for_template_tag() call below raises an
        # opaque 500 ('NoneType' object has no attribute ...). Return a clear 400 instead.
        raise ResourceError(f"Pod with id '{pod_id_net}' not found in tenant '{g.request_tenant_id}', site '{g.site_id}'.", 404)

    # Auth already checks permissions for pod_id. We must also check permissions for template.
    template = Template.db_get_with_pk(new_template_tag_from_pod.template_id, tenant="siteadmintable", site=g.site_id)
    if not template:
        raise PermissionsException(f"Template with id '{new_template_tag_from_pod.template_id}' not found. Please ensure template exists.")

    # Check permissions for user to create a template tag under the template
    has_pem = check_permissions(user=g.username, object=template, object_type="template", level=USER , roles=g.roles)
    if not has_pem:
        logger.info("NOT allowing request.")
        raise PermissionsException(f"Not authorized -- you do not have permission to create a template tag under the template_id: {new_template_tag_from_pod.template_id}")
    current_pod_def, modified_fields = pod.get_pod_definition_for_template_tag()
    logger.debug(f"Current pod definition: {current_pod_def}, modified_fields: {modified_fields}")

    # Create a dict of only modified fields
    modified_pod_def = {field: current_pod_def[field] for field in modified_fields if field not in ["pod_id"]}

    # Hygiene so a live pod validates as a *template* pod_definition (mirrors the stack snapshot
    # path in api_stacks.save_stack_as_template). Two live-pod artifacts the template models reject:
    #   1) networking carries runtime-only keys (custom_domain, cert_ready, cert_state, ...) that the
    #      template Networking model forbids -> strip to the template-valid field set.
    #   2) secret_map holds concrete secret references (${secret:user:name}) which template validation
    #      rejects and which would leak a real secret -> replace each value with a ${:?...} blank the
    #      deployer fills. Keys are preserved so ${pods:secrets:KEY} env refs still resolve.
    if "networking" in modified_pod_def:
        modified_pod_def["networking"] = sanitize_member_networking(
            modified_pod_def["networking"], _TEMPLATE_NET_FIELDS)
    if "secret_map" in modified_pod_def:
        modified_pod_def["secret_map"] = {
            k: placeholderize_secret_value(v)
            for k, v in (modified_pod_def["secret_map"] or {}).items()}
    logger.debug(f"Modified pod definition: {modified_pod_def}")

    template_tag = TemplateTag(**new_template_tag_from_pod.dict(), pod_definition=modified_pod_def)

    # Create template database entry
    template_tag.db_create(tenant="siteadmintable", site=g.site_id)
    logger.debug(f"New template_tag saved in db. template_id: {template_tag.template_id}; tenant: {g.request_tenant_id}.")

    return ok(result=template_tag.display(), msg="Template tag added successfully.")


def get_pod_networking_objects(net_info: dict, tenant_id: str, site_id: str, username: str = "nouser"):
    """
    Get the tapis auth response headers.

    If pod.networking.<network_key>.tapis_auth_response_headers is:
    {
        "X-Tapis-Username": <<tapisusername>>@tapis.io",
        "FROM": "pods auth endpoint from <<tenant>>.<<site>>",
        "OAUTH2_USERNAME_KEY": "username"
    }
    
    Then we set headers from auth calls to pods to pod container as set by user.
    Users can specify <<tapisusername>>, <<tapistenantid>>, or <<tapissiteid>> for replacement. 

    Final headers to pass to pod container:
    headers = {
        "X-Tapis-Username": myuser@tapis.io,
        "FROM": "pods auth endpoint from tacc.tacc",
        "OAUTH2_USERNAME_KEY": "username"
    }
    """
    tapis_auth_response_headers = net_info.get("tapis_auth_response_headers", {})
    final_headers = {}
    if tapis_auth_response_headers:
        for header, value in tapis_auth_response_headers.items():
            if "<<tapisusername>>" in value:
                value = value.replace("<<tapisusername>>", username)
            if "<<tapistenantid>>" in value:
                value = value.replace("<<tapistenantid>>", tenant_id)
            if "<<tapissiteid>>" in value:
                value = value.replace("<<tapissiteid>>", site_id)
            # We should rarely ever send token, leaving commented for now.
            # Only some admins should be able to. No use case yet. 
            #if "<<token>>" in value:
            #    value = value.replace("<<token>>", "token")
            final_headers[header] = value
    return final_headers


@router.get(
    "/pods/{pod_id_net}/auth",
    tags=["Pods"],
    summary="pod_auth",
    operation_id="pod_auth",
    response_model=PodResponse)
async def pod_auth(pod_id_net, request: Request):
    """
    Auth endpoint for each pod. When a networking object defines tapis_auth=True, this endpoint manages auth.

    Traefik has a forwardAuth middleware for http routes. This redirects users, auth happens, if traefik gets 200 then traefik allows user to endpoint.
    Auth flow for a user getting to "fastapi hello world" pod at https://fastapi.pods.tacc.tapis.io.
      1) User navigates to https://fastapi.pods.tacc.tapis.io
      2) Traefik redirects user to https://tacc.tapis.io/v3/pods/fastapi/auth
      3) Check if logged in via cookies, if logged in, respond 200 + set user defined headers. Otherwise...
      4) Pods service creates client in correct tenant for user or updates client if it already exists. (we expect only one client in use at a time)
      5) With client the /auth endpoint redirects users to https://tacc.tapis.io/v3/oauth2/authorize?client_id={client_id}&redirect_uri={callback_url}&response_type=code
      6) User logs in via browser, authorizes client, redirects to callback_url at https://tacc.tapis.io/v3/pods/fastapi/auth/callback?code=CodeHere
      7) Callback url exchanges code for token, gets username from token, sets X-Tapis-Token cookies, sets response headers according to tapis_auth_response_headers
      8) User gets redirected back to https://fastapi.pods.tacc.tapis.io/{tapis_auth_return_path}, Traefik starts forwardAuth, user at this point should be authenticated
      9) Auth endpoint responds with 200, sets headers specified by networking stanza, and users gets to fastapi hello world response.

    users can specify:
     - tapis_auth=True/False - Turns on auth
     - tapis_auth_response_headers - dict[str] - headers to set on response and their values
     - tapis_auth_allowed_users - list[str] - list of tapis users or permission-based groups allowed to access pod.
       Supports literal usernames, "*" (all authenticated users), and special group strings:
         AUTHORIZED_READS  -> users with READ or higher permission on the pod
         AUTHORIZED_USERS  -> users with USER or higher permission on the pod (default)
         AUTHORIZED_ADMINS -> users with ADMIN or higher (including APPROVEDADMIN) permission on the pod
       Groups resolve against the pod's permissions list at auth time.
       Can mix groups with literal usernames, e.g. ["AUTHORIZED_ADMINS", "guest_user"].
     - tapis_auth_return_path - str - uri to return to after auth, default is "passthrough", which we save in cookies(?) and return to. x-forwarded-host?
    
     - response headers need to be slightly modifiable to allow for different application requirements
     - for example we have to pass username, but many apps require @email.bit, so user must be able to append to user.
     - tapis_auth_response_headers: {"X-Tapis-Username": "<<tapisusername>>@tapis.io", "FROM": "pods auth endpoint from <<tenant>>.<<site>>", "OAUTH2_USERNAME_KEY": "username"}
    """
    logger.debug(f"GET /pods/{pod_id_net}/auth - pod-auth, headers: {request.headers}, request.cookies: {request.cookies}, request_tenant_id: {g.request_tenant_id}, site_id: {g.site_id}")
    # In cases where networking key is not 'default', the pod_id_net is f"{pod_id}-{network_key}"
    parts = pod_id_net.split('-', 1)
    pod_id = parts[0]
    network_key = parts[1] if len(parts) > 1 else 'default'
    logger.debug(f"In pod_auth, pod_id: {pod_id}, network_key: {network_key}")

    ## Headers contains x-forwarded stuff we can use to deduct correct tenant (x-forwarded-host)
    ## example data for future reference
    # 'x-forwarded-for': '10.233.72.192'             # doesn't seem like real ip, we're getting proxy server forward info
    # 'x-forwarded-host': 'tacc.develop.tapis.io'
    # 'x-forwarded-port': '80', 'x-forwarded-prefix': '/v3'
    # 'x-forwarded-proto': 'http'
    # 'x-forwarded-server': 'pods-traefik-65c7ccb5fd-ffk4g'
    # 'x-real-ip': '10.233.72.193'    
    
    # if not authenticated, start the OAuth flow
    pod_init = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Derive the final pod object by combining the pod and templates
    if pod_init.template:
        pod = combine_pod_and_template_recursively(pod_init, pod_init.template, tenant=g.request_tenant_id, site=g.site_id)
    else:
        pod = pod_init

    # Get networking info for pod
    net_info = pod.networking.get(network_key, None)
    if not net_info:
        raise Exception(f"Pod {pod_id} has a misconfigured networking value for network_key: {network_key}")

    # check if dict
    # net_info
    if type(net_info) is not dict:
        try:
            net_info = net_info.dict()
        except Exception as e:
            raise Exception(f"Error converting net_info to dict: {e}")

    # Cross-tenant validation: if this is a cross-tenant request (flagged by auth.py),
    # check that the token's tenant is in the pod's permissions list (tenant.<tenant_id> entries).
    pod_permissions = pod_init.get_permissions()
    tapis_auth_allowed_tenants = get_allowed_tenants_from_permissions(pod_permissions)
    cross_tenant = getattr(g, 'cross_tenant_request', False)
    if cross_tenant:
        token_tenant_id = getattr(g, 'token_tenant_id', None)
        if not check_tapis_auth_tenant_allowed(token_tenant_id, g.request_tenant_id, tapis_auth_allowed_tenants):
            logger.info(f"Cross-tenant request rejected. token_tenant: {token_tenant_id}, pod_tenant: {g.request_tenant_id}, allowed: {tapis_auth_allowed_tenants}")
            return JSONResponse(
                content=f"Cross-tenant auth not allowed. Token tenant '{token_tenant_id}' is not in this pod's permissions (set via tenant.<tenant_id> permission entries).",
                status_code=403
            )
        logger.info(f"Cross-tenant request allowed. token_tenant: {token_tenant_id}, pod_tenant: {g.request_tenant_id}")

    ## We now want to check if session/headers have a valid Tapis token for the current site/tenant. If so, we can return 200.
    ## Session and headers can both be manually modified, this is where we must validate the token is valid via a call to get_userinfo.
    try:
        authorized, username, roles = validate_token(request)
        # check if user is allowed to access pod
        tapis_auth_allowed_users = net_info.get("tapis_auth_allowed_users", [])
        if authorized:
            logger.debug(f"User authenticated: {username}")

            # Additional cross-tenant check on the actual token (not just header-level from auth.py).
            # This catches cases where the token in cookies is from a different tenant than the pod.
            # check_tapis_auth_tenant_allowed allows the pod's own tenant automatically.
            token_str = request.cookies.get('X-Tapis-Token') or request.headers.get('X-Tapis-Token') or request.headers.get('x-tapis-token')
            if token_str:
                token_tenant = get_token_tenant_id(token_str)
                if token_tenant and not check_tapis_auth_tenant_allowed(token_tenant, g.request_tenant_id, tapis_auth_allowed_tenants):
                    # Explicit 403 (a raise here would be swallowed by the except below and
                    # the user would fall into the OAuth redirect path with no explanation).
                    logger.info(f"Cross-tenant token rejected for {pod_id_net}. token_tenant: {token_tenant}, pod_tenant: {g.request_tenant_id}, allowed: {tapis_auth_allowed_tenants}")
                    return JSONResponse(
                        content=f"Pods: token tenant '{token_tenant}' is not allowed for this pod. Pod tenant: '{g.request_tenant_id}'; extra tenants allowed via permissions: {tapis_auth_allowed_tenants}.",
                        status_code=403)

            tapis_auth_headers = get_pod_networking_objects(
                net_info=net_info,
                username=username,
                tenant_id=g.request_tenant_id,
                site_id=g.site_id
            )
            if tapis_auth_allowed_users:
                if not check_tapis_auth_allowed(username, tapis_auth_allowed_users, pod_permissions):
                    # Explicit 403 for allowlist rejection — previously a raise that the
                    # except below logged and swallowed, so denied users were re-sent
                    # through the OAuth flow / told "not authenticated" instead of
                    # "authenticated but not allowed".
                    logger.info(f"User '{username}' rejected by tapis_auth_allowed_users for {pod_id_net}.")
                    return JSONResponse(
                        content=f"Pods: user '{username}' is authenticated but not in this pod's tapis_auth_allowed_users.",
                        status_code=403)
            return JSONResponse(content=ok("Already authenticated"), status_code=200, headers=tapis_auth_headers)
    except Exception as e:
        logger.debug(f"Authentication failed: {getattr(e, 'detail', None) or e}")

    ## if request headers has X-Tapis-Token, we assume they're not browser based and want to use the token
    ## if it doesn't validate they need a warning message rather than getting an error due to redirect
    logger.debug(f"request_info dump: {request.headers}, {request.cookies}, {request.query_params}")
    if request.headers.get('X-Tapis-Token') or request.headers.get('x-tapis-token2'):
        logger.debug(f"X-Tapis-Token found in headers, but not authenticated. Returning 403.")
        return JSONResponse(content="Pods Service tapis_auth - not authenticated", status_code=403)
    

    # Get info for clients
    # The goal is: https://tacc.develop.tapis.io/v3/pods/{{pod_id}}/auth
    pod_id, tapis_domain = net_info['url'].split('.pods.') ## Should return `mypod` & `tacc.tapis.io` with proper tenant and schmu
    tapis_tenant = tapis_domain.split('.')[0]
    if not net_info.get("tapis_auth", False):
        return JSONResponse(content = f"This pod does not have tapis_auth configured in networking for this pod_id_net: {pod_id_net}. net_info: {net_info} Leave or remedy. Initial Auth", status_code = 403)
    
    
    auth_url =  f"https://{tapis_domain}/v3/pods/{pod_id_net}/auth"
    auth_callback_url =  f"https://{tapis_domain}/v3/pods/{pod_id_net}/auth/callback" # should match client callback_url

    client_id = f"PODS-SERVICE-{pod.k8_name}-{network_key}"
    #client_key = "4STQ^t&RGa$sah!SZ9zCP9UScGoEkS^GYLZDjjtjPBipp4kVLyrr@X"
    client_display_name = f"Tapis Pods Service Pod: {pod_id}"
    client_description = f"Tapis Pods Service Pod: {pod_id}"

    
    logger.debug(f"GET /pods/{pod_id_net}/auth - pod-auth, headers: {request.headers}, request.cookies: {request.cookies}, tenant_id: {g.request_tenant_id}, derived_tenant_id: {tapis_tenant}, site_id: {g.site_id}")
    
    td = None
    # Create tapis client or update tapis client if needed
    try:
        logger.debug(f"Creating client_id: {client_id}, tenant: {tapis_tenant}")
        res, td = t.authenticator.create_client(
            client_id = client_id,
            #client_key = client_key,
            callback_url = auth_callback_url,
            display_name = client_display_name,
            description = client_description,
            _x_tapis_tenant = tapis_tenant,
            _x_tapis_user = "_tapis_pods",
            _tapis_debug = True
        )
    except BadRequestError as e: # Exceptions in 3 shouldn't have e.message (only e.args), but this one does.
        logger.debug(f"Got error creating client: {e.message}")
        if "This change would violate uniqueness constraints" in e.message:
            logger.debug(f"Client already exists, updating client_id: {client_id}, tenant: {tapis_tenant}")
            try:
                res, td = t.authenticator.update_client(
                    client_id = client_id,
                    callback_url = auth_callback_url,
                    display_name = client_display_name,
                    description = client_description,
                    _x_tapis_tenant = tapis_tenant,
                    _x_tapis_user = "_tapis_pods",
                    _tapis_debug = True
                )
                # Assuming you want to return a success response after updating
                success_msg = f"Client {client_id} updated successfully."
                logger.info(success_msg)
            except Exception as e:
                msg = (f"Error updating client_id: {client_id}. e: {e.args}, e: {e}, dir(e): {dir(e)}")
                logger.warning(msg)
                return JSONResponse(content = msg, status_code = 500)
        else:
            msg = (f"Error creating client_id: {client_id}. e.message: {e.message}, e.request: {e.request}, e.response: {e.response}, tapis_debug = {td}")
            logger.warning(msg)

#       return JSONResponse(content = msg, status_code = 500)
    oauth2_url = f"https://{tapis_domain}/v3/oauth2/authorize?client_id={client_id}&redirect_uri={auth_callback_url}&response_type=code"
    logger.debug(f"oauth2 url is: {oauth2_url}")
    return RedirectResponse(url=oauth2_url, status_code=302)
    # result = {'path': auth_callback_url, 'code': 302}
    return JSONResponse(content = str(result))


@router.get(
    "/pods/{pod_id_net}/auth/callback",
    tags=["Pods"],
    summary="pod_auth_callback",
    operation_id="pod_auth_callback",
    response_model=PodResponse)
def callback(pod_id_net, request: Request):
    logger.info(f"GET /pods/{pod_id_net}/auth/callback - pod_auth_callback, headers: {request.headers}, request.cookies: {request.cookies}")
    parts = pod_id_net.split('-', 1)
    pod_id = parts[0]
    network_key = parts[1] if len(parts) > 1 else 'default'
    pod_init = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    
    if pod_init.template:
        # Derive the final pod object by combining the pod and templates
        pod = combine_pod_and_template_recursively(pod_init, pod_init.template, tenant=g.request_tenant_id, site=g.site_id)
    else:
        pod = pod_init

    net_info = pod.networking.get(network_key, None)
    if not net_info:
        raise Exception(f"Pod {pod_id} does not have networking key that matches pod_id_net: {pod_id_net}")

    if type(net_info) is not dict:
        try:
            net_info = net_info.dict()
        except Exception as e:
            raise Exception(f"Error converting net_info to dict: {e}")

    # Cross-tenant validation for callback
    pod_permissions = pod_init.get_permissions()
    tapis_auth_allowed_tenants = get_allowed_tenants_from_permissions(pod_permissions)
    cross_tenant = getattr(g, 'cross_tenant_request', False)
    if cross_tenant:
        token_tenant_id = getattr(g, 'token_tenant_id', None)
        if not check_tapis_auth_tenant_allowed(token_tenant_id, g.request_tenant_id, tapis_auth_allowed_tenants):
            logger.info(f"Cross-tenant callback rejected. token_tenant: {token_tenant_id}, pod_tenant: {g.request_tenant_id}, allowed: {tapis_auth_allowed_tenants}")
            return JSONResponse(
                content=f"Cross-tenant auth not allowed. Token tenant '{token_tenant_id}' is not in this pod's permissions (set via tenant.<tenant_id> permission entries).",
                status_code=403
            )
        logger.info(f"Cross-tenant callback allowed. token_tenant: {token_tenant_id}, pod_tenant: {g.request_tenant_id}")

    pod_id, tapis_domain = net_info['url'].split('.pods.') ## Should return `mypod` & `tacc.tapis.io` with proper tenant and schmu
    tapis_tenant = tapis_domain.split('.')[0]
    if not net_info.get("tapis_auth", False):
        return JSONResponse(content = f"This pod does not have tapis_auth configured in networking for this pod_id_net: {pod_id_net}. Leave or remedy. Callback", status_code = 403)

    client_id = f"PODS-SERVICE-{pod.k8_name}-{network_key}"

    try:
        res, td = t.authenticator.get_client(
            client_id = client_id,
            _x_tapis_tenant = tapis_tenant,
            _x_tapis_user = "_tapis_pods",
            _tapis_debug = True)
    except Exception as e:
        return JSONResponse(content=f"Error retrieving client: {e}", status_code=500)

    # return JSONResponse(content = f"Callback for pod_id_net: {pod_id_net}, tapis_domain: {tapis_domain}", status_code = 200)
    code = request.query_params.get('code')
    if not code:
        raise Exception(f"Error: No code in request; debug: {request.query_params}")
    logger.debug(f"GET /pods/{pod_id_net}/auth/callback - pod_auth_callback1, tapis_domain: {tapis_domain}, code: {code}")
    url = f"https://{tapis_domain}/v3/oauth2/tokens"
    data = {
        "code": code,
        "redirect_uri": f"https://{tapis_domain}/v3/pods/{pod_id_net}/auth/callback",
        "grant_type": "authorization_code",
    }

    try:
        #logger.debug(dir(res))
        response = requests.post(url, data=data, auth=(client_id, res.client_key))
        response.raise_for_status()
        logger.debug(f"GET /pods/{pod_id_net}/auth/callback callback request response: {response.text}")
        json_resp = response.json()
        #json_resp = json.loads(response.text)
        token = json_resp['result']['access_token']['access_token']
    except Exception as e:
        raise Exception(f"Error generating Tapis token; debug: {e}")

    try:
        logger.debug(f"GET /pods/{pod_id_net}/auth/callback - pod_auth_callback2, token: {token}")

        authorized, username, roles = validate_token(request, token=token)
        
        logger.debug(f"GET /pods/{pod_id_net}/auth/callback - pod_auth_callback3, username: {username}, tapis_domain: {tapis_domain}")

        # Cross-tenant check on the newly obtained token.
        # check_tapis_auth_tenant_allowed allows the pod's own tenant automatically.
        token_tenant = get_token_tenant_id(token)
        if token_tenant and not check_tapis_auth_tenant_allowed(token_tenant, g.request_tenant_id, tapis_auth_allowed_tenants):
            raise Exception(f"Token tenant '{token_tenant}' not in allowed tenants for pod_id: {pod_id_net}. Pod tenant: '{g.request_tenant_id}'. Allowed extra tenants (via permissions): {tapis_auth_allowed_tenants}.")
        if token_tenant and token_tenant != g.request_tenant_id:
            logger.info(f"Cross-tenant token accepted in callback. token_tenant: {token_tenant}, pod_tenant: {g.request_tenant_id}")

        # tapis_auth_headers = get_pod_networking_objects(
        #     net_info=net_info,
        #     username=username,
        #     tenant_id=g.request_tenant_id,
        #     site_id=g.site_id
        # )
        tapis_auth_allowed_users = net_info.get("tapis_auth_allowed_users", [])
        if tapis_auth_allowed_users:
            if not check_tapis_auth_allowed(username, tapis_auth_allowed_users, pod_permissions):
                # Explicit 403 — a raise here lands in the outer except and resurfaces
                # as a misleading "Error setting cookies" exception.
                logger.info(f"User '{username}' rejected by tapis_auth_allowed_users for {pod_id_net} (callback flow).")
                return JSONResponse(
                    content=f"Pods: user '{username}' is authenticated but not in this pod's tapis_auth_allowed_users.",
                    status_code=403)

        response = RedirectResponse(url=f"https://{net_info['url']}{net_info['tapis_auth_return_path']}", status_code=302)

        # Setting cookies
        domain = conf.get('COOKIE_DOMAIN', f"{tapis_domain}")
        logger.debug(f"About to set cookies. domain: {domain}, net_info['url']: {net_info['url']}")

        response.set_cookie("X-Tapis-Token", token, domain=net_info["url"], secure=True)
#        response.set_cookie("X-Tapis-Username", username, domain=net_info["url"], secure=True)    

        response.set_cookie("X-Tapis-Token", token, domain=domain, secure=True)
#        response.set_cookie("X-Tapis-Username", username, domain=domain, secure=True)    

        logger.debug(f"GET /pods/{pod_id_net}/auth/callback - pod_auth_callback last bit, response: {response}, net_info: {net_info['url']}")

        return response
    except Exception as e:
        raise Exception(f"Error setting cookies; debug: {e}")

    # response = make_response(redirect(os.environ['FRONT_URL'], code=302))

    # domain = conf.get('COOKIE_DOMAIN', f".pods.{tapis_domain}")
    # response.set_cookie("token", token, domain=domain, secure=True)
    # response.set_cookie("username", username, domain=domain, secure=True)    
    
    # return response

    #return JSONResponse(content = f"Callback for pod_id_net: {pod_id_net}, tapis_domain: {tapis_domain}, username: {username}, token: {token}", status_code = 200)
    #return response


# ─────────────────────────────────────────────────────────────────────────────
# Access gate — shared password/token ingress auth (complements tapis_auth)
#
# When a networking entry sets access_gate=true, Traefik forwardAuth calls /gate.
# Visitors are NOT Tapis users; they present a shared secret (typed password or a
# ?access= link) which is validated against the pod's access tokens. On success a
# cookie carrying the secret is set (Domain=<tenant base>, so it reaches the pod
# host), and later gate checks re-validate it live.
# ─────────────────────────────────────────────────────────────────────────────

def _gate_cookie_name(pod_id: str) -> str:
    return f"tapis_pod_gate_{pod_id}"


def _gate_split_pod_id(pod_id_net: str):
    parts = pod_id_net.split('-', 1)
    return parts[0], (parts[1] if len(parts) > 1 else 'default')


def _gate_load_net_info(pod_id: str, network_key: str):
    """Return (pod, net_info dict) with template merged, or raise."""
    pod_init = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    if pod_init.template:
        pod = combine_pod_and_template_recursively(pod_init, pod_init.template, tenant=g.request_tenant_id, site=g.site_id)
    else:
        pod = pod_init
    net_info = pod.networking.get(network_key, None)
    if net_info is not None and type(net_info) is not dict:
        net_info = net_info.dict()
    return pod, net_info


def _safe_header_val(v: str, maxlen: int = 80) -> str:
    """Sanitize a value for use as an HTTP header (latin-1, no CR/LF, bounded)."""
    v = (v or "").replace("\r", " ").replace("\n", " ").strip()
    v = v.encode("latin-1", "ignore").decode("latin-1")
    return v[:maxlen]


def _client_ip(request: Request) -> str:
    """Rate-limit key: the socket peer, NOT client-controlled X-Forwarded-For.

    XFF's leftmost hop is attacker-supplied, so keying the brute-force window on it let a
    caller rotate the header to reset the per-(pod,ip) throttle. The socket peer can't be
    spoofed; behind the shared ingress proxy this makes the redeem throttle effectively
    per-pod, which is the correct bound for a shared-password gate.
    """
    return (request.client.host if request.client else "") or "unknown"


# ── redeem rate limiting (brute-force protection) ────────────────────────────
# In-memory sliding window of FAILED redeem attempts per (pod_id, ip). pods-api runs a
# single replica, so this process-local map is effectively global; if that ever changes,
# move this to a shared store (redis). Only failures count — a correct code is never
# throttled. After _GATE_RL_MAX failures inside _GATE_RL_WINDOW seconds, further attempts
# are refused until the oldest failure ages out of the window.
_GATE_ATTEMPTS: dict = {}
_GATE_RL_WINDOW = 300   # seconds
_GATE_RL_MAX = 10       # failed attempts allowed per window


def _gate_rl_locked_seconds(pod_id: str, ip: str) -> int:
    """Return seconds until the caller may try again (0 = not locked). Prunes stale entries."""
    # pod_id is only unique within a tenant/site — key the bucket accordingly
    key = (g.request_tenant_id, g.site_id, pod_id, ip)
    now = time.time()
    fails = [t for t in _GATE_ATTEMPTS.get(key, []) if now - t < _GATE_RL_WINDOW]
    if fails:
        _GATE_ATTEMPTS[key] = fails
    else:
        _GATE_ATTEMPTS.pop(key, None)
    if len(fails) >= _GATE_RL_MAX:
        return max(1, int(_GATE_RL_WINDOW - (now - fails[0])))
    return 0


def _gate_rl_record_fail(pod_id: str, ip: str) -> None:
    _GATE_ATTEMPTS.setdefault((g.request_tenant_id, g.site_id, pod_id, ip), []).append(time.time())


def _gate_rl_clear(pod_id: str, ip: str) -> None:
    # MUST use the same 4-part key as _gate_rl_locked_seconds/_gate_rl_record_fail.
    # Keyed on (pod_id, ip) alone this silently matched nothing, so a visitor who
    # mistyped and then succeeded stayed counted and could still be locked out for
    # the rest of the window.
    _GATE_ATTEMPTS.pop((g.request_tenant_id, g.site_id, pod_id, ip), None)


# Redeem-failure messages: (message, owner_fault). owner_fault=True means "nothing the
# visitor did" (the code itself is dead) → shown in a calm amber note; False means "check
# your code" → shown as a red retry error.
_GATE_REDEEM_MESSAGES = {
    "empty":     ("Enter the access code above to continue.", False),
    "not_found": ("We don’t recognize that code. Check it for typos and try again.", False),
    "revoked":   ("This code has been turned off by the pod owner. Ask them for a new one — nothing’s wrong on your end.", True),
    "expired":   ("This code has expired. Ask the pod owner for a fresh one — nothing’s wrong on your end.", True),
    "exhausted": ("This code has reached its sign-in limit. Ask the pod owner for a new one — nothing’s wrong on your end.", True),
}


def _gate_login_html(pod_id: str, tapis_domain: str, network_key: str, return_path: str,
                     error: str = "", owner_fault: bool = False) -> str:
    """Minimal, self-contained login page shown when a gated pod has no valid session.
    `owner_fault` renders the message as a calm note (the code is dead — not the visitor's
    fault) rather than a red retry error."""
    action = f"https://{tapis_domain}/v3/pods/{pod_id}-{network_key}/gate/redeem" if network_key != "default" \
        else f"https://{tapis_domain}/v3/pods/{pod_id}/gate/redeem"
    if error:
        cls = "note" if owner_fault else "err"
        icon = "ⓘ " if owner_fault else ""
        err_html = f'<p class="{cls}">{icon}{_html.escape(error)}</p>'
    else:
        err_html = ''
    # game-loading-screen style quip
    quip = "Tip: a one-click access link signs you in without this screen — ask the owner to share one."
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(pod_id)} — access</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         background:#0f1117; color:#e6e6e6; padding:1.5rem; }}
  .card {{ width:100%; max-width:360px; background:#171a23; border:1px solid #262a36;
          border-radius:12px; padding:1.6rem 1.5rem; box-shadow:0 8px 30px rgba(0,0,0,.35); }}
  h1 {{ font-size:1.05rem; margin:0 0 .2rem; font-weight:650; }}
  .sub {{ font-size:.8rem; color:#9aa0ad; margin:0 0 1.1rem; }}
  label {{ display:block; font-size:.72rem; letter-spacing:.03em; text-transform:uppercase;
          color:#9aa0ad; margin:0 0 .35rem; }}
  input[type=password] {{ width:100%; padding:.65rem .7rem; border-radius:8px; border:1px solid #2d323f;
          background:#0f1117; color:#e6e6e6; font-size:.95rem; }}
  input[type=password]:focus {{ outline:none; border-color:#8b7ec7; box-shadow:0 0 0 3px rgba(139,126,199,.25); }}
  button {{ margin-top:1rem; width:100%; padding:.65rem; border:none; border-radius:8px; cursor:pointer;
          background:#8b7ec7; color:#fff; font-weight:650; font-size:.9rem; }}
  button:hover {{ filter:brightness(1.07); }}
  .err {{ color:#ff8080; font-size:.8rem; margin:.6rem 0 0; }}
  .note {{ color:#e0b872; font-size:.8rem; margin:.6rem 0 0; line-height:1.4;
          background:rgba(224,184,114,.08); border:1px solid rgba(224,184,114,.25);
          border-radius:8px; padding:.55rem .65rem; }}
  .quip {{ font-size:.72rem; color:#6b7280; margin:1.1rem 0 0; line-height:1.4; }}
</style></head>
<body>
  <form class="card" method="POST" action="{action}">
    <h1>🔒 {_html.escape(pod_id)}</h1>
    <p class="sub">This site is private. Enter the access code to continue.</p>
    <label for="secret">Access code</label>
    <input id="secret" name="secret" type="password" autofocus autocomplete="off" required>
    <input type="hidden" name="return_path" value="{_html.escape(return_path)}">
    {err_html}
    <button type="submit">Enter</button>
    <p class="quip">{quip}</p>
  </form>
</body></html>"""


@router.get(
    "/pods/{pod_id_net}/gate",
    tags=["Pods"],
    summary="pod_access_gate",
    operation_id="pod_access_gate",
    include_in_schema=False)
async def pod_access_gate(pod_id_net, request: Request):
    """Traefik forwardAuth target for access_gate. 200 = allow (valid session cookie);
    otherwise redirect a ?access= link to redeem, or show the login form (401)."""
    pod_id, network_key = _gate_split_pod_id(pod_id_net)
    logger.info(f"GET /pods/{pod_id_net}/gate - access-gate check.")

    pod, net_info = _gate_load_net_info(pod_id, network_key)
    if not net_info:
        return JSONResponse(content=f"Pod {pod_id} misconfigured networking for '{network_key}'.", status_code=500)
    if not net_info.get("access_gate", False):
        # Gate is off — or was just toggled off and Traefik hasn't dropped the forwardAuth
        # middleware yet. FAIL OPEN (allow) so visitors aren't blocked during that reconcile
        # window; blocking here would show every visitor "does not have access_gate enabled".
        return JSONResponse(content=ok("Access gate disabled"), status_code=200)

    _pod_id_from_url, tapis_domain = net_info['url'].split('.pods.')
    return_path = net_info.get("access_gate_return_path", "/") or "/"

    # 1) valid session cookie → allow, and stamp which code the visitor came in on so the
    #    traffic pipeline can attribute their (otherwise anonymous) requests. The gate
    #    middleware forwards X-Tapis-Gate-Code to the pod; Traefik logs it → traffic_utils
    #    surfaces it as username "gate:<label>".
    gate_tok = (
        PodAccessToken.check_gate(pod_id, cookie_val, tenant=g.request_tenant_id, site=g.site_id)
        if (cookie_val := request.cookies.get(_gate_cookie_name(pod_id)))
        else None
    )
    if gate_tok:
        code_label = _safe_header_val(gate_tok.label or gate_tok.id[:8])
        return JSONResponse(content=ok("Access granted"), status_code=200,
                            headers={"X-Tapis-Gate-Code": code_label})

    # 2) redemption link ?access=CODE on the original request → hand off to redeem (which sets the cookie)
    forwarded_uri = request.headers.get('x-forwarded-uri', '') or ''
    access_code = None
    if 'access=' in forwarded_uri:
        try:
            q = parse_qs(urlparse(forwarded_uri).query)
            access_code = (q.get('access') or [None])[0]
        except Exception:
            access_code = None
    if access_code:
        redeem_base = f"https://{tapis_domain}/v3/pods/{pod_id_net}/gate/redeem"
        return RedirectResponse(url=f"{redeem_base}?access={quote(access_code)}", status_code=302)

    # 3) no session → show login form
    return HTMLResponse(content=_gate_login_html(pod_id, tapis_domain, network_key, return_path), status_code=401)


async def _gate_do_redeem(pod_id_net: str, request: Request, secret: str, return_path_override: str = ""):
    """Shared redeem logic for GET (?access=) and POST (form). Sets the session cookie."""
    pod_id, network_key = _gate_split_pod_id(pod_id_net)
    pod, net_info = _gate_load_net_info(pod_id, network_key)
    if not net_info:
        return JSONResponse(content=f"Pod {pod_id_net} misconfigured networking.", status_code=500)
    if not net_info.get("access_gate", False):
        # Gate is off (e.g. someone clicks an old redemption link after it was disabled) —
        # no code needed; just send them to the site instead of erroring.
        return RedirectResponse(url=f"https://{net_info['url']}/", status_code=302)

    _pod_id_from_url, tapis_domain = net_info['url'].split('.pods.')
    return_path = return_path_override or net_info.get("access_gate_return_path", "/") or "/"
    if not return_path.startswith("/"):
        return_path = "/"

    # Brute-force guard: refuse further attempts once this IP has failed too many times.
    ip = _client_ip(request)
    locked = _gate_rl_locked_seconds(pod_id, ip)
    if locked:
        wait = f"{locked // 60}m {locked % 60}s" if locked >= 60 else f"{locked}s"
        html = _gate_login_html(
            pod_id, tapis_domain, network_key, return_path,
            error=f"Too many attempts from your network. Please wait about {wait} and try again.",
            owner_fault=True)
        return HTMLResponse(content=html, status_code=429)

    tok, reason, cookie_value = PodAccessToken.redeem(pod_id, secret, tenant=g.request_tenant_id, site=g.site_id)
    if not tok:
        # Count real guesses (a wrong/empty code) toward the limit; don't penalize a code
        # that is simply dead (revoked/expired/exhausted) — that's not a brute-force signal.
        if reason in ("not_found", "empty"):
            _gate_rl_record_fail(pod_id, ip)
        # Reason-specific messages: separate "check your code" (empty / not_found) from
        # "nothing you did" (revoked / expired / exhausted) so a visitor knows whether to
        # retry or to ask the pod owner for a fresh code.
        error, blame_owner = _GATE_REDEEM_MESSAGES.get(
            reason, ("That access code didn’t work. Try again, or ask the pod owner for a new one.", True)
        )
        html = _gate_login_html(pod_id, tapis_domain, network_key, return_path,
                                error=error, owner_fault=blame_owner)
        return HTMLResponse(content=html, status_code=401)

    # success — clear this IP's failure streak
    _gate_rl_clear(pod_id, ip)

    # success — set the session cookie HOST-ONLY (no domain=), so it is scoped to this
    # pod's hostname alone.
    #
    # It previously used the tenant base domain, which meant the gate session secret was
    # transmitted to EVERY other pod under *.pods.<tenant>.<base> and to the Tapis API
    # host — any other pod owner in the tenant could harvest and replay another pod's
    # gate cookie. Host-only is safe here because the whole redeem flow is proxied
    # through the pod's own hostname (traefik forwardAuth), so the browser's origin for
    # this response is exactly the host the cookie needs to be sent back to.
    # COOKIE_DOMAIN remains honored as a deliberate operator override.
    cookie_domain = conf.get('COOKIE_DOMAIN', None)
    max_age = 7 * 24 * 3600
    if tok.expires_at:
        remaining = int((tok.expires_at - datetime.utcnow()).total_seconds())
        max_age = max(60, min(max_age, remaining))
    response = RedirectResponse(url=f"https://{net_info['url']}{return_path}", status_code=302)
    # cookie_value is the high-entropy session value (a link's own secret, or a password
    # token's session_secret) — never the typed password.
    cookie_kwargs = {"secure": True, "httponly": True, "samesite": "lax", "max_age": max_age}
    if cookie_domain:
        cookie_kwargs["domain"] = cookie_domain
    response.set_cookie(_gate_cookie_name(pod_id), cookie_value or secret, **cookie_kwargs)
    return response


@router.get(
    "/pods/{pod_id_net}/gate/redeem",
    tags=["Pods"],
    summary="pod_access_gate_redeem_link",
    operation_id="pod_access_gate_redeem_link",
    include_in_schema=False)
async def pod_access_gate_redeem_link(pod_id_net, request: Request, access: str = Query(default="")):
    """Redeem a ?access= link secret into a session cookie."""
    logger.info(f"GET /pods/{pod_id_net}/gate/redeem - link redemption.")
    return await _gate_do_redeem(pod_id_net, request, access)


@router.post(
    "/pods/{pod_id_net}/gate/redeem",
    tags=["Pods"],
    summary="pod_access_gate_redeem_form",
    operation_id="pod_access_gate_redeem_form",
    include_in_schema=False)
async def pod_access_gate_redeem_form(pod_id_net, request: Request,
                                      secret: str = Form(default=""),
                                      return_path: str = Form(default="")):
    """Redeem a login-form password into a session cookie."""
    logger.info(f"POST /pods/{pod_id_net}/gate/redeem - form redemption.")
    return await _gate_do_redeem(pod_id_net, request, secret, return_path_override=return_path)


@router.get(
    "/pods/{pod_id}/access-tokens",
    tags=["Permissions"],
    summary="list_pod_access_tokens",
    operation_id="list_pod_access_tokens",
    response_model=PodAccessTokensResponse)
async def list_pod_access_tokens(pod_id):
    """List the access-gate credentials minted for a pod (metadata only — never the secret)."""
    logger.info(f"GET /pods/{pod_id}/access-tokens - Top of list_pod_access_tokens.")
    # ensure pod exists (and 404s cleanly if not)
    Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    tokens = PodAccessToken.list_for_pod(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    result = [tok.display() for tok in tokens]
    return ok(result=result, msg="Pod access tokens retrieved successfully.")


@router.post(
    "/pods/{pod_id}/access-tokens",
    tags=["Permissions"],
    summary="create_pod_access_token",
    operation_id="create_pod_access_token",
    response_model=PodAccessTokenMintResponse)
async def create_pod_access_token(pod_id, mint_request: AccessTokenMintRequest):
    """Mint an access-gate credential (password or shareable link). The raw secret and a
    redemption URL are returned ONCE — they are not recoverable afterward."""
    logger.info(f"POST /pods/{pod_id}/access-tokens - Top of create_pod_access_token.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    if mint_request.kind == "password" and not mint_request.password:
        raise ValueError("kind='password' requires a 'password' value.")

    expires_at = None
    if mint_request.expires_in_seconds:
        expires_at = datetime.utcnow() + timedelta(seconds=mint_request.expires_in_seconds)

    tok, raw = PodAccessToken.mint(
        pod_id, tenant=g.request_tenant_id, site=g.site_id,
        label=mint_request.label, kind=mint_request.kind, created_by=g.username,
        raw_secret=mint_request.password, expires_at=expires_at, max_uses=mint_request.max_uses,
    )

    # Build a redemption URL pointing straight at the /gate/redeem endpoint (NOT the pod
    # URL with ?access=). Redeem sets the cookie on a direct response we control and then
    # 302s to the pod, so a shared link works without relying on Traefik forwarding
    # X-Forwarded-Uri. ONLY for link tokens — a password's URL would leak the human
    # password (a reuse risk); passwords are meant to be typed on the gate screen.
    redemption_url = ""
    if mint_request.kind == "link":
        try:
            for _k, _net in (pod.networking or {}).items():
                n = _net if isinstance(_net, dict) else _net.dict()
                if n.get("protocol") == "http" and n.get("url") and ".pods." in n["url"]:
                    _tapis_domain = n["url"].split(".pods.", 1)[1]
                    redemption_url = (
                        f"https://{_tapis_domain}/v3/pods/{pod_id}/gate/redeem?access={quote(raw)}"
                    )
                    break
        except Exception:
            redemption_url = ""

    # Audit trail — record the mint in the pod's action_logs (label only, never the secret).
    try:
        _lbl = mint_request.label or tok.id[:8]
        pod.db_update(log=f"Minted access-gate code '{_lbl}' ({mint_request.kind}) by {g.username}",
                      tenant=g.request_tenant_id, site=g.site_id)
    except Exception as e:
        logger.warning(f"Failed to write mint action_log for pod {pod_id}: {e}")

    result = tok.display()
    result["secret"] = raw
    result["redemption_url"] = redemption_url
    return ok(result=result, msg="Access token minted. Save the secret now — it will not be shown again.")


@router.delete(
    "/pods/{pod_id}/access-tokens/{token_id}",
    tags=["Permissions"],
    summary="revoke_pod_access_token",
    operation_id="revoke_pod_access_token",
    response_model=PodAccessTokensResponse)
async def revoke_pod_access_token(pod_id, token_id):
    """Revoke (permanently disable) an access-gate credential. Existing sessions using it
    stop working on their next gate check."""
    logger.info(f"DELETE /pods/{pod_id}/access-tokens/{token_id} - Top of revoke_pod_access_token.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    tok = PodAccessToken.get(token_id, pod_id, tenant=g.request_tenant_id, site=g.site_id)
    if not tok:
        raise ValueError(f"Access token '{token_id}' not found for pod '{pod_id}'.")
    tok.revoked = True
    tok.db_update(tenant=g.request_tenant_id, site=g.site_id)
    # Audit trail — record the revoke in the pod's action_logs.
    try:
        pod.db_update(log=f"Revoked access-gate code '{tok.label or tok.id[:8]}' by {g.username}",
                      tenant=g.request_tenant_id, site=g.site_id)
    except Exception as e:
        logger.warning(f"Failed to write revoke action_log for pod {pod_id}: {e}")
    tokens = PodAccessToken.list_for_pod(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    result = [t.display() for t in tokens]
    return ok(result=result, msg="Access token revoked successfully.")


@router.get(
    "/pods/{pod_id}/events",
    tags=["Pods"],
    summary="get_pod_events",
    operation_id="get_pod_events")
async def get_pod_events(
    pod_id,
    limit: int = Query(default=50, ge=1, le=500, description="Max events to return."),
):
    """
    Get Kubernetes events for a pod.

    Returns events from the K8s event stream for this pod's underlying container.
    Useful for diagnosing mount failures, image pull errors, OOMKilled, and container
    crash reasons that do not surface in the pods-service action_logs.

    Also returns a quick summary of which ephemeral ConfigMaps exist vs are missing.
    """
    logger.info(f"GET /pods/{pod_id}/events - Top of get_pod_events.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    if not pod:
        # Guard: admins bypass the auth-layer 404 (check_object_id), so without this the
        # .k8_name deref below raises an opaque 500 ('NoneType' object has no attribute ...).
        raise ResourceError(f"Pod with id '{pod_id}' not found in tenant '{g.request_tenant_id}', site '{g.site_id}'.", 404)

    # ── k8s events ────────────────────────────────────────────────────────────
    events = []
    try:
        ev_list = k8.list_namespaced_event(
            namespace=NAMESPACE,
            field_selector=f"involvedObject.name={pod.k8_name}",
        )
        for e in sorted(ev_list.items, key=lambda x: (x.last_timestamp or datetime.min), reverse=True)[:limit]:
            events.append({
                "type":           e.type,
                "reason":         e.reason,
                "message":        e.message,
                "count":          e.count,
                "first_time":     e.first_timestamp.isoformat() if e.first_timestamp else None,
                "last_time":      e.last_timestamp.isoformat()  if e.last_timestamp  else None,
                "source":         e.source.component if e.source else None,
            })
    except Exception as e:
        logger.warning(f"get_pod_events: failed to list k8 events for {pod_id}: {e}")

    # ── ephemeral ConfigMap status ─────────────────────────────────────────────
    import hashlib, os as _os, re as _re
    cm_status = []
    vol_mounts = pod.volume_mounts or {}
    for mount_path, vol in vol_mounts.items():
        if vol is None:
            continue
        vtype = vol.get("type", "") if isinstance(vol, dict) else getattr(vol, "type", "")
        if str(vtype).lower() != "ephemeral":
            continue
        # Replicate the configmap name generation from kubernetes_templates.py
        mount_hash = hashlib.md5(mount_path.encode()).hexdigest()[:8]
        raw_src = "ephemeral"
        src = _re.sub(r'[^a-z0-9-]', '', raw_src.lower())
        if not src or not src[0].isalnum():
            src = "vol" + src
        if src and not src[-1].isalnum():
            src = src.rstrip('-')
        if not src:
            src = "ephemeral"
        full_name = f"{pod.k8_name}--{src}--{mount_hash}"
        if len(full_name) > 62:
            full_name = full_name[:62]
        cm_name = full_name.lower()[:63]
        exists = configmap_exists(cm_name, namespace=NAMESPACE)
        config_content = vol.get("config_content") if isinstance(vol, dict) else getattr(vol, "config_content", None)
        cm_status.append({
            "mount_path":       mount_path,
            "configmap_name":   cm_name,
            "exists_in_k8s":    exists,
            "config_content_bytes": len(config_content) if config_content else 0,
        })

    return ok(result={
        "pod_id":    pod_id,
        "k8_name":   pod.k8_name,
        "status":    pod.status,
        "events":    events,
        "ephemeral_configmaps": cm_status,
    }, msg=f"Pod events retrieved for {pod_id}.")


@router.get(
    "/pods/{pod_id}/metrics",
    tags=["Pods"],
    summary="get_pod_metrics",
    operation_id="get_pod_metrics")
async def get_pod_metrics(
    pod_id,
    history: bool = Query(False, description="Include ~1h cpu/mem time series (usage_history). Served from the tenant-wide range cache — no extra backend load."),
):
    """
    Get live compute and traffic metrics for a pod.

    Returns current CPU/memory usage from the configured metrics backend
    (`metrics_backend` config: grafana/metrics-server/none) plus
    24-hour traffic statistics from the traffic_logs table. When no backend is
    configured or the backend is down, the `usage` field explains why —
    the request still returns 200; traffic data is always available.
    With `?history=true` the response also carries `usage_history`
    ({"cpu": [[ts, cores]...], "mem": [[ts, bytes]...], window_s, step_s}) —
    filtered out of the SAME cached tenant-wide range query the fleet
    endpoints use, so per-pod sparklines add zero metrics-backend queries.
    """
    logger.info(f"GET /pods/{pod_id}/metrics - Top of get_pod_metrics.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    if not pod:
        # Guard: admins bypass the auth-layer 404 (check_object_id), so without this the
        # .k8_name deref below raises an opaque 500 ('NoneType' object has no attribute ...).
        raise ResourceError(f"Pod with id '{pod_id}' not found in tenant '{g.request_tenant_id}', site '{g.site_id}'.", 404)

    # ── Live usage via the pluggable metrics backend (never raises) ────────────
    from metrics_backend import pod_usage_dict, bulk_range_dict, explore_hint
    usage = pod_usage_dict(pod.k8_name)

    usage_history = None
    if history:
        bulk = bulk_range_dict(f"pods-{g.site_id}-{g.request_tenant_id}-.*")
        if "unavailable" in bulk:
            usage_history = {"unavailable": bulk["unavailable"]}
        else:
            series = bulk.get("pods", {}).get(pod.k8_name) or {"cpu": [], "mem": []}
            usage_history = {**series, "window_s": bulk.get("window_s"), "step_s": bulk.get("step_s")}

    # ── k8s metrics-server (legacy path; {} unless metrics-server installed) ──
    k8s_data = get_pod_k8s_metrics(pod.k8_name)

    # ── 24-hour traffic summary ────────────────────────────────────────────────
    traffic_24h: dict = {}
    try:
        from sqlmodel import select, func as sqlfunc
        from models_traffic import TrafficLog
        from datetime import timedelta
        from stores import pg_store

        store = pg_store[g.site_id][g.request_tenant_id]
        since = datetime.utcnow() - timedelta(hours=24)

        # Total requests and avg latency
        stmt_all = (
            select(
                sqlfunc.count().label("total"),
                sqlfunc.avg(TrafficLog.duration_ms).label("avg_ms"),
                sqlfunc.max(TrafficLog.duration_ms).label("max_ms"),
            )
            .where(TrafficLog.pod_id == pod_id)
            .where(TrafficLog.tenant_id == g.request_tenant_id)
            .where(TrafficLog.ts >= since)
        )
        row_all = store.run("execute", stmt_all, first=True)

        # Successful (< 400)
        stmt_ok = (
            select(sqlfunc.count().label("cnt"))
            .where(TrafficLog.pod_id == pod_id)
            .where(TrafficLog.tenant_id == g.request_tenant_id)
            .where(TrafficLog.ts >= since)
            .where(TrafficLog.status_code < 400)
        )
        row_ok = store.run("execute", stmt_ok, first=True)

        total = row_all.total if row_all else 0
        success = row_ok.cnt if row_ok else 0
        traffic_24h = {
            "total":          total or 0,
            "success":        success or 0,
            "error_rate_pct": round((1 - (success / total)) * 100, 1) if total else 0,
            "avg_latency_ms": round(row_all.avg_ms or 0, 1) if row_all else 0,
            "max_latency_ms": round(row_all.max_ms or 0, 1) if row_all else 0,
        }
    except Exception as e:
        logger.warning(f"get_pod_metrics traffic query error: {e}")
        traffic_24h = {"error": str(e)}

    # ── Resource config ────────────────────────────────────────────────────────
    res = pod.resources or {}

    return ok(result={
        "pod_id":   pod_id,
        "k8_name":  pod.k8_name,
        "status":   pod.status,
        "usage":    usage,          # cpu/mem from metrics backend, or {"unavailable": reason}
        "usage_history": usage_history,  # only with ?history=true
        "backend":  {"explore": explore_hint()},  # Grafana deep-link ingredients (or None)
        "k8s_metrics": k8s_data,    # {} if metrics-server unavailable
        "resources": {
            "cpu_request_m":  int(res.get("cpu_request",  0) or 0),
            "cpu_limit_m":    int(res.get("cpu_limit",    0) or 0),
            "mem_request_mb": int(res.get("mem_request",  0) or 0),
            "mem_limit_mb":   int(res.get("mem_limit",    0) or 0),
            "gpus":           int(res.get("gpus",         0) or 0),
        },
        "traffic_24h": traffic_24h,
    }, msg=f"Pod metrics retrieved for {pod_id}.")
