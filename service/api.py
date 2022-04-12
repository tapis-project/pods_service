from fastapi import FastAPI
from req_utils import ok, error_handler, GlobalsMiddleware, g

from tapisservice.config import conf
from tapisservice.errors import BaseTapisError, PermissionsError
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from routers.pods import router as pod_router, test2



api = FastAPI(title="kgservice",
              debug=False,
              exception_handlers={Exception: error_handler})

api.include_router(pod_router)
api.add_middleware(GlobalsMiddleware)

@api.get("/fillerapi/healthcheck")
async def api_healthcheck():
    """
    Health check for service. Returns healthy when api is running.
    Should add database health check, should add kubernetes health check
    """
    return ok("I promise I'm healthy.")

@api.get("/fillerapi/")
async def api_list_fillernode() -> any:
    fillernodes = []
    print('days')
    fillernodes = 2
    fillernodes['22']
    return fillernodes

@api.get("/test/")
async def api_list_fillernode() -> any:
    fillernodes = []
    print('days')
    raise PermissionsError("PLANNED")
    fillernodes = 2
    fillernodes['twenty']
    return fillernodes

@api.get("/globaltest/")
async def globaltest() -> any:
    g.crazy = 26
    test2()
    return g.crazy
