from utils import error_handler, HttpUrlRedirectMiddleware
from tapisservice.tapisfastapi.utils import GlobalsMiddleware
from tapisservice.tapisfastapi.auth import TapisMiddleware
from tapisservice.config import conf

import re as _re
from starlette.requests import Request as _Request
from starlette.types import ASGIApp as _ASGIApp, Receive as _Receive, Scope as _Scope, Send as _Send
from tapisservice.tapisfastapi.auth import FormattedRequest as _FormattedRequest

# Regex matching pod OAuth routes: /pods/<pod_id_net>/auth and /pods/<pod_id_net>/auth/callback
_POD_AUTH_PATH_RE = _re.compile(r'^/pods/[^/]+/auth(/callback)?$')

class PodsTapisMiddleware(TapisMiddleware):
    """TapisMiddleware wrapper that skips token authentication for pod OAuth routes.
    
    The /pods/{pod_id_net}/auth and /pods/{pod_id_net}/auth/callback routes are browser-initiated
    OAuth flow endpoints. Letting TapisMiddleware's core_validate_request_token run on these routes
    can cause errors when browsers send expired/invalid token cookies, since it may raise
    AuthenticationError instead of NoTokenError.
    
    We skip only the authentication step (token validation) but still run the authorization callback,
    which handles NEED-BASEURL tenant resolution (setting g.request_tenant_id and g.site_id).
    """
    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["type"] == "http":
            request = _Request(scope, receive)
            if _POD_AUTH_PATH_RE.match(request.url.path):
                # Skip token authentication but still run authorization (NEED-BASEURL tenant resolution)
                formatted_request = _FormattedRequest(
                    headers=request.headers,
                    base_url=request.base_url._url,
                    url=request.url,
                    method=request.method)
                if self.authz_callback:
                    self.authz_callback(formatted_request)
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)

from __init__ import Tenants
from fastapi import FastAPI
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError

from auth import authorization, authentication
from api_pods import router as router_pods
from api_pods_podid import router as router_pods_podsid
from api_pods_podid_func import router as router_pods_podsid_func
from api_pods_podid_jupyter import router as router_pods_podsid_jupyter
from api_volumes import router as router_volumes
from api_volumes_volid import router as router_volumes_volumeid
from api_volumes_volid_func import router as router_volumes_volumeid_func
from api_snapshots import router as router_snapshots
from api_snapshots_snapid import router as router_snapshots_snapshotid
from api_snapshots_snapid_func import router as router_snapshots_snapshotid_func
from api_templates import router as router_templates
from api_templates_templateid import router as router_templates_templateid
from api_templates_templateid_tags import router as router_templates_templateid_tags
from api_templates_templateid_tags_tagid import router as router_templates_templateid_tags_tagid
from api_templates_templateid_func import router as router_templates_templateid_func
from api_images import router as router_images
from api_images_imageid import router as router_images_imageid
from api_secrets import router as router_secrets
from api_secrets_secretid import router as router_secrets_secretid
from api_secrets_secretid_func import router as router_secrets_secretid_func
from api_clusters import router as router_clusters
from api_misc import router as router_misc


description = """
The Pods Service is a web service and distributed computing platform providing pods-as-a-service (PaaS). The service 
implements a message broker and processor model that requests pods, alongside a health module to poll for pod
data, including logs, status, and health. The primary use of this service is to have quick to deploy long-lived
services based on Docker images that are exposed via HTTP or TCP endpoints listed by the API.

**The Pods service provides functionality for two types of pod solutions:**
 * **Templated Pods** for run-as-is popular images. Neo4J is one example, the template manages TCP ports, user creation, and permissions.
 * **Custom Pods** for arbitrary docker images with less functionality. In this case we will expose port 5000 and do nothing else.

 The live-docs act as the most up-to-date API reference. Visit the [documentation for more information](https://tapis.readthedocs.io/en/latest/technical/pods.html).
"""

tags_metadata = [
    {
        "name": "Pods",
        "description": "Create and command pods.",
    },
    {
        "name": "Templates",
        "description": "Create and manage templated pod definitions for use in pod deployment.",
    },
    {
        "name": "Volumes",
        "description": "Create and manage volumes.",
    },
    {
        "name": "Snapshots",
        "description": "Create and manage snapshots.",
    },
    {
        "name": "Images",
        "description": "Create and manage docker images available in the service.",
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
    version="26Q1.1",
    contact={
        "name": "CIC Support",
        "email": "cicsupport@tacc.utexas.edu",
        "url": "https://tapis-project.org"
    },
    license_info={
        "name": "BSD 3.0",
        "url": "https://github.com/tapis-project/pods_service",
    },
    debug=False,
    exception_handlers={
        Exception: error_handler,
        RequestValidationError: error_handler,
        422: error_handler
    },
    middleware=[
        Middleware(HttpUrlRedirectMiddleware),
        Middleware(GlobalsMiddleware),
        Middleware(
            CORSMiddleware,
            allow_origins=conf.get("cors_allow_origins", ["http://localhost:3000", "http://upstream.pods.tacc.tapis.io"]),# "*", "http://localhost:3001", "localhost:5000", "http://localhost:5000", "localhost"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["X-Tapis-Token", "Origin", "Access-Control-Request-Methods", "*"],
            # I don't think we need X-TapisUsername, it's for auth testing, but it's here.
            expose_headers=["X-TapisUsername", "x-tapis-token", "*"],
            max_age=600),
        Middleware(
            PodsTapisMiddleware,
            tenant_cache=Tenants,
            authn_callback=authentication,
            authz_callback=authorization)
    ])

# misc - must be first due to pods/auth route
api.include_router(router_misc)
# templates
api.include_router(router_templates)
api.include_router(router_templates_templateid)
api.include_router(router_templates_templateid_tags)
api.include_router(router_templates_templateid_func)
api.include_router(router_templates_templateid_tags_tagid)
# images
api.include_router(router_images)
api.include_router(router_images_imageid)
# snapshots
api.include_router(router_snapshots)
api.include_router(router_snapshots_snapshotid)
api.include_router(router_snapshots_snapshotid_func)
# volumes
api.include_router(router_volumes)
api.include_router(router_volumes_volumeid)
api.include_router(router_volumes_volumeid_func)
# jupyter
api.include_router(router_pods_podsid_jupyter)
# clusters
#api.include_router(router_clusters)
# secrets
api.include_router(router_secrets)
api.include_router(router_secrets_secretid)
# pods
api.include_router(router_pods)
api.include_router(router_pods_podsid)
api.include_router(router_pods_podsid_func)
