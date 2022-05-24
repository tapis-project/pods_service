# Utilities for authn/z
import base64
import os
import re
import timeit

import jwt
import requests
from tapisservice.tapisfastapi.utils import g
import codes

from __init__ import t, Tenants
from tapisservice.tapisfastapi.auth import authn_and_authz
from tapisservice.logs import get_logger
logger = get_logger(__name__)
from errors import ResourceError, PermissionsException
from models import Pod

from tapisservice.config import conf


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


def check_pod_id(request):
    """Get the db_id and actor_identifier from the request path."""
    # pods_id identifier, index 2.
    #     /pods/<pod_id>
    logger.debug(f"Top of check_pod_id.")

    idx = 2
    path_split = request.url.path.split("/")

    if len(path_split) < 3:
        logger.error(f"Unrecognized request -- could not find the pod_id. path_split: {path_split}")
        raise PermissionsException("Not authorized.")
    logger.debug(f"path_split: {path_split}")
    try:
        pod_id = path_split[idx]
    except IndexError:
        raise ResourceError("Unable to parse actor identifier: is it missing from the URL?", 404)
    logger.debug(f"pod_id: {pod_id}; tenant: {g.request_tenant_id}")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    if not pod:
        msg = f"Pod with identifier '{pod_id}' not found"
        logger.info(msg)
        raise ResourceError(msg, 404)
    return pod


def check_permissions(user, pod, level, roles=None):
    """Check the permissions store for user and level. Here, `identifier` is a unique id in the
    permissions_store; e.g., actor db_id or alias_id.
    """
    logger.debug(f"Checking pod_id: {pod.pod_id} permissions for user {user}")
    # first, if roles were passed, check for admin role
    if roles:
        if codes.ADMIN_ROLE in roles:
            return True

    # Get all permissions for this pod
    permissions = pod.get_permissions()
    # Attempt to get permission level for particular user.
    user_level = permissions.get(user)
    if not user_level:
        logger.info(f"Found no permissions for user {user} on pod {pod.pod_id}. Permissions: {permissions}")
        return False

    # Get user pem and compare to level.
    user_pem = codes.PermissionLevel(user_level)
    if user_pem >= level:
        logger.info(f"Allowing request - user has appropriate permission for pod: {pod.pod_id}.")
        return True
    else:
        # we found the permission for the user but it was insufficient; return False right away
        logger.info(f"Found permission {level} for pod: {pod.pod_id}, insufficient permission, rejecting request.")
        return False


def authorization(request):
    """
    This is the flaskbase authorization callback and implements the main Abaco authorization
    logic. This function is called by flaskbase after all authentication processing and initial
    authorization logic has run.
    """
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

    # get the pod pod_id from a possible identifier once and for all -
    # these routes do not have an pod pod_id in them:
    # if request.url.path == '/docs':
    #     return True
    if (request.url.path == '/redoc' or
        request.url.path == '/docs' or
        request.url.path == '/openapi.json'):
        logger.debug(f"Spec and Docs doesn't need auth. Skipping. url.path: {request.url.path}")
        return
    elif (request.url.path == '/pods' or 
          request.url.path == '/pods/' or
          request.url.path == '/docs'):
        logger.debug(f"Don't need to run check_pod_id(), no pod_id in url.path: {request.url.path}")
        pass
    else:
        # every other route should have an pod identifier. Check if it's real.
        logger.debug(f"fetching pod_id; rule: {request.url.path}")
        pod = check_pod_id(request)

    ### Fill in g object
    # Generally request.base_url returns `https://tapis.io`
    g.api_server = request.base_url.replace('http://', 'https://')
    g.admin = False
    # Set g.site_id and g.roles
    get_user_site_id()
    get_user_sk_roles()

    ### Set admin
    # if codes.ADMIN_ROLE in g.roles:
    #     g.admin = True
    #     logger.info("Allowing request because of ADMIN_ROLE.")
    #     return True

    # if request.method == 'OPTIONS':
    #     # allow all users to make OPTIONS requests
    #     logger.info("Allowing request because of OPTIONS method.")
    #     return True

    logger.debug(f"request.url.path: {request.url.path}")

    # # the utilization endpoint is available to every authenticated user
    # if '/actors/utilization' == request.url_rule.rule or '/actors/utilization/' == request.url_rule.rule:
    #     return True

    #### Do checks for pods read/user/admin roles. Add in "required_roles" attr when neccessary in api.
    # there are special rules on the pods root collection:
    if '/pods' == request.url.path or '/pods/' == request.url.path:
        logger.debug("Checking permissions on root collection.")
        # Only ADMIN can set privileged and some attrs. Check for that here.
        # if request.method == 'POST':
        #     check_privileged()

        # if we are here, it is either a GET or a new actor, so the request is allowed:
        logger.debug("new actor or GET on root connection. allowing request.")
        return True


    has_pem = False

    path_split = request.url.path.split("/")
    if len(path_split) < 3:
        logger.error(f"Unrecognized request -- could not find the pod_id. path_split: {path_split}")
        raise PermissionsException("Not authorized.")

    # Anything here should wind up as /pods/pod_id/func/. ["", "pods", "pod_id", "func", ""]
    if not path_split[1] == "pods":
        raise PermissionsException(f"URL should start with /pods, got {request.url.path}")

    if len(path_split) > 3:
        # Check for func = permissions
        if path_split[3] == "permissions":
            if request.method == 'GET':
                # GET permissions requires USER
                has_pem = check_permissions(user=g.username, pod=pod, level=codes.USER)
            elif request.method == 'DELETE':
                # DELETE permissions requires ADMIN
                has_pem = check_permissions(user=g.username, pod=pod, level=codes.ADMIN)
            elif request.method == 'POST':
                # POST permissions requires ADMIN
                has_pem = check_permissions(user=g.username, pod=pod, level=codes.ADMIN)
        # Check for func = logs
        if path_split[3] == "logs":
            if request.method == 'GET':
                # GET logs requires READ
                has_pem = check_permissions(user=g.username, pod=pod, level=codes.READ)
        # Check for func = credentials
        if path_split[3] == "credentials":
            if request.method == 'GET':
                # GET creds requires USER
                has_pem = check_permissions(user=g.username, pod=pod, level=codes.USER)
    else:
        # Now just /pods/{pod_id}
        if request.method == 'GET':
            # GET pod require READ
            has_pem = check_permissions(user=g.username, pod=pod, level=codes.READ)
        elif request.method == 'PUT':
            # PUT pod require USER
            has_pem = check_permissions(user=g.username, pod=pod, level=codes.USER)
        elif request.method == 'DELETE':
            # DELETE pod require ADMIN
            has_pem = check_permissions(user=g.username, pod=pod, level=codes.ADMIN)
        # else:
        #     logger.debug(f"URL rule in request: {request.url_rule.rule}")
        #     # first, only admins can create/update actors to be privileged, so check that:
        #     if request.method == 'POST' or request.method == 'PUT':
        #         check_privileged()

    if not has_pem:
        logger.info("NOT allowing request.")
        raise PermissionsException(f"Not authorized -- you do not have access to this pod endpoint.")


def authentication(request):
    if (request.url.path == '/redoc' or
        request.url.path == '/docs' or 
        request.url.path == '/openapi.json'):
        pass