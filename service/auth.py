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
from tapisservice.logs import get_logger
from tapisservice.config import conf
logger = get_logger(__name__)

from errors import ResourceError, PermissionsException
from models_pods import Pod
from models_volumes import Volume
from models_snapshots import Snapshot
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


def check_pod_id(request):
    """Get pod_id from the request path."""
    # pods_id identifier, index 2.
    #     /pods/<pod_id>
    #     path_split: ['', 'pods', 'pod_id'] 
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
        raise ResourceError("Unable to parse pods_id: is it missing from the URL?", 404)
    logger.debug(f"pod_id: {pod_id}; tenant: {g.request_tenant_id}")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    if not pod:
        msg = f"Pod with identifier '{pod_id}' not found"
        logger.info(msg)
        raise ResourceError(msg, 404)
    return pod

def check_volume_id(request):
    """Get the volume_id from the request path."""
    # volumes_id identifier, index 2.
    #     /pods/volumes/<volume_id>
    #     path_split: ['', 'pods', 'volumes', 'volume_id'] 
    logger.debug(f"Top of check_volume_id.")

    idx = 3
    path_split = request.url.path.split("/")

    if len(path_split) < 4:
        logger.error(f"Unrecognized request -- could not find the volume_id. path_split: {path_split}")
        raise PermissionsException("Not authorized.")
    logger.debug(f"path_split: {path_split}")
    try:
        volume_id = path_split[idx]
    except IndexError:
        raise ResourceError("Unable to parse volume_id: is it missing from the URL?", 404)
    logger.debug(f"volume_id: {volume_id}; tenant: {g.request_tenant_id}")
    volume = Volume.db_get_with_pk(volume_id, tenant=g.request_tenant_id, site=g.site_id)
    if not volume:
        msg = f"Volume with identifier '{volume_id}' not found"
        logger.info(msg)
        raise ResourceError(msg, 404)
    return volume

def check_snapshot_id(request):
    """Get the snapshot_id from the request path."""
    # snapshot_id identifier, index 2.
    #     /pods/snapshots/<snapshot_id>
    #     path_split: ['', 'pods', 'snapshots', 'snapshot_id'] 
    logger.debug(f"Top of check_snapshot_id.")

    idx = 3
    path_split = request.url.path.split("/")

    if len(path_split) < 4:
        logger.error(f"Unrecognized request -- could not find the snapshot_id. path_split: {path_split}")
        raise PermissionsException("Not authorized.")
    logger.debug(f"path_split: {path_split}")
    try:
        snapshot_id = path_split[idx]
    except IndexError:
        raise ResourceError("Unable to parse snapshot_id: is it missing from the URL?", 404)
    logger.debug(f"snapshot_id: {snapshot_id}; tenant: {g.request_tenant_id}")
    snapshot = Snapshot.db_get_with_pk(snapshot_id, tenant=g.request_tenant_id, site=g.site_id)
    if not snapshot:
        msg = f"Snapshot with identifier '{snapshot_id}' not found"
        logger.info(msg)
        raise ResourceError(msg, 404)
    return snapshot

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
    pod = None
    volume = None
    snapshot = None
    if (request.url.path == '/redoc' or
        request.url.path == '/docs' or
        request.url.path == '/openapi.json' or
        request.url.path == '/traefik-config' or
        request.url.path.startswith('/error-handler/')):
        logger.debug(f"Spec, Docs, Traefik conf doesn't need auth. Skipping. url.path: {request.url.path}")
        return
    elif (request.url.path == '/pods' or 
          request.url.path == '/pods/volumes' or
          request.url.path == '/pods/snapshots' or
          request.url.path == '/docs'):
        logger.debug(f"Don't need to run check_pod_id(), no pod_id in url.path: {request.url.path}")
        pass
    elif request.url.path.startswith("/pods/volumes"):
        logger.debug(f"Found route which should have volume_id: {request.url.path}")
        volume = check_volume_id(request)
    elif request.url.path.startswith("/pods/snapshots"):
        logger.debug(f"Found route which should have snapshot_id: {request.url.path}")
        snapshot = check_snapshot_id(request)
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

    if request.method == 'OPTIONS':
        # allow all users to make OPTIONS requests
        logger.info("Allowing request because of OPTIONS method.")
        return True

    logger.debug(f"request.url.path: {request.url.path}")

    #### Do checks for pods read/user/admin roles. Add in "required_roles" attr when neccessary in api.
    # there are special rules on the pods root collection:
    if '/pods' == request.url.path or '/pods/volumes' == request.url.path or '/pods/snapshots' == request.url.path:
        logger.debug("Checking permissions on root collection.")
        # Only ADMIN can set privileged and some attrs. Check for that here.
        # if request.method == 'POST':
        #     check_privileged()

        # if we are here, it is either a GET or a new object, so the request is allowed:
        logger.debug("new object or GET on root connection. allowing request.")
        return True

    # We check permissions, if user does not have permission, these functions will error and provide context.
    if pod:
        check_pod_permission(pod, request)
    elif volume:
        check_volume_permission(volume, request)
    if snapshot:
        check_snapshot_permission(snapshot, request)

