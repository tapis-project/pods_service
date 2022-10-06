from req_utils import error_handler, HttpUrlRedirectMiddleware
from tapisservice.tapisfastapi.utils import GlobalsMiddleware
from tapisservice.tapisfastapi.auth import TapisMiddleware

from __init__ import Tenants
from fastapi import FastAPI
from fastapi.middleware import Middleware

from auth import authorization, authentication
from api_pods import router as router_pods
from api_pods_podid import router as router_pods_podsname
from api_pods_podid_func import router as router_pods_podsname_func
from api_misc import router as router_misc


description = """
The Pods Service is a web service and distributed computing platform providing pods-as-a-service (PaaS). The service 
implements a message broker and processor model that requests pods, alongside a health module to poll for pod
data, including logs, status, and health. The primary use of this service is to have quick to deploy long-lived
services based on Docker images that are exposed via HTTP or TCP endpoints listed by the API.

**The Pods service provides functionality for two types of pod solutions:**
 * **Templated Pods** for run-as-is popular images. Neo4J is one example, the template manages TCP ports, user creation, and permissions.
 * **Custom Pods** for arbitrary docker images with less functionality. In this case we will expose port 5000 and do nothing else.
"""

tags_metadata = [
    {
        "name": "Pods",
        "description": "Create and command pods.",
    },
    {
        "name": "Credentials",
        "description": "Manage pod's credentials used.",
    },
    {
        "name": "Logs",
        "description": "Manage pod logs.",
    },
    {
        "name": "Permissions",
        "description": "Manage pod permissions. Grant specific TACC users **READ**, **USER**, and **ADMIN** level permissions.",
    }
]

api = FastAPI(
    title="Tapis Pods Service",
    description=description,
    openapi_tags=tags_metadata,
    version="0.30",
    contact={
        "name": "CIC Support",
        "email": "cicsupport@tacc.utexas.edu",
    },
    license_info={
        "name": "BSD 3.0",
        "url": "https://github.com/tapis-project/pods_service",
    },
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
api.include_router(router_misc)
