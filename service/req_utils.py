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
logger = get_logger(__name__)


TAG = conf.version

async def error_handler(request: Request, exc):
    response = None
    status_code: int = -1
    if conf.show_traceback:
    #if False:
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
        # We are looking for all errors derived from BaseTapisError
        if isinstance(exc, BaseTapisError):
            response = error(msg=exc.msg)
            status_code = exc.code
        elif isinstance(exc, RequestValidationError) or isinstance(exc, ValidationError):
            error_list = []
            for error_dict in exc.errors():
                error_list.append(f"{', '.join(error_dict['loc'])}: {error_dict['msg']}")
            response = error(msg=error_list)
            status_code = 400
        else:
            response = error(msg=f'Unexpected. {repr(exc)}')
            status_code = 500

    return JSONResponse(
        status_code=status_code,
        content=response
    )
