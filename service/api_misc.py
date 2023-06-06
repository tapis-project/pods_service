from fastapi import APIRouter
from fastapi.responses import JSONResponse
from tapisservice.errors import BaseTapisError
from tapisservice.tapisfastapi.utils import g, ok, error
from kubernetes_utils import get_traefik_configmap
from models_pods import TapisApiModel
import yaml

router = APIRouter()

@router.get("/traefik-config",
    tags=["Misc"],
    summary="traefik_config",
    operation_id="traefik_config")
async def api_traefik_config():
    """
    Supplies traefik-config to service. Returns json traefik-config object for
    traefik to use with the http provider. Dynamic configs don't work well in 
    Kubernetes.
    """
    config = get_traefik_configmap()
    yaml_config = yaml.safe_load(config.to_dict()['data']['traefik.yml'])
    return yaml_config

@router.get("/healthcheck",
    tags=["Misc"],
    summary="healthcheck",
    operation_id="healthcheck")
async def api_healthcheck():
    """
    Health check for service. Returns healthy when api is running.
    Should add database health check, should add kubernetes health check
    """
    return ok("I promise I'm healthy.")

@router.get(
    "/error-handler/{status}",
    tags=["Misc"],
    summary="error_handler",
    operation_id="error_handler")
async def error_codes(status):
    """Handles all error codes from Traefik.
    """
    status = int(status)
    match status:
        case 400:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 401:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 402:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 403:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 404:
            message = "Invalid request: Invalid request: the requested URL is not an Pods endpoint."
        case 405:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 500:
            message = "Unable to parse Pods service response. The server may be misconfigured or overloaded."
        case 501:
            message = "Unable to parse Pods service response. The server may be misconfigured or overloaded."
        case 502:
            message = "Timeout error waiting on Pods service response. The server may be busy or overloaded."
        case 503:
            message = "Unable to parse Pods service response. The server may be misconfigured or overloaded."
        case 504:
            message = "Unable to parse Pods service response. The server may be misconfigured or overloaded."
        case _:
            message = "Invalid request: The Pods service does not know how to fulfill the request."

    return JSONResponse(status_code=status, content=error(message))