def check_pod_permission(pod, request):
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

# GET logs requires READ
# GET pod require READ

# GET permissions requires USER
# PUT pod require USER
# GET creds requires USER

# DELETE permissions requires ADMIN
# POST permissions requires ADMIN
# DELETE pod require ADMIN
# GET restart requires ADMIN
# GET stop requires ADMIN
# GET start requires ADMIN

                # GET permissions requires USER
                has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.USER, roles=g.roles)
            elif request.method == 'DELETE':
                # DELETE permissions requires ADMIN
                has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.ADMIN, roles=g.roles)
            elif request.method == 'POST':
                # POST permissions requires ADMIN
                has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.ADMIN, roles=g.roles)
        # Check for func = logs
        if path_split[3] == "logs":
            if request.method == 'GET':
                # GET logs requires READ
                has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.READ, roles=g.roles)
        # Check for func = credentials
        if path_split[3] == "credentials":
            if request.method == 'GET':
                # GET creds requires USER
                has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.USER, roles=g.roles)
        # Check for func = stop
        if path_split[3] == "stop":
            if request.method == 'GET':
                # GET stop requires ADMIN
                has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.ADMIN, roles=g.roles)
        # Check for func = start
        if path_split[3] == "start":
            if request.method == 'GET':
                # GET start requires ADMIN
                has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.ADMIN, roles=g.roles)
        # Check for func = restart
        if path_split[3] == "restart":
            if request.method == 'GET':
                # GET restart requires ADMIN
                has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.ADMIN, roles=g.roles)
    else:
        # Now just /pods/{pod_id}
        if request.method == 'GET':
            # GET pod require READ
            has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.READ, roles=g.roles)
        elif request.method == 'PUT':
            # PUT pod require USER
            has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.USER, roles=g.roles)
        elif request.method == 'DELETE':
            # DELETE pod require ADMIN
            has_pem = check_permissions(user=g.username, object=pod, object_type="pod", level=codes.ADMIN, roles=g.roles)
        # else:
        #     logger.debug(f"URL rule in request: {request.url_rule.rule}")
        #     # first, only admins can create/update actors to be privileged, so check that:
        #     if request.method == 'POST' or request.method == 'PUT':
        #         check_privileged()

    if not has_pem:
        logger.info("NOT allowing request.")
        raise PermissionsException(f"Not authorized -- you do not have access to this pod endpoint.")


