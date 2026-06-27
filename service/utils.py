import traceback
from typing import List, Dict
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from tapisservice.tapisfastapi.utils import error
from tapisservice.config import conf
from tapisservice.errors import BaseTapisError
from tapisservice.logs import get_logger
from sqlalchemy.exc import IntegrityError
logger = get_logger(__name__)


TAG = conf.version

async def error_handler(request: Request, exc):
    logger.debug(f"Top of Pods Service error handler. Got error: {repr(exc)}")
    response = None
    status_code: int = -1
    if conf.show_traceback:
        logger.debug(f"building traceback for exception...")
        logger.debug(f"error type is: {type(exc).__name__}")
        try:
            raise exc
        except Exception:
            logger.debug("caught the re-raised exception.")
            try:
                trace = traceback.format_exc()
                logger.debug(f"re-raised exception; trace: {trace}")
                # Set response for development traceback in req.
                response = error(msg=f'conf.show_traceback = True; only for development:\n {trace}')
                status_code = 500
            except Exception as e:
                logger.error(f"Got exception trying to format the exception! e: {repr(e)}")

    if not response and status_code == -1:
        # We are looking for all errors derived from 
        logger.debug(f"Top of Pods Service error handler. Got error type: {type(exc).__name__}")
        if isinstance(exc, BaseTapisError):
            response = error(msg=exc.msg)
            status_code = exc.code
        elif isinstance(exc, IntegrityError):

            extra_end_str = "\\n')"
            response = error(msg=f"Duplicate key found:{repr(exc).split('DETAIL: ')[1].replace(extra_end_str, '')}")
            status_code = 500
            if "psycopg2.errors.NotNullViolation" in repr(exc):
                msg = f"Got IntegrityError - psycopg2.errors.NotNullViolation: {repr(exc).split('DETAIL: ')[1].replace(extra_end_str, '')}"
                response = error(msg=msg)
            elif "psycopg2.errors.UniqueViolation" in repr(exc):
                msg = f"Got IntegrityError - psycopg2.errors.UniqueViolation : {repr(exc).split('DETAIL: ')[1].replace(extra_end_str, '')}"
                response = error(msg=msg)
        elif isinstance(exc, RequestValidationError) or isinstance(exc, ValidationError):
            error_list = []
            logger.debug(f"Got validation error: {repr(exc)}")
            for error_dict in exc.errors():
                error_list.append(f"{', '.join(str(err) for err in error_dict['loc'])}: {error_dict['msg']}")
            if error_list is None:
                response = error(msg=f'Unexpected. {repr(exc)}')
            response = error(msg=error_list)
            status_code = 400
        elif isinstance(exc, ValueError):
            # ValueError is used for validation errors raised from code
            response = error(msg=str(exc))
            status_code = 400
        else:
            # Unexpected exception: log the full server-side traceback (file:line) so 500s are
            # diagnosable from the service logs even when conf.show_traceback is off in prod. The
            # response stays terse — the traceback is logs-only, never returned to the caller.
            logger.error(
                f"Unexpected exception handling {request.method} {request.url.path}: "
                f"{repr(exc)}\n{traceback.format_exc()}"
            )
            response = error(msg=f'Unexpected. {repr(exc)} debug: {exc.errors() if hasattr(exc, "errors") else "no errors() method"}')
            status_code = 500

    return JSONResponse(
        status_code=status_code,
        content=response
    )


import re
from starlette.datastructures import URL
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

repeated_quotes = re.compile(r'//+')

class HttpUrlRedirectMiddleware:
  """
  # https://github.com/tiangolo/fastapi/issues/2090
  This http middleware redirects urls with repeated slashes to the cleaned up
  versions of the urls
  """

  def __init__(self, app: ASGIApp) -> None:
    self.app = app

  async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:

    if scope["type"] == "http" and repeated_quotes.search(URL(scope=scope).path):
      url = URL(scope=scope)
      url = url.replace(path=repeated_quotes.sub('/', url.path))
      response = RedirectResponse(url, status_code=307)
      await response(scope, receive, send)
    else:
      await self.app(scope, receive, send)

import codes

