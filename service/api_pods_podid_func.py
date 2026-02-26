from fastapi import APIRouter, Request, UploadFile, File, Form, Body, Path, Query
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from models_pods import Pod, Password, PodResponse, PodPermissionsResponse, PodCredentialsResponse, PodLogsResponse, ExecutePodCommands, PodBaseFull
from models_templates_tags import Template, TemplateTag, TemplateTagResponse, NewTemplateTagFromPod
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
from kubernetes_utils import run_k8_exec, k8s_copy_bytes_to_pod, NAMESPACE
from utils import check_permissions
from errors import ResourceError, PermissionsException
from models_volume_mounts_utils import validate_volume_mounts_on_start
from datetime import datetime
import time
import re
import asyncio
import io

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
            stdout, stderr, duration, status, success = run_k8_exec(pod.k8_name, cmd, timeout=command.command_timeout)
            
            results.append({
                "command": cmd,
                "stdout": stdout,
                "stderr": stderr,
                "success": success if success else (status if status else False),
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

    # Update pod history with summary
    summary = f"'{g.username}' executed {len(commands)} commands."
    if custom_msg:
        summary += f" ({custom_msg.split(' Consider')[0]})"
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


def validate_token(request: Request, token: str = None):
    """
    Validate a Tapis JWT from cookies or headers by making a call to the get_userinfo endpoint.
    Returns authorized:bool, username:str, roles:List[str]
    """
    logger.debug(f"Validating token from request: cookies={request.cookies}, headers={request.headers}")
    token = token or request.cookies.get('X-Tapis-Token') or request.headers.get('X-Tapis-Token') or request.headers.get('x-tapis-token') or request.headers.get('X-TAPIS-TOKEN')
    if not token:
        logger.debug("Token not found in cookies or headers.")
        return False, None, None

    url = f"{request.base_url}v3/oauth2/userinfo".replace('http://', 'https://')
    logger.debug(f"Running get_userinfo with url: {url}")
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

    ## We now want to check if session/headers have a valid Tapis token for the current site/tenant. If so, we can return 200.
    ## Session and headers can both be manually modified, this is where we must validate the token is valid via a call to get_userinfo.
    try:
        authorized, username, roles = validate_token(request)
        # check if user is allowed to access pod
        tapis_auth_allowed_users = net_info.get("tapis_auth_allowed_users", [])
        if authorized:
            logger.debug(f"User authenticated: {username}")
            tapis_auth_headers = get_pod_networking_objects(
                net_info=net_info,
                username=username,
                tenant_id=g.request_tenant_id,
                site_id=g.site_id
            )
            if tapis_auth_allowed_users:
                pod_permissions = pod_init.get_permissions()
                if not check_tapis_auth_allowed(username, tapis_auth_allowed_users, pod_permissions):
                    raise Exception(f"User {username} not in networking.tapis_auth_allowed_users for pod_id: {pod_id_net}.")
            return JSONResponse(content=ok("Already authenticated"), status_code=200, headers=tapis_auth_headers)
    except Exception as e:
        logger.debug(f"Authentication failed: {e.detail}")

    ## if request headers has X-Tapis-Token, we assume they're not browser based and want to use the token
    ## if it doesn't validate they need a warning message rather than getting an error due to redirect
    logger.debug(f"request_info dump: {request.headers}, {request.cookies}, {request.query_params}")
    if request.headers.get('X-Tapis-Token') or request.headers.get('x-tapis-token2'):
        logger.debug(f"X-Tapis-Token found in headers, but not authenticated. Returning 403.")
        return JSONResponse(content="Not authenticated", status_code=403)
    

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

        # tapis_auth_headers = get_pod_networking_objects(
        #     net_info=net_info,
        #     username=username,
        #     tenant_id=g.request_tenant_id,
        #     site_id=g.site_id
        # )
        tapis_auth_allowed_users = net_info.get("tapis_auth_allowed_users", [])
        if tapis_auth_allowed_users:
            pod_permissions = pod_init.get_permissions()
            if not check_tapis_auth_allowed(username, tapis_auth_allowed_users, pod_permissions):
                raise Exception(f"User {username} not in networking.tapis_auth_allowed_users for pod_id: {pod_id_net}.")

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