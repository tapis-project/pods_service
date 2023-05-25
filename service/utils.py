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
        elif isinstance(exc, RequestValidationError) or isinstance(exc, ValidationError):
            error_list = []
            for error_dict in exc.errors():
                error_list.append(f"{', '.join(str(err) for err in error_dict['loc'])}: {error_dict['msg']}")
            response = error(msg=error_list)
            status_code = 400
        else:
            response = error(msg=f'Unexpected. {repr(exc)}')
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

def check_permissions(user, level, object, object_type, roles=None):
    """Check the appropriate permissions store for user and level.
    object: a pod, volume, or snapshot object. Also can be result of models_base.parse_permissions().
    """
    # Running something like: Checking pod_id: {pod.pod_id} permissions for user {user}
    logger.debug(f"Checking {object_type}_id: {eval(f'object.{object_type}_id')} permissions for user {user}")

    # first, if roles were passed, check for admin role
    if roles:
        if codes.ADMIN_ROLE in roles:
            return True

    # Get all permissions for this object_type
    # Running something like: volumes.get_permissions()
    permissions = object.get_permissions()
    
    # Attempt to get permission level for particular user.
    user_level = permissions.get(user)
    if not user_level:
        logger.info(f"Found no permissions for user {user} on {object_type}: {eval(f'object.{object_type}_id')}. Permissions: {permissions}")
        return False

    # Get user pem and compare to level.
    user_pem = codes.PermissionLevel(user_level)
    if user_pem >= level:
        logger.info(f"Allowing request - user has appropriate permission for {object_type}: {eval(f'object.{object_type}_id')}.")
        return True
    else:
        # we found the permission for the user but it was insufficient; return False right away
        logger.info(f"Found permission {level} for  {object_type}: {eval(f'object.{object_type}_id')}, insufficient permission, rejecting request.")
        return False