def _pod_stack_grants(user, level, pod, roles=None, tenant=None):
    """Stack permission inheritance: if a pod belongs to a stack, the parent stack's
    permission list also grants access (effective pod perm = max(pod, stack)).

    Returns True iff the pod's stack grants `level` to `user`. Falsey/missing stack_id or a
    deleted stack → False (no inheritance, falls back to the pod's own result).
    """
    stack_id = getattr(pod, "stack_id", None)
    if not stack_id:
        return False
    from models_stacks import Stack
    stack = Stack.db_get_with_pk(
        stack_id,
        tenant=getattr(pod, "tenant_id", None) or tenant,
        site=getattr(pod, "site_id", None),
    )
    if not stack:
        return False
    logger.info(f"Checking inherited stack '{stack_id}' permission for pod {getattr(pod, 'pod_id', '?')}.")
    return check_permissions(user, level, stack, "stack", roles=roles, tenant=tenant)


def check_permissions(user, level, object, object_type, roles=None, tenant=None):
    """Check the appropriate permissions store for user and level.
    user: username
    level: codes.PermissionLevel enum
    object: a pod, volume, or snapshot object. Also can be result of models_base.parse_permissions().
    object_type: "pod", "volume", "snapshot", or "template"
    roles: passthrough roles for checking admin
    tenant: tenant_id of incoming request to check against tenant-scoped permissions (tenant.dev:READ)
    """
    # Running something like: Checking pod_id: {pod.pod_id} permissions for user {user}
    logger.debug(f"Checking {object_type}_id: {eval(f'object.{object_type}_id')} permissions for user {user}")

    # Admin bypass only when admin mode is explicitly activated via X-Pods-Admin header.
    # g.admin_active is already gated by g.admin (which checks ADMIN_ROLE or hardcoded usernames)
    # in check_route_permissions, so we trust it directly here.
    from tapisservice.tapisfastapi.utils import g as _g
    if getattr(_g, 'admin_active', False):
        return True

    # Get all permissions for this object_type
    # Running something like: volumes.get_permissions()
    permissions = object.get_permissions()
    
    # Check for site(**:READ) perms, only for template
    if object_type == "template":
        site_wide_level = permissions.get("**")
        if site_wide_level:
            site_pem = codes.PermissionLevel(site_wide_level)
            if site_pem >= level:
                logger.info(f"Allowing request - site-wide '**' permission grants {site_wide_level} for {object_type}: {eval(f'object.{object_type}_id')}.")
                return True
        
        # tenant-wide(tenant.*:READ) check requires incoming tenant arg to check against
        if tenant:
            tenant_key = f"tenant.{tenant}"
            tenant_scoped_level = permissions.get(tenant_key)
            if tenant_scoped_level:
                tenant_pem = codes.PermissionLevel(tenant_scoped_level)
                if tenant_pem >= level:
                    logger.info(f"Allowing request - {tenant_key} permission grants {tenant_scoped_level} for {object_type}: {eval(f'object.{object_type}_id')}.")
                    return True
    
    # Attempt to get permission level for particular user.
    user_level = permissions.get(user)
    wildcard_level = permissions.get("*")
    if not user_level and not wildcard_level:
        logger.info(f"Found no permissions for user {user} on {object_type}: {eval(f'object.{object_type}_id')}. Permissions: {permissions}")
        if object_type == "pod" and _pod_stack_grants(user, level, object, roles=roles, tenant=tenant):
            return True
        return False
    elif wildcard_level:
        # If we have a wildcard permission, use that instead of user permission.
        logger.info(f"Found wildcard permission for {object_type} for user {user}.")
        user_level = wildcard_level

    tenant_wide_level = permissions.get("TENANT")
    # Get user pem and compare to level.
    user_pem = codes.PermissionLevel(user_level)
    if user_pem >= level:
        logger.info(f"Allowing request - user has appropriate permission for {object_type}: {eval(f'object.{object_type}_id')}.")
        return True
    elif tenant_wide_level and codes.PermissionLevel(tenant_wide_level) >= level:
        logger.info(f"Allowing request - TENANT has appropriate permission for {object_type}: {eval(f'object.{object_type}_id')}.")
        return True
    else:
        # we found the permission for the user but it was insufficient; try stack inheritance for pods
        logger.info(f"Found permission {level} for  {object_type}: {eval(f'object.{object_type}_id')}, insufficient permission, rejecting request.")
        if object_type == "pod" and _pod_stack_grants(user, level, object, roles=roles, tenant=tenant):
            return True
        return False
