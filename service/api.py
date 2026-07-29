from utils import error_handler, HttpUrlRedirectMiddleware
from tapisservice.tapisfastapi.utils import GlobalsMiddleware
from tapisservice.tapisfastapi.auth import TapisMiddleware
from tapisservice.config import conf

from starlette.requests import Request as _Request
from starlette.types import ASGIApp as _ASGIApp, Receive as _Receive, Scope as _Scope, Send as _Send
from tapisservice.tapisfastapi.auth import FormattedRequest as _FormattedRequest

# Which routes skip token validation is defined ONCE in auth.py (NO_TOKEN_ROUTES /
# AUTHN_EXEMPT_STATIC), consumed here via request_skips_token_auth (imported below).

class PodsTapisMiddleware(TapisMiddleware):
    """TapisMiddleware wrapper that skips token authentication for exempt routes.

    Exempt routes (auth.request_skips_token_auth): pod OAuth browser flows, pod
    access-gate visitor flows, node agent endpoints (claim/agent-token auth in-handler),
    and static utility paths (healthcheck/docs/...). Letting TapisMiddleware's
    core_validate_request_token run on these can 401 callers that legitimately carry
    no Tapis token — or worse, carry a stale token cookie, which raises
    AuthenticationError instead of NoTokenError.

    We skip only the authentication step (token validation) but still run the authorization
    callback, which enforces the route allowlist and handles NEED-BASEURL tenant resolution
    (setting g.request_tenant_id and g.site_id).
    """
    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["type"] == "http":
            request = _Request(scope, receive)
            if request_skips_token_auth(request.url.path, request.method):
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

from auth import authorization, authentication, request_skips_token_auth
from api_admin import router as router_admin
from api_pods import router as router_pods
from api_pods_podid import router as router_pods_podsid
from api_pods_podid_func import router as router_pods_podsid_func
from api_pods_podid_jupyter import router as router_pods_podsid_jupyter
from api_stacks import router as router_stacks
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
from api_templates_templateid_gallery import router as router_templates_templateid_gallery
from api_images import router as router_images
from api_images_imageid import router as router_images_imageid
from api_secrets import router as router_secrets
from api_secrets_secretid import router as router_secrets_secretid
from api_secrets_secretid_func import router as router_secrets_secretid_func
from api_nodes import router as router_nodes
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
api.include_router(router_admin)
# templates
api.include_router(router_templates)
api.include_router(router_templates_templateid)
api.include_router(router_templates_templateid_tags)
api.include_router(router_templates_templateid_func)
api.include_router(router_templates_templateid_tags_tagid)
api.include_router(router_templates_templateid_gallery)
# images
api.include_router(router_images)
api.include_router(router_images_imageid)
# snapshots — func router MUST register before the /{snapshot_id} router: it
# holds the literal /pods/snapshots/usage route, which /{snapshot_id} would
# otherwise swallow (snapshot_id="usage" → None → 500)
api.include_router(router_snapshots)
api.include_router(router_snapshots_snapshotid_func)
api.include_router(router_snapshots_snapshotid)
# volumes — same ordering requirement for /pods/volumes/usage
api.include_router(router_volumes)
api.include_router(router_volumes_volumeid_func)
api.include_router(router_volumes_volumeid)
# jupyter
api.include_router(router_pods_podsid_jupyter)
# nodes
api.include_router(router_nodes)
# secrets
api.include_router(router_secrets)
api.include_router(router_secrets_secretid)
# pods
api.include_router(router_pods)
# stacks MUST be registered before router_pods_podsid — /pods/stacks would otherwise match /pods/{pod_id}
api.include_router(router_stacks)
api.include_router(router_pods_podsid)
api.include_router(router_pods_podsid_func)
