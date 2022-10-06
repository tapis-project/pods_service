from fastapi import APIRouter
from tapisservice.errors import BaseTapisError
from tapisservice.tapisfastapi.utils import g, ok
from kubernetes_utils import get_traefik_configmap
import yaml

router = APIRouter()

@router.get("/traefik-config")
async def api_traefik_config():
    """
    Supplies traefik-config to service. Returns json traefik-config object for
    traefik to use with the http provider. Dynamic configs don't work well in 
    Kubernetes.
    """
    print("boo")
    config = get_traefik_configmap()
    yaml_config = yaml.safe_load(config.to_dict()['data']['traefik-conf.yml'])
    return yaml_config

@router.get("/healthcheck")
async def api_healthcheck():
    """
    Health check for service. Returns healthy when api is running.
    Should add database health check, should add kubernetes health check
    """
    return ok("I promise I'm healthy.")