def check_volume_permission(volume, request):
    has_pem = False

    path_split = request.url.path.split("/")
    if len(path_split) < 4:
        logger.error(f"Unrecognized request -- could not find the volume_id. path_split: {path_split}")
        raise PermissionsException("Not authorized.")

    # Anything here should wind up as /pods/volumes/volume_id/func/. ["", "pods", "volumes", "volume_id", "func", ""]
    if not path_split[1] == "pods":
        raise PermissionsException(f"URL should start with /pods, got {request.url.path}")

    if not path_split[2] == "volumes":
        raise PermissionsException(f"URL should start with /pods/volumes, got {request.url.path}")

    if len(path_split) > 4:
        # Check for func = permissions
        if path_split[4] == "permissions":
            if request.method == 'GET':
                # GET permissions requires USER
                has_pem = check_permissions(user=g.username, object=volume, object_type="volume", level=codes.USER, roles=g.roles)
            elif request.method == 'DELETE':
                # DELETE permissions requires ADMIN
                has_pem = check_permissions(user=g.username, object=volume, object_type="volume", level=codes.ADMIN, roles=g.roles)
            elif request.method == 'POST':
                # POST permissions requires ADMIN
                has_pem = check_permissions(user=g.username, object=volume, object_type="volume", level=codes.ADMIN, roles=g.roles)
        # Check for func = list
        if path_split[4] == "list":
            if request.method == 'GET':
                # GET list requires READ
                has_pem = check_permissions(user=g.username, object=volume, object_type="volume", level=codes.READ, roles=g.roles)
        # Check for func = upload
        if path_split[4] == "upload":
            if request.method == 'POST':
                # POST upload requires USER
                has_pem = check_permissions(user=g.username, object=volume, object_type="volume", level=codes.USER, roles=g.roles)

    else:
        # Now just /pods/volumes/{volume_id}
        if request.method == 'GET':
            # GET volume requires READ
            has_pem = check_permissions(user=g.username, object=volume, object_type="volume", level=codes.READ, roles=g.roles)
        elif request.method == 'PUT':
            # PUT volume requires USER
            has_pem = check_permissions(user=g.username, object=volume, object_type="volume", level=codes.USER, roles=g.roles)
        elif request.method == 'DELETE':
            # DELETE volume requires ADMIN
            has_pem = check_permissions(user=g.username, object=volume, object_type="volume", level=codes.ADMIN, roles=g.roles)
        # else:
        #     logger.debug(f"URL rule in request: {request.url_rule.rule}")
        #     # first, only admins can create/update actors to be privileged, so check that:
        #     if request.method == 'POST' or request.method == 'PUT':
        #         check_privileged()

    if not has_pem:
        logger.info("NOT allowing request.")
        raise PermissionsException(f"Not authorized -- you do not have access to this volume endpoint.")


def check_snapshot_permission(snapshot, request):
    has_pem = False

    path_split = request.url.path.split("/")
    if len(path_split) < 4:
        logger.error(f"Unrecognized request -- could not find the snapshot_id. path_split: {path_split}")
        raise PermissionsException("Not authorized.")

    # Anything here should wind up as /pods/snapshots/snapshot_id/func/. ["", "pods", "snapshots", "snapshot_id", "func", ""]
    if not path_split[1] == "pods":
        raise PermissionsException(f"URL should start with /pods, got {request.url.path}")

    if not path_split[2] == "snapshots":
        raise PermissionsException(f"URL should start with /pods/snapshots, got {request.url.path}")

    if len(path_split) > 4:
        # Check for func = permissions
        if path_split[4] == "permissions":
            if request.method == 'GET':
                # GET permissions requires USER
                has_pem = check_permissions(user=g.username, object=snapshot, object_type="snapshot", level=codes.USER, roles=g.roles)
            elif request.method == 'DELETE':
                # DELETE permissions requires ADMIN
                has_pem = check_permissions(user=g.username, object=snapshot, object_type="snapshot", level=codes.ADMIN, roles=g.roles)
            elif request.method == 'POST':
                # POST permissions requires ADMIN
                has_pem = check_permissions(user=g.username, object=snapshot, object_type="snapshot", level=codes.ADMIN, roles=g.roles)
        # Check for func = list
        if path_split[4] == "list":
            if request.method == 'GET':
                # GET list requires READ
                has_pem = check_permissions(user=g.username, object=snapshot, object_type="snapshot", level=codes.READ, roles=g.roles)
        # Check for func = upload
        if path_split[4] == "upload":
            if request.method == 'POST':
                # POST upload requires USER
                has_pem = check_permissions(user=g.username, object=snapshot, object_type="snapshot", level=codes.USER, roles=g.roles)

    else:
        # Now just /pods/snapshots/{snapshot_id}
        if request.method == 'GET':
            # GET snapshot requires READ
            has_pem = check_permissions(user=g.username, object=snapshot, object_type="snapshot", level=codes.READ, roles=g.roles)
        elif request.method == 'PUT':
            # PUT snapshot requires USER
            has_pem = check_permissions(user=g.username, object=snapshot, object_type="snapshot", level=codes.USER, roles=g.roles)
        elif request.method == 'DELETE':
            # DELETE snapshot requires ADMIN
            has_pem = check_permissions(user=g.username, object=snapshot, object_type="snapshot", level=codes.ADMIN, roles=g.roles)

    if not has_pem:
        logger.info("NOT allowing request.")
        raise PermissionsException(f"Not authorized -- you do not have access to this snapshot endpoint.")


def authentication(request):
    if (request.url.path == '/redoc' or
        request.url.path == '/docs' or
        request.url.path == '/openapi.json' or
        request.url.path == '/traefik-config' or
        request.url.path.startswith('/error-handler/')):
        pass