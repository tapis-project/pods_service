from req_utils import error_handler
from tapisservice.tapisfastapi.utils import GlobalsMiddleware
from tapisservice.tapisfastapi.auth import TapisMiddleware
#from auth import TapisMiddleware

from __init__ import Tenants
from fastapi import FastAPI
from fastapi.middleware import Middleware
from api_pods_podname import router as router_pods_podsname
from api_pods import router as router_pods
from auth import authorization

api = FastAPI(title="kgservice",
              debug=False,
              exception_handlers={Exception: error_handler},
              middleware=[
                  Middleware(GlobalsMiddleware),
                  Middleware(TapisMiddleware, tenant_cache=Tenants, authn_callback=None, authz_callback=authorization)
              ])

api.include_router(router_pods_podsname)
api.include_router(router_pods)
