import traceback
from typing import List, Dict
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tapisservice.config import conf
from tapisservice.errors import BaseTapisError
from tapisservice.logs import get_logger
logger = get_logger(__name__)


TAG = conf.version

async def error_handler(request: Request, exc: Exception):
    #if conf.show_traceback:
    if True:
        logger.debug(f"building traceback for exception...")
        logger.debug(f"the type of exc is: {type(exc)}")
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
                logger.error(f"Got exception trying to format the exception! e: {e}")

    if not response and status_code:
        if isinstance(exc, BaseTapisError):
            response = error(msg=exc.msg)
            status_code = exc.code
        else:
            response = error(msg=f'Unexpected {type(exc).__name__}: {exc}')
            status_code = 500

    return JSONResponse(
        status_code=status_code,
        content=response
    )

def ok(result, msg="The request was successful.", metadata={}):
    if not isinstance(metadata, dict):
        raise TypeError("Got exception formatting response. Metadata should be dict.")
    d = {'result': result,
         'status': 'success',
         'version': TAG,
         'message': msg,
         'metadata': metadata}
    return d

def error(result=None, msg="Error processing the request.", metadata={}):
    if not isinstance(metadata, dict):
        raise TypeError("Got exception formatting response. Metadata should be dict.")
    d = {'result': result,
         'status': 'error',
         'version': TAG,
         'message': msg,
         'metadata': metadata}
    return d


# spec_path = os.environ.get("TAPIS_API_SPEC_PATH", '/home/tapis/service/resources/openapi_v3.yml')
# try:
#     spec_dict = yaml.safe_load(open(spec_path, 'r'))
#     spec = create_spec(spec_dict)
# except Exception as e:
#     msg = f"Could not find/parse API spec file at path: {spec_path}; additional information: {e}"
#     print(msg)
#     raise BaseTapisError(msg)

# class RequestParser(reqparse.RequestParser):
#     """Wrap reqparse to raise APIException."""

#     def parse_args(self, *args, **kwargs):
#         try:
#             return super(RequestParser, self).parse_args(*args, **kwargs)
#         except ClientDisconnected as exc:
#             raise BaseTapisError(exc.data['message'], 400)


# class TapisApi(Api):
#     """General flask_restful Api subclass for all the Tapis APIs."""
#     pass



"""
https://gist.github.com/ddanier/ead419826ac6c3d75c96f9d89bea9bd0
This allows to use global variables inside the FastAPI application
using async mode.
# Usage
Just import `g` and then access (set/get) attributes of it:
```python
from your_project.globals import g
g.foo = "foo"
# In some other code
assert g.foo == "foo"
```
Best way to utilize the global `g` in your code is to set the desired
value in a FastAPI dependency, like so:
```python
async def set_global_foo() -> None:
    g.foo = "foo"
@app.get("/test/", dependencies=[Depends(set_global_foo)])
async def test():
    assert g.foo == "foo"
```
# Setup
Add the `GlobalsMiddleware` to your app:
```python
app = fastapi.FastAPI(
    title="Your app API",
)
app.add_middleware(GlobalsMiddleware)  # <-- This line is necessary
```
Then just use it. ;-)
"""
from contextvars import ContextVar, Token
from typing import Any, Dict

from starlette.types import ASGIApp, Receive, Scope, Send


class Globals:
    __slots__ = ("_vars", "_reset_tokens")

    _vars: Dict[str, ContextVar]
    _reset_tokens: Dict[str, Token]

    def __init__(self) -> None:
        object.__setattr__(self, '_vars', {})
        object.__setattr__(self, '_reset_tokens', {})

    def reset(self) -> None:
        for _name, var in self._vars.items():
            try:
                var.reset(self._reset_tokens[_name])
            # ValueError will be thrown if the reset() happens in
            # a different context compared to the original set().
            # Then just set to None for this new context.
            except ValueError:
                var.set(None)

    def _ensure_var(self, item: str) -> None:
        if item not in self._vars:
            self._vars[item] = ContextVar(f"globals:{item}", default=None)
            self._reset_tokens[item] = self._vars[item].set(None)

    def __getattr__(self, item: str) -> Any:
        self._ensure_var(item)
        return self._vars[item].get()

    def __setattr__(self, item: str, value: Any) -> None:
        self._ensure_var(item)
        self._vars[item].set(value)


class GlobalsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        g.reset()
        await self.app(scope, receive, send)


g = Globals()