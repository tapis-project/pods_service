from req_utils import error_handler, GlobalsMiddleware, TapisMiddleware
#from auth import TapisMiddleware

from fastapi import FastAPI
from fastapi.middleware import Middleware
from api_pod import router as pod_router
from api_pods import router as pods_router


api = FastAPI(title="kgservice",
              debug=False,
              exception_handlers={Exception: error_handler},
              middleware=[
                  Middleware(GlobalsMiddleware),
                  Middleware(TapisMiddleware)
              ])

api.include_router(pod_router)
api.include_router(pods_router)
