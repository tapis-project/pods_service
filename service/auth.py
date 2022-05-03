# Utilities for authn/z
import base64
import os
import re
import timeit

import jwt
import requests
from tapisservice.tapisfastapi.utils import g

from __init__ import t, Tenants
from tapisservice.tapisfastapi.auth import authn_and_authz
from tapisservice.logs import get_logger
logger = get_logger(__name__)
from errors import ResourceError, PermissionsException

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
            logger.debug(f"request.url.path: {request.url.path}")
        else:
            logger.info("request.url has no path.")
            raise ResourceError(
                "Invalid request: the API endpoint does not exist or the provided HTTP method is not allowed.", 405)

    else:
        logger.info("Request has no request.url")
        raise ResourceError(
            "Invalid request: the API endpoint does not exist or the provided HTTP method is not allowed.", 405)

    # Generally request.base_url returns `https://tapis.io`
    g.api_server = request.base_url.replace('http://', 'https://')

    g.admin = False
    if request.method == 'OPTIONS':
        # allow all users to make OPTIONS requests
        logger.info("Allowing request because of OPTIONS method.")
        return True

    get_user_site_id()

    get_user_sk_roles()

    # there is a bug in wso2 that causes the roles claim to sometimes be missing; this should never happen:
    #if not g.roles:
    #    g.roles = ['Internal/everyone']

    # all other requests require some kind of abaco role:
    # THIS IS NO LONGER TRUE. Only rules are admin and privileged.

    logger.debug(f"request.url.path: {request.url.path}")

    # # the admin role when JWT auth is configured:
    # if codes.ADMIN_ROLE in g.roles:
    #     g.admin = True
    #     logger.info("Allowing request because of ADMIN_ROLE.")
    #     return True

    # the admin API requires the admin role:
    if '/pods/admin' in request.url.path:
        if g.admin:
            return True
        else:
            raise PermissionsException("Pod Admin role required.")

    # # the utilization endpoint is available to every authenticated user
    # if '/actors/utilization' == request.url_rule.rule or '/actors/utilization/' == request.url_rule.rule:
    #     return True

    # if '/actors/search/<string:search_type>' == request.url_rule.rule:
    #     return True

    # # there are special rules on the actors root collection:
    # if '/actors' == request.url_rule.rule or '/actors/' == request.url_rule.rule:
    #     # if we are here, it is either a GET or a new actor, so the request is allowed:
    #     logger.debug("new actor or GET on root connection. allowing request.")
    #     return True
