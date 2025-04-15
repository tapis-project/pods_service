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
    logger.debug(f"Roles received: {roles_list}")
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
    if object_type in ['image']:
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
        # IMAGES
        ["/pods/images/{image_id:path}", "GET", codes.NONE],
        ["/pods/images/{image_id}", "DELETE", codes.NONE],#"ONLY-ADMIN"], # this should require admin, but can't use codes.ADMIN as permissions not defined on # just need to edit tests for this to work
        ["/pods/images", "GET", codes.NONE],
        ["/pods/images", "POST", codes.NONE],
        ["/pods/images/bulk", "POST", codes.NONE],
        # TEMPLATES
        ["/pods/templates/tags", "GET", codes.NONE],
        ["/pods/templates/{template_id}/tags/{tag_id}", "GET", codes.READ],
        ["/pods/templates/{template_id}/tags", "GET", codes.READ],
        ["/pods/templates/{template_id}/tags", "POST", codes.USER],
        ["/pods/templates/{template_id}/permissions", "GET", codes.USER],
        ["/pods/templates/{template_id}/permissions/{user}", "DELETE", codes.ADMIN],
        ["/pods/templates/{template_id}/permissions", "POST", codes.ADMIN],
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
        # PODS
        ["/pods/{pod_id}/permissions", "GET", codes.USER],
        ["/pods/{pod_id}/permissions/{user}", "DELETE", codes.ADMIN],
        ["/pods/{pod_id}/permissions", "POST", codes.ADMIN],
        ["/pods/{pod_id}/logs", "GET", codes.READ],
        ["/pods/{pod_id}/credentials", "GET", codes.USER],
        ["/pods/{pod_id}/save_pod_as_template_tag", "POST", codes.ADMIN],
        ["/pods/{pod_id}/stop", "GET", codes.ADMIN],
        ["/pods/{pod_id}/start", "GET", codes.ADMIN],
        ["/pods/{pod_id}/restart", "GET", codes.ADMIN],
        ["/pods/{pod_id}/derived", "GET", codes.READ],
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
        raise PermissionsException(f"Could not match request to an API route.")

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
        # We might not have g.request_tenant_id yet, so we need to resolve it
        if not g.request_tenant_id:
            resolve_tenant_id_for_request(g, request, Tenants)
            logger.debug(f"Resolved NEED-BASEURL route. g.request_tenant_id: {g.request_tenant_id}")
        get_user_site_id()
        has_pem = True
        return

    # Sets g.site_id and g.roles.
    # Required for all API routes
    get_user_site_id()
    get_user_sk_roles()
    g.admin = True if "PODS_ADMIN" in g.roles else False
    if g.admin:
        has_pem = True

    if "{pod_id_net}" in matched_route[0]:
        # pod_id_net can be `myid-networking3` for example. We need to get rid of the networking bit for permissions check
        pod = check_object_id(request, 'pod', 2)
        pod.pod_id = pod.pod_id.split("-")[0]
        has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=matched_route[2] , roles=g.roles)
    elif "{pod_id}" in matched_route[0]:
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
        has_pem = check_permissions(user=g.username, object=template, object_type="template", level=matched_route[2] , roles=g.roles)
    elif "{image_id}" in matched_route[0]:
        image = check_object_id(request, 'image', 3)
        # images don't have permissions
        #has_pem = check_permissions(user=g.username, object=image, object_type="image", level=matched_route[2] , roles=g.roles)

    # check for codes.NONE
    if matched_route[2] == codes.NONE:
        logger.info("Allowing request because of NONE code. Specs/Docs/Traefik/OAuth/RootPaths don't need auth.")
        has_pem = True
        return

    # Last minute check for stragglers
    if not has_pem:
        logger.info("NOT allowing request.")
        raise PermissionsException(f"Not authorized -- you do not have access to this endpoint. {matched_route[1]}-{matched_route[0]}")


def authentication(request):
    if (request.url.path == '/redoc' or
        request.url.path == '/docs' or
        request.url.path == '/openapi.json' or
        request.url.path == '/traefik-config' or
        request.url.path.startswith('/error-handler/')):
        pass