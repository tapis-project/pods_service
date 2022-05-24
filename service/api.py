from req_utils import error_handler, HttpUrlRedirectMiddleware
from tapisservice.tapisfastapi.utils import GlobalsMiddleware
from tapisservice.tapisfastapi.auth import TapisMiddleware

from __init__ import Tenants
from fastapi import FastAPI
from fastapi.middleware import Middleware

from auth import authorization, authentication
from api_pods import router as router_pods
from api_pods_podname import router as router_pods_podsname
from api_pods_podname_func import router as router_pods_podsname_func


api = FastAPI(title="pods",
              debug=False,
              exception_handlers={Exception: error_handler},
              middleware=[
                  Middleware(HttpUrlRedirectMiddleware),
                  Middleware(GlobalsMiddleware),
                  Middleware(TapisMiddleware, tenant_cache=Tenants, authn_callback=authentication, authz_callback=authorization)
              ])

api.include_router(router_pods)
api.include_router(router_pods_podsname)
api.include_router(router_pods_podsname_func)
