from fastapi import FastAPI, Request
from fastapi.middleware import Middleware
from fastapi.exceptions import RequestValidationError
from jsonschema import ValidationError
from req_utils import ok, error_handler, GlobalsMiddleware, TapisMiddleware, g
#from auth import TapisMiddleware

from tapisservice.config import conf
from tapisservice.errors import BaseTapisError, PermissionsError
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from routers.pods import router as pod_router, test2


api = FastAPI(title="kgservice",
              debug=False,
              exception_handlers={Exception: error_handler,
                                  RequestValidationError: error_handler},
              middleware=[
                  Middleware(GlobalsMiddleware),
                  Middleware(TapisMiddleware)
              ])

api.include_router(pod_router)


@api.get("/healthcheck")
async def api_healthcheck():
    """
    Health check for service. Returns healthy when api is running.
    Should add database health check, should add kubernetes health check
    """
    return ok("I promise I'm healthy.")

@api.get("/broken/")
async def broken() -> any:
    fillernodes = []
    print('days')
    raise PermissionsError("PLANNED")

@api.get("/global/")
async def globaltest() -> any:
    g.test = 26
    test2()
    return g.tapis_user, g.test
