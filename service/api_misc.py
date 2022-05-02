from fastapi import APIRouter
from tapisservice.errors import BaseTapisError
from req_utils import ok, g

router = APIRouter()


@router.get("/healthcheck")
async def api_healthcheck():
    """
    Health check for service. Returns healthy when api is running.
    Should add database health check, should add kubernetes health check
    """
    return ok("I promise I'm healthy.")

@router.get("/broken/")
async def broken():
    fillernodes = []
    print('days')
    raise BaseTapisError("PLANNED")

@router.get("/global/")
async def globaltest():
    g.test = 26
    return g.tapis_user, g.test
