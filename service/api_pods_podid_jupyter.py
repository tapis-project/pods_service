from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, RedirectResponse
from models_pods import Pod, Password, PodResponse, PodPermissionsResponse, PodCredentialsResponse, PodLogsResponse, ExecutePodCommands, NewPod
from models_templates_tags import Template, TemplateTag, TemplateTagResponse, NewTemplateTagFromPod
from models_templates_utils import combine_pod_and_template_recursively
from models_misc import SetPermission
from channels import CommandChannel
from codes import OFF, ON, RESTART, REQUESTED, STOPPED, USER
import requests
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from __init__ import t, BadRequestError
from typing import List, Any
from kubernetes_utils import run_k8_exec
from utils import check_permissions
from errors import ResourceError, PermissionsException
from datetime import datetime
import time
from api_pods import create_pod as create_pod_api

from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


@router.get(
    "/pods/jupyter/ensure",
    tags=["Jupyter"],
    summary="Ensure user has a running Jupyter pod, useful for starting up coding environment",
    operation_id="ensure_jupyter_pod",
    response_model=Any
)
async def ensure_jupyter_pod(request: Request):
    """
    Ensure the current user has a running Jupyter pod.
    If not, create a new one named '{username}jupyter' from the base Jupyter template.
    Returns pod name and URL.
    """
    logger.info("GET /pods/jupyter/ensure - Top of ensure_jupyter_pod.")

    pods =  Pod.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)
    jupyterlab_on_pods = []
    jupyterlab_off_pods = []
    for pod in pods:
        if getattr(pod, "template", "").startswith("jupyterlab:"):
            if getattr(pod, "status_requested", None) == "ON":
                jupyterlab_on_pods.append(pod)
            elif getattr(pod, "status_requested", None) in ["OFF", "RESTART"]:
                jupyterlab_off_pods.append(pod)

    jupyterlab_pods = jupyterlab_on_pods + jupyterlab_off_pods
    logger.info(f"Pods with jupyterlab template")

    # prefer running pod
    if jupyterlab_on_pods:
        logger.debug(f"found jupyter pods that are running: {jupyterlab_on_pods}")
        #should sort by newest as well
        pod = jupyterlab_on_pods[0]
        jupyter_bits = {
            "pod_id": getattr(pod, 'pod_id', ""),
            "status": getattr(pod, 'status_requested', ""),
            "networking": getattr(pod, 'networking', ""),
            "url": f"https://{getattr(pod, 'networking', {}).get('default', {}).get('url', '')}"
        }
        return ok(result=jupyter_bits, msg="Retrieved existing Jupyter pod successfully.")
    else:
        logger.debug(f"no jupyter pods found, creating a new one")
        # No running pod, create a new one
        max_increment = 999
        increment = 1
        existing_ids = {getattr(p, "pod_id", "") for p in jupyterlab_off_pods}
        while increment <= max_increment:
            pod_id = f"tapistempjupyter{g.username}{increment:03d}"
            if pod_id not in existing_ids:
                break
            increment += 1
        else:
            logger.error("Exceeded maximum number of Jupyter pods (999) for user.")
            raise HTTPException(status_code=400, detail="Exceeded maximum number of Jupyter pods (999) for user.")

        try:
            logger.debug(f"Creating new Jupyter pod with ID: {pod_id}")
            new_pod = {
                "pod_id": pod_id,
                "template": "jupyterlab:noauth",
                "description": "Pod created via ensure_jupyter_pod endpoint. Authentication handled by Tapis. Stops in 48 hours.",
                # "tenant": g.request_tenant_id,
                # "site": g.site_id,
                # "username": g.username
            }
            # Convert dict to NewPod Pydantic model
            new_pod_obj = NewPod(**new_pod)
            try:
                pod_res = await create_pod_api(new_pod_obj)
            except Exception as e:
                logger.error(f"Error creating Jupyter pod: {e}")
                raise HTTPException(status_code=500, detail=f"Could not create Jupyter pod: {e}")

            logger.debug(f"Response from create_pod: {pod_res}")
            pod = pod_res['result']
            logger.debug(f"Created new Jupyter pod: {pod}")
            # Check if pod creation was successful
            jupyter_bits = {
                "pod_id": pod.get('pod_id'),
                "status": pod.get('status'),
                "networking": pod.get('networking'),
                "url": f"https://{pod.get('networking', {}).get('default', {}).get('url', '')}"
            }
            return ok(result=jupyter_bits, msg="Created new Jupyter pod successfully.")
        except Exception as e:
            logger.error(f"Error creating Jupyter pod: {e}")
            raise HTTPException(status_code=500, detail=f"Could not create Jupyter pod: {e}")

@router.post(
    "/pods/jupyter/{pod_id}/upload",
    tags=["Jupyter"],
    summary="Upload a document to the user's Jupyter pod",
    operation_id="upload_to_jupyter",
)
async def upload_to_jupyter(
    pod_id: str,
    request: Request,
    file: UploadFile = File(...),
    path: str = Form(...)
):
    """
    Upload a document to the user's running Jupyter pod using the Jupyter API.
    Input: multipart form (file), and 'path' (destination in Jupyter).
    """
    logger.info(f"POST /pods/jupyter/{pod_id}/upload - Top of upload_to_jupyter.")
    # still not working
    return JSONResponse(status_code=200, content={"message": "Not implemented yet."})

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Check permissions and status
    if not pod or getattr(pod, "status_requested", None) != "ON":
        raise HTTPException(status_code=404, detail="Can't find suitable running Jupyter pod for user.")
    logger.debug(f"jupyter upload input path: {path}")

    # Get networking.url for upload
    networking = getattr(pod, "networking", {})
    default_network = networking.get('default', None)
    if not default_network:
        raise HTTPException(status_code=500, detail="No default networking information found for the pod.")
    logger.debug(f"default_network: {default_network}")
    jupyter_url = default_network.get("url") if isinstance(default_network, dict) else getattr(default_network, "url", None)
    if not jupyter_url:
        raise HTTPException(status_code=500, detail="Could not determine Jupyter pod URL.")

    logger.debug(f"jupyter upload input path: {path}")
    upload_url = f"https://{jupyter_url}/api/contents/{path}"
    file_bytes = await file.read()
    data = {
        "content": file_bytes.decode("utf-8"),
        "type": "notebook",
        "format": "text"
    }
    # Forward x-tapis-token header if present
    headers = {}
    tapis_token = request.headers.get("x-tapis-token")
    if tapis_token:
        headers["x-tapis-token"] = tapis_token

    logger.debug(f"request headers: {request.headers}; x-tapis-token: {tapis_token}; headers: {headers}")
    try:
        resp = requests.put(upload_url, json=data, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Error uploading file to Jupyter: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload file to Jupyter: {e}")

    return ok(result={"upload_path": path, "pod_name": pod.pod_id, "url": "https://"+jupyter_url})