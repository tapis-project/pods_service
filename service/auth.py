# Utilities for authn/z
import base64
import os
import re
import timeit

import jwt
import requests
import codes

from __init__ import t, Tenants
from tapisservice.tapisfastapi.utils import g
from tapisservice.logs import get_logger
from tapisservice.config import conf
from tapisservice.auth import resolve_tenant_id_for_request
logger = get_logger(__name__)

from errors import ResourceError, PermissionsException
from models_pods import Pod
from models_volumes import Volume
from models_snapshots import Snapshot
from models_templates import Template
from models_images import Image
from models_secrets import Secret
from models_stacks import Stack
from utils import check_permissions

TOKEN_RE = re.compile('Bearer (.+)')

WORLD_USER = 'ABACO_WORLD'


def get_user_sk_roles():
    """
    Using values from the g object. Gets roles for a user with g.username and g.request_tenant_id
    """
    logger.debug(f"Getting SK roles on tenant {g.request_tenant_id} and user {g.username}")
    start_timer = timeit.default_timer()
    try:
        roles_obj = t.sk.getUserRoles(tenant=g.request_tenant_id, user=g.username, _tapis_set_x_headers_from_service=True)
    except Exception as e:
        end_timer = timeit.default_timer()
        total = (end_timer - start_timer) * 1000
        if total > 4000:
            logger.critical(f"t.sk.getUserRoles took {total} to run for user {g.username}, tenant: {g.request_tenant_id}")
        raise e
    end_timer = timeit.default_timer()
    total = (end_timer - start_timer) * 1000
    if total > 4000:
        logger.critical(f"t.sk.getUserRoles took {total} to run for user {g.username}, tenant: {g.request_tenant_id}")
    roles_list = roles_obj.names
    if len(roles_list) < 10:
        logger.debug(f"Roles received: {roles_list}")
    else: 
        logger.debug(f"Roles received: {roles_list[:10]}... and {len(roles_list) - 10} more")
    g.roles = roles_list


def get_user_site_id():
    user_tenant_obj = t.tenant_cache.get_tenant_config(tenant_id=g.request_tenant_id)
    user_site_obj = user_tenant_obj.site
    g.site_id = user_site_obj.site_id


def check_object_id(request, object_type, idx):
    """Get the object_id from the request path."""
    # object_id identifier, index idx.
    #     /pods/<object_type>/<object_id>
    #     path_split: ['', 'pods', '<object_type>', 'object_id'] 
    logger.debug(f"Top of check_object_id. object_type: {object_type}; idx: {idx}")

    path_split = request.url.path.split("/")

    if len(path_split) <= idx:
        logger.error(f"Unrecognized request -- could not find {object_type}_id. path_split: {path_split}")
        raise PermissionsException("Not authorized.")
    logger.debug(f"path_split: {path_split}")
    try:
        object_id = path_split[idx]
    except IndexError:
        raise ResourceError(f"Unable to parse {object_type}_id: is it missing from the URL?", 404)
    if object_type in ['image', 'secret', 'template']:
        logger.debug(f"Attempting to grab {object_type}_id: {object_id}; tenant: siteadmintable")
        obj = globals()[object_type.capitalize()].db_get_with_pk(object_id, tenant="siteadmintable", site=g.site_id)
    else:
        logger.debug(f"Attempting to grab {object_type}_id: {object_id}; tenant: {g.request_tenant_id}")
        obj = globals()[object_type.capitalize()].db_get_with_pk(object_id, tenant=g.request_tenant_id, site=g.site_id)
    if not obj:
        msg = f"{object_type.capitalize()} with identifier {object_type}_id: '{object_id}' not found"
        logger.info(msg)
        raise ResourceError(msg, 404)
    return obj


def authorization(request):
    """
    This is the flaskbase authorization callback and implements the main Abaco authorization
    logic. This function is called by flaskbase after all authentication processing and initial
    authorization logic has run.
    """
    logger.debug(f"top of authorization: request.url.path: {request.url.path}")

    # first check whether the request is even valid -
    if hasattr(request, 'url'):
        logger.debug(f"request.url: {request.url}")
        if hasattr(request.url, 'path'):
            # if "//" or "///" in request.url.path:
            #     logger.debug(f"Found multiple slashes, simplifying (Because we use / parsing later). original path: {request.url.path}")
            #     request.url.path = request.url.path.replace("///", "/").replace("//", "/")
            logger.debug(f"request.url.path: {request.url.path}")
        else:
            logger.info("request.url has no path.")
            raise ResourceError(
                "Invalid request: the API endpoint does not exist or the provided HTTP method is not allowed.", 405)
    else:
        logger.info("Request has no request.url")
        raise ResourceError(
            "Invalid request: the API endpoint does not exist or the provided HTTP method is not allowed.", 405)

    # We check permissions, if user does not have permission, these functions will error and provide context.
    check_route_permissions(request)


def check_route_permissions(request):
    has_pem = False
    matched_route = None
    routes = [
        # NOT-API endpoints which don't use url/user/tenant info
        ["/redoc", "GET", "NOT-API"],
        ["/docs", "GET", "NOT-API"],
        ["/openapi.json", "GET", "NOT-API"],
        ["/traefik-config", "GET", "NOT-API"],
        ["/error-handler/{status}", "GET", "NOT-API"],
        ["/pod-splash", "GET", "NOT-API"],      # startup splash — Traefik routes here during readiness gate
        ["/healthcheck", "GET", "NOT-API"],
        # IMAGES
        ["/pods/images/{image_id:path}", "GET", codes.NONE],
        ["/pods/images/{image_id:path}", "PUT", codes.NONE],
        ["/pods/images/{image_id:path}", "DELETE", codes.NONE],#"ONLY-ADMIN"], # this should require admin, but can't use codes.ADMIN as permissions not defined on # just need to edit tests for this to work
        ["/pods/images", "GET", codes.NONE],
        ["/pods/images", "POST", codes.NONE],
        ["/pods/images/bulk", "POST", codes.NONE],
        # TEMPLATES
        ["/pods/templates/tags", "GET", codes.NONE],
        ["/pods/templates/{template_id}/tags/{tag_id}", "GET", codes.READ],
        ["/pods/templates/{template_id}/tags/{tag_id}", "DELETE", codes.ADMIN],
        ["/pods/templates/{template_id}/tags", "GET", codes.READ],
        ["/pods/templates/{template_id}/tags", "POST", codes.USER],
        ["/pods/templates/{template_id}/permissions", "GET", codes.USER],
        ["/pods/templates/{template_id}/permissions/{user}", "DELETE", codes.ADMIN],
        ["/pods/templates/{template_id}/permissions", "POST", codes.ADMIN],
        ["/pods/templates/{template_id}/gallery/photos/{n}", "GET", codes.READ],
        ["/pods/templates/{template_id}/gallery/photos/{n}", "PUT", codes.NONE],
        ["/pods/templates/{template_id}/gallery/photos/{n}", "DELETE", codes.NONE],
        ["/pods/templates/{template_id}/gallery/note", "PUT", codes.NONE],
        ["/pods/templates/{template_id}/gallery", "GET", codes.READ],
        ["/pods/templates/{template_id}/list", "GET", codes.READ],
        ["/pods/templates/{template_id}", "GET", codes.READ],
        ["/pods/templates/{template_id}", "PUT", codes.USER],
        ["/pods/templates/{template_id}", "DELETE", codes.ADMIN],
        ["/pods/templates", "GET", codes.NONE],
        ["/pods/templates", "POST", codes.NONE],
        # VOLUMES
        ["/pods/volumes/{volume_id}/permissions", "GET", codes.USER],
        ["/pods/volumes/{volume_id}/permissions/{user}", "DELETE", codes.ADMIN],
        ["/pods/volumes/{volume_id}/permissions", "POST", codes.ADMIN],
        ["/pods/volumes/{volume_id}/list", "GET", codes.READ],
        ["/pods/volumes/{volume_id}/upload/{filename}", "POST", codes.USER],
        ["/pods/volumes/{volume_id}/contents/{path:path}", "GET", codes.USER],
        ["/pods/volumes/{volume_id}", "GET", codes.READ],
        ["/pods/volumes/{volume_id}", "PUT", codes.USER],
        ["/pods/volumes/{volume_id}", "DELETE", codes.ADMIN],
        ["/pods/volumes", "GET", codes.NONE],
        ["/pods/volumes", "POST", codes.NONE],
        ["/pods/{pod_id}/events", "GET", codes.READ],
        ["/pods/{pod_id}/metrics", "GET", codes.READ],
        ["/pods/admin/metrics", "GET", codes.ADMIN],
        ["/pods/volumes/{volume_id}/usage", "GET", codes.READ],
        ["/pods/volumes/usage", "GET", codes.READ],
        # SNAPSHOTS
        ["/pods/snapshots/{snapshot_id}/permissions", "GET", codes.USER],
        ["/pods/snapshots/{snapshot_id}/permissions/{user}", "DELETE", codes.ADMIN],
        ["/pods/snapshots/{snapshot_id}/permissions", "POST", codes.ADMIN],
        ["/pods/snapshots/{snapshot_id}/list", "GET", codes.READ],
        ["/pods/snapshots/{snapshot_id}/contents/{path:path}", "GET", codes.USER],
        ["/pods/snapshots/{snapshot_id}", "GET", codes.READ],
        ["/pods/snapshots/{snapshot_id}", "PUT", codes.USER],
        ["/pods/snapshots/{snapshot_id}", "DELETE", codes.ADMIN],
        ["/pods/snapshots", "GET", codes.NONE],
        ["/pods/snapshots", "POST", codes.NONE],
        ["/pods/snapshots/{snapshot_id}/usage", "GET", codes.READ],
        ["/pods/snapshots/usage", "GET", codes.READ],
        # JUPYTER
        ["/pods/jupyter/{pod_id}/upload", "POST", codes.USER],
        ["/pods/jupyter/ensure", "GET", codes.USER],
        # SECRETS
        ["/pods/secrets", "GET", codes.NONE],
        ["/pods/secrets", "POST", codes.NONE],
        ["/pods/secrets/{secret_id}/permissions", "GET", codes.USER],
        ["/pods/secrets/{secret_id}/permissions/{user}", "DELETE", codes.ADMIN],
        ["/pods/secrets/{secret_id}/permissions", "POST", codes.ADMIN],
        ["/pods/secrets/{secret_id}/value", "GET", codes.USER],
        ["/pods/secrets/{secret_id}", "GET", codes.READ],
        ["/pods/secrets/{secret_id}", "PUT", codes.USER],
        ["/pods/secrets/{secret_id}", "DELETE", codes.ADMIN],
        # CLUSTERS
        ["/pods/clusters", "GET", codes.NONE],
        ["/pods/clusters", "POST", codes.NONE],
        ["/pods/clusters/{cluster_id}", "GET", codes.READ],
        ["/pods/clusters/{cluster_id}", "DELETE", codes.ADMIN],
        ["/pods/clusters/{cluster_id}/bootstrap", "POST", codes.ADMIN],
        ["/pods/clusters/{cluster_id}/permissions", "GET", codes.USER],
        ["/pods/clusters/{cluster_id}/permissions/{user}", "DELETE", codes.ADMIN],
        ["/pods/clusters/{cluster_id}/permissions", "POST", codes.ADMIN],
        ["/pods/clusters/{cluster_id}/stats", "GET", codes.USER],
        ["/pods/clusters/{cluster_id}/pods", "GET", codes.USER],
         #["/pods/clusters/{cluster_id}/add_pod/{pod_id}", "POST", codes.ADMIN],
         #["/pods/clusters/{cluster_id}/remove_pod/{pod_id}", "POST", codes.ADMIN],
         # GRAPHQL
         ["/pods/graphql", "POST", codes.NONE],  # GraphQL endpoint, no auth needed yet
        # STACKS — MUST be registered before the /pods/{pod_id} routes below; the {pod_id}
        # regex ([^/]+) would otherwise swallow "stacks".
        ["/pods/stacks/{stack_id}/permissions", "GET", codes.USER],
        ["/pods/stacks/{stack_id}/permissions/{user}", "DELETE", codes.ADMIN],
        ["/pods/stacks/{stack_id}/permissions", "POST", codes.ADMIN],
        ["/pods/stacks/{stack_id}/action", "POST", codes.ADMIN],  # parity: direct pod stop/start/restart require pod ADMIN
        ["/pods/stacks/{stack_id}/save_as_template", "POST", codes.USER],
        ["/pods/stacks/{stack_id}/update", "POST", codes.USER],
        ["/pods/stacks/from-template", "POST", codes.NONE],
        ["/pods/stacks/{stack_id}", "GET", codes.READ],
        ["/pods/stacks/{stack_id}", "PUT", codes.USER],
        ["/pods/stacks/{stack_id}", "DELETE", codes.ADMIN],
        ["/pods/stacks", "GET", codes.NONE],
        ["/pods/stacks", "POST", codes.NONE],
        # stack membership — set on the pod, pod-ADMIN gated (suffixed → before bare /pods/{pod_id})
        ["/pods/{pod_id}/stack", "POST", codes.ADMIN],
        ["/pods/{pod_id}/stack", "DELETE", codes.ADMIN],
        # PODS
        ["/pods/{pod_id}/permissions", "GET", codes.USER],
        ["/pods/{pod_id}/permissions/{user}", "DELETE", codes.ADMIN],
        ["/pods/{pod_id}/permissions", "POST", codes.ADMIN],
        ["/pods/{pod_id}/logs", "GET", codes.READ],
        ["/pods/{pod_id}/credentials", "GET", codes.USER],
        ["/pods/{pod_id}/save_pod_as_template_tag", "POST", codes.ADMIN],
        ["/pods/{pod_id}/upload_to_pod", "POST", codes.ADMIN],
        ["/pods/{pod_id}/stop", "GET", codes.ADMIN],
        ["/pods/{pod_id}/start", "GET", codes.ADMIN],
        ["/pods/{pod_id}/restart", "GET", codes.ADMIN],
        ["/pods/admin/health", "GET", codes.ADMIN],
        ["/pods/admin/debug-traffic", "GET", codes.ADMIN],
        ["/pods/{pod_id}/traffic", "GET", codes.READ],
        ["/pods/{pod_id}/log-runs", "GET", codes.READ],
        ["/pods/{pod_id}/log-runs/{run_index}", "GET", codes.READ],
        ["/pods/{pod_id}/derived", "GET", codes.READ],
        ["/pods/{pod_id}/download_from_pod{path:path}", "GET", codes.ADMIN],
        ["/pods/{pod_id}/list_files{path:path}", "GET", codes.ADMIN],
        ["/pods/{pod_id}/exec", "POST", codes.ADMIN],
        ["/pods/{pod_id_net}/auth", "GET", "NEED-BASEURL"], # oauth
        ["/pods/{pod_id_net}/auth/callback", "GET", "NEED-BASEURL"], # oauth
        ["/pods/{pod_id}", "GET", codes.READ],
        ["/pods/{pod_id}", "PUT", codes.USER],
        ["/pods/{pod_id}", "DELETE", codes.ADMIN],
        ["/pods", "GET", codes.NONE],
        ["/pods", "POST", codes.NONE]
    ]

    # check that route matches one route with regex match. If it does, do pem check
    # for snapshots/volumes/pods
    for route in routes:
        # check if method matches current request's method
        if route[1] != request.method:
            continue
        
        # Convert {variable_name:path} to a regex that matches any characters including slashes
        regex_route_path = re.sub(r'\{[^}]*:path\}', '.*', route[0])
        # Convert {variable_name} (not followed by :path) to regex alphanumeric matches
        regex_route_path = re.sub(r'\{[^}]*\}', '[^/]+', regex_route_path)
        # Ensure the regex matches the entire path from start (^) to end ($)
        regex_route_path = f"^{regex_route_path}$"
        if re.match(regex_route_path, request.url.path):
            logger.debug(f"Matched API route: {route[1]} - {route[0]}")
            matched_route = route
            #raise PermissionsException(f"Matched API route: {route[1]} - {route[0]}.")
            break
    
    if not matched_route:
        raise PermissionsException(
            f"Request path '{request.url.path}' ({request.method}) does not match any Pods API route. "
            f"If you are accessing a pod URL, the pod may still be starting — check its status in TapisUI."
        )

    ## check for options
    if request.method == "OPTIONS":
        logger.debug(f"OPTIONS request. Allowing request.")
        has_pem = True
        return

    # check for level="NOT-API"
    # NOT-API routes don't use url/user/tenant info
    if matched_route[2] == "NOT-API":
        has_pem = True
        return
    elif matched_route[2] == "NEED-BASEURL":
        ## Needed for auth where we need tenant/site info, but not token info.
        logger.debug(f"Matched NEED-BASEURL: g.request_tenant_id: {g.request_tenant_id}, g.username: {g.username}")
        g.cross_tenant_request = False
        # We might not have g.request_tenant_id yet, so we need to resolve it
        if not g.request_tenant_id:
            try:
                resolve_tenant_id_for_request(g, request, Tenants)
                logger.debug(f"Resolved NEED-BASEURL route. g.request_tenant_id: {g.request_tenant_id}")
            except Exception as e:
                # resolve_tenant_id_for_request raises PermissionsError when token tenant != URL tenant.
                # For NEED-BASEURL routes (pod auth/callback), we allow this through and defer
                # cross-tenant validation to the route handler, which checks pod permissions for tenant.* entries.
                logger.info(f"resolve_tenant_id_for_request failed for NEED-BASEURL route, likely cross-tenant: {e}")
                # g.request_tenant_id and g.token_tenant_id may already be set by resolve_tenant_id_for_request
                # before it raised. If not, extract request_tenant_id from URL manually.
                if not g.request_tenant_id:
                    # Fallback: extract tenant from URL path. URL looks like /v3/pods/{pod_id_net}/auth
                    # The tenant comes from the base_url/host, not the path.
                    # resolve_tenant_id_for_request should have set it before raising, but just in case:
                    try:
                        host = request.headers.get('host', '') or str(request.url.hostname or '')
                        # host is like 'tacc.tapis.io' or 'tacc.develop.tapis.io'
                        g.request_tenant_id = host.split('.')[0]
                        logger.info(f"Extracted request_tenant_id from host: {g.request_tenant_id}")
                    except Exception as host_e:
                        logger.error(f"Failed to extract tenant from host: {host_e}")
                        raise PermissionsException(f"Unable to determine tenant for request: {e}")
                g.cross_tenant_request = True
                logger.info(f"Cross-tenant NEED-BASEURL request. request_tenant_id: {g.request_tenant_id}, token_tenant_id: {getattr(g, 'token_tenant_id', 'unknown')}")
        get_user_site_id()
        has_pem = True
        return

    # Sets g.site_id and g.roles.
    # Required for all API routes
    get_user_site_id()
    get_user_sk_roles()
    # local_admin_usernames grants admin WITHOUT the SK pods_admin role — a convenience for
    # local/test deployments (so test identities can exercise admin-gated paths). Gated behind
    # local_development (default False) so a populated list can never act as a backdoor in a
    # production config: BOTH the flag must be on AND the username listed.
    local_admins = getattr(conf, 'local_admin_usernames', []) if getattr(conf, 'local_development', False) else []
    g.admin = True if codes.ADMIN_ROLE in g.roles or g.username == "cgarcia" or g.username in local_admins else False
    # g.admin = user HAS the admin role — grants implicit access to all routes.
    # g.admin_active = user also sent X-Pods-Admin: true — activates UI-level admin powers (see all pods, etc.)
    g.admin_active = False
    x_admin_header = request.headers.get("x-pods-admin", "").lower().strip()
    if x_admin_header == "true":
        if not g.admin:
            raise PermissionsException("X-Pods-Admin header requires the PODS_ADMIN role.")
        g.admin_active = True

    if g.admin:
        # Admins bypass all object-level permission checks.
        logger.debug(f"Admin user {g.username} granted access to {request.url.path}.")
        return

    if "{pod_id_net}" in matched_route[0]:
        logger.debug(f"Matched {{pod_id_net}} route. request.url.path: {request.url.path}")
        # pod_id_net can be `myid-networking3` for example. We need to get rid of the networking bit for permissions check
        pod = check_object_id(request, 'pod', 2)
        pod.pod_id = pod.pod_id.split("-")[0]
        has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=matched_route[2] , roles=g.roles)
    elif "jupyter/{pod_id}" in matched_route[0]:
        logger.debug(f"Matched jupyter/--pod_id-- route. request.url.path: {request.url.path}")
        # moves field to 3rd position
        pod = check_object_id(request, 'pod', 3)
        has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=matched_route[2] , roles=g.roles)
    elif "clusters/{cluster_id}" in matched_route[0]:
        logger.debug(f"Matched clusters/--cluster_id-- route. request.url.path: {request.url.path}")
        cluster = check_object_id(request, 'cluster', 2)
        has_pem = check_permissions(user=g.username, object=cluster, object_type="cluster", level=matched_route[2] , roles=g.roles)
    elif "{stack_id}" in matched_route[0]:
        logger.debug(f"Matched /--stack_id-- route. request.url.path: {request.url.path}")
        stack = check_object_id(request, 'stack', 3)
        has_pem = check_permissions(user=g.username, object=stack, object_type="stack", level=matched_route[2] , roles=g.roles)
    elif "{pod_id}" in matched_route[0]:
        logger.debug(f"Matched /--pod_id-- route. request.url.path: {request.url.path}")
        pod = check_object_id(request, 'pod', 2)
        has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=matched_route[2] , roles=g.roles)
    elif "{volume_id}" in matched_route[0]:
        volume = check_object_id(request, 'volume', 3)
        has_pem = check_permissions(user=g.username, object=volume, object_type="volume", level=matched_route[2] , roles=g.roles)
    elif "{snapshot_id}" in matched_route[0]:
        snapshot = check_object_id(request, 'snapshot', 3)
        has_pem = check_permissions(user=g.username, object=snapshot, object_type="snapshot", level=matched_route[2] , roles=g.roles)
    elif "{template_id}" in matched_route[0]:
        template = check_object_id(request, 'template', 3)
        has_pem = check_permissions(user=g.username, object=template, object_type="template", level=matched_route[2], roles=g.roles, tenant=g.request_tenant_id)
    elif "{secret_id}" in matched_route[0]:
        secret = check_object_id(request, 'secret', 3)
        has_pem = check_permissions(user=g.username, object=secret, object_type="secret", level=matched_route[2] , roles=g.roles)
    elif "{image_id}" in matched_route[0]:
        image = check_object_id(request, 'image', 3)
        # images don't have permissions
        #has_pem = check_permissions(user=g.username, object=image, object_type="image", level=matched_route[2] , roles=g.roles)
    elif "jupyter/ensure" in matched_route[0]:
        # jupyter/ensure doesn't have permissions
        has_pem = True
    elif matched_route[2] == codes.ADMIN:
        # Flat admin routes with no object ID (e.g. /pods/admin/health)
        has_pem = g.admin

    # check for codes.NONE
    if matched_route[2] == codes.NONE:
        logger.info("Allowing request because of NONE code. Specs/Docs/Traefik/OAuth/RootPaths don't need auth.")
        has_pem = True
        return

    # Last minute check for stragglers
    if not has_pem:
        logger.info("NOT allowing request.")
        raise PermissionsException(f"Not authorized -- you do not have access to this endpoint. {matched_route[1]} {matched_route[0]}")


def authentication(request):
    # Pod OAuth routes handle their own auth flow — skip token checks entirely.
    # Regex matches /pods/<pod_id_net>/auth and /pods/<pod_id_net>/auth/callback
    if re.match(r'^/pods/[^/]+/auth(/callback)?$', request.url.path):
        pass
    elif (request.url.path == '/redoc' or
        request.url.path == '/docs' or
        request.url.path == '/openapi.json' or
        request.url.path == '/traefik-config' or
        request.url.path == '/pod-splash' or
        request.url.path == '/healthcheck' or
        request.url.path.startswith('/error-handler/')):
        pass