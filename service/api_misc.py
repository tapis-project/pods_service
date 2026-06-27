from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from tapisservice.errors import BaseTapisError
from tapisservice.tapisfastapi.utils import g, ok, error
from kubernetes_utils import get_traefik_configmap
from models_pods import TapisApiModel
from tapisservice.config import conf
import yaml

router = APIRouter()

@router.get("/traefik-config",
    tags=["Misc"],
    summary="traefik_config",
    operation_id="traefik_config")
async def api_traefik_config():
    """
    Supplies traefik-config to service. Returns json traefik-config object for
    traefik to use with the http provider. Dynamic configs don't work well in 
    Kubernetes.
    """
    config = get_traefik_configmap()
    yaml_config = yaml.safe_load(config.to_dict()['data']['traefik.yml'])
    return yaml_config

@router.get(
    "/pod-splash",
    tags=["Misc"],
    summary="pod_splash",
    operation_id="pod_splash",
    include_in_schema=False)
async def pod_splash():
    """Served by Traefik for pods that are AVAILABLE but not yet ready (readiness probe still
    running) or whose TLS certificate is still being issued (held by the cert gate in
    health_central). Returns a generic, anonymized HTML page — intentionally reveals no pod
    name, image, or config. Auto-refreshes (interval from `cert_splash_refresh_seconds`) so
    browsers pick up the real service once it's ready and secured.
    """
    refresh = int(getattr(conf, 'cert_splash_refresh_seconds', 10) or 10)
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{refresh}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Starting…</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0;
         display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}}
    .card{{background:#1a1d27;border:1px solid #2d3148;border-radius:12px;
          padding:2.5rem 3rem;max-width:420px;width:100%;text-align:center}}
    .spinner{{width:40px;height:40px;border:3px solid #2d3148;
             border-top-color:#6366f1;border-radius:50%;
             animation:spin 0.9s linear infinite;margin:0 auto 1.5rem}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
    h1{{font-size:1.25rem;font-weight:600;margin-bottom:.5rem;color:#f1f5f9}}
    p{{font-size:.9rem;color:#94a3b8;line-height:1.5;margin-bottom:.75rem}}
    .badge{{display:inline-block;background:#1e2035;border:1px solid #3730a3;
           color:#818cf8;font-size:.75rem;padding:.2rem .6rem;border-radius:999px;margin-top:.5rem}}
    .refresh{{font-size:.75rem;color:#475569;margin-top:1.25rem}}
  </style>
</head>
<body>
  <div class="card">
    <div class="spinner"></div>
    <h1>Service is starting up</h1>
    <p>Health checks are running and the secure connection (TLS certificate) is being finalized. The service will be available shortly.</p>
    <p>This page refreshes automatically every {refresh} seconds.</p>
    <span class="badge">starting &amp; securing</span>
    <p class="refresh">If this takes more than a few minutes, the service may have failed to start.</p>
  </div>
</body>
</html>"""
    return HTMLResponse(
        content=html,
        status_code=503,
        headers={
            "Retry-After": str(refresh),
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@router.get("/healthcheck",
    tags=["Misc"],
    summary="healthcheck",
    operation_id="healthcheck")
async def api_healthcheck():
    """
    Health check for service. Returns healthy when api is running.
    Should add database health check, should add kubernetes health check
    """
    return ok("I promise I'm healthy.")

@router.get(
    "/error-handler/{status}",
    tags=["Misc"],
    summary="error_handler",
    operation_id="error_handler")
async def error_codes(status):
    """Handles all error codes from Traefik.
    """
    status = int(status)
    match status:
        case 400:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 401:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 402:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 403:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 404:
            message = "Invalid request: Invalid request: the requested URL is not an Pods endpoint."
        case 405:
            message = "Invalid request: The Pods service does not know how to fulfill the request."
        case 500:
            message = "Unable to parse Pods service response. The server may be misconfigured or overloaded."
        case 501:
            message = "Unable to parse Pods service response. The server may be misconfigured or overloaded."
        case 502:
            message = "Timeout error waiting on Pods service response. The server may be busy or overloaded."
        case 503:
            message = "Unable to parse Pods service response. The server may be misconfigured or overloaded."
        case 504:
            message = "Unable to parse Pods service response. The server may be misconfigured or overloaded."
        case _:
            message = "Invalid request: The Pods service does not know how to fulfill the request."

    return JSONResponse(status_code=status, content=error(message))


###
### Tapis JWT Auth - for pre-pod auth
###
def is_logged_in(cookies):
    """
    Check whether the current session contains a valid login;
    If so: return True, username, roles
    Otherwise: return False, None, None
    """
    if 'username' in cookies:
        return True, cookies['username'], cookies['roles']
    return False, None, None


# @router.get("/pods/auth",
#     tags=["Misc"],
#     summary="OAuth2 endpoint to act as middleware between pods and user traffic, checking for authorization on a per url basis.",
#     operation_id="auth")
# async def api_auth(request: Request, username: str = None):
#     """
#     Write to session

#     Traefik continues to user pod if 200, otherwise goes to result.
#     Process a callback from a Tapis authorization server:
#       1) Get the authorization code from the query parameters.
#       2) Exchange the code for a token
#       3) Add the user and token to the sessionhttps
#       4) Redirect to the /data endpoint.
#     """
#     logger.debug(f"In pod-auth, headers: {request.headers}, request.cookies: {request.cookies}")
#     #return JSONResponse(status_code=400,content = str(request.headers))
    
#     ## Headers contains x-forwarded stuff we can use to deduct correct tenant (x-forwarded-host)
#     ## example data for future reference
#     # 'x-forwarded-for': '10.233.72.192'
#     # 'x-forwarded-host': 'tacc.develop.tapis.io'
#     # 'x-forwarded-port': '80', 'x-forwarded-prefix': '/v3'
#     # 'x-forwarded-proto': 'http'
#     # 'x-forwarded-server': 'pods-traefik-65c7ccb5fd-ffk4g'
#     # 'x-real-ip': '10.233.72.193'

    
#     ## if x-tapis-token in headers or in session, continue, otherwise authorize and set one or both.
#     xTapisToken = "test"
#     if username:
#         return JSONResponse(
#             status_code=200,
#             content=ok(f"I promise I'm username: {username}."),
#             # session={
#             #     "X-TapisUsername": username,
#             #     "X-Tapis-Token": xTapisToken
#             # },
#             headers={
#                 "X-TapisUsername": username,
#                 "X-Tapis-Token": xTapisToken
#             })
#     else:
#         authenticated, _, _ = is_logged_in(request.cookies)
#         # if already authenticated, return 200, which will allow the request to continue in Traefik
#         if authenticated:
#             return {'code': 200} #result = {'path':'/', 'code': 302}

#         # if not authenticated, start the OAuth flow
#         app_base_url = "https://tacc.develop.tapis.io"

#         client_def = {
#             "client_id": "testdev",
#             "client_key": "4STQ^t&RGa$sah!SZ9zCP9UScGoEkS^GYLZDjjtjPBipp4kVLyrr@X",
#             "callback_url": "https://tacc.develop.tapis.io/v3/pods/auth/callback",
#             "display_name": "pods-tacc-tacc-client-1",
#             "description": "Testing client for Pods traefik auth"
#         }

#         client_id = "testdev"
#         callback_url = f"{app_base_url}/oauth2/callback" # should match client callback_url  
#         tapis_url = f"{app_base_url}/v3/oauth2/authorize?client_id={client_id}&redirect_uri={callback_url}&response_type=code"
#         # print('no, not auth, redirect to:',tapis_url)
#         result = {'path': tapis_url, 'code': 302}
#         return RedirectResponse(url=tapis_url, status_code=302)
#         return JSONResponse(content = str(result))

#     # Shouldn't be able to get here
#     raise Exception(f"not implemented")
#     return ok("I promise I'm healthy.")



###
### From VC1/backend/app.py
###
def get_username(token):
    """
    Validate a Tapis JWT, `token`, and resolve it to a username.
    """
    headers = {'Content-Type': 'text/html'}
    # call the userinfo endpoint
    url = f"{config['tapis_base_url']}/v3/oauth2/userinfo"
    headers = {'X-Tapis-Token': token}
    try:
        rsp = requests.get(url, headers=headers)
        rsp.raise_for_status()
        username = rsp.json()['result']['username']
    except Exception as e:
        raise Exception(f"Error looking up token info; debug: {e}")
    return username

# @router.get("/pods/auth/callback",
#     tags=["Misc"],
#     summary="callback.",
#     operation_id="auth")
# def callback(request: Request):
#     # return JSONResponse(content = str(dir(request)))
#     # code = request.args.get('code')
#     # if not code:
#     #     raise Exception(f"Error: No code in request; debug: {request.args}")
#     url = f"{config['tapis_base_url']}/v3/oauth2/tokens"
#     data = {
#         "code": "code",
#         "redirect_uri": f"{config['app_base_url']}/oauth2/callback",
#         "grant_type": "authorization_code",
#     }
#     try:
#         response = requests.post(url, data=data, auth=(config['client_id'], config['client_key']))
#         response.raise_for_status()
#         json_resp = json.loads(response.text)
#         token = json_resp['result']['access_token']['access_token']
#     except Exception as e:
#         raise Exception(f"Error generating Tapis token; debug: {e}")

#     username = auth.get_username(token)
    
#     response = make_response(redirect(os.environ['FRONT_URL'], code=302))

#     domain = os.environ.get('COOKIE_DOMAIN', ".pods.icicle.tapis.io")
#     response.set_cookie("token", token, domain=domain, secure=True)
#     response.set_cookie("username", username, domain=domain, secure=True)    
    
#     return response


def login():
    """
    Check for the existence of a login session, and if none exists, start the OAuth2 flow.
    """
    authenticated, _, _ = is_logged_in()
    # if already authenticated, redirect to the root URL
    if authenticated:
        result = {'path':'/', 'code': 302}
        return result
    # otherwise, start the OAuth flow
    
    callback_url = f"{config['app_base_url']}/oauth2/callback"  #https://vaapibackend.pods.icicle.tapis.io
    tapis_url = f"{config['tapis_base_url']}/v3/oauth2/authorize?client_id={config['client_id']}&redirect_uri={callback_url}&response_type=code"
    # print('no, not auth, redirect to:',tapis_url)
    result = {'path': tapis_url, 'code': 302}
    return jsonify(result)



###
### From iciflaskn
###
def add_user_to_session(username, token):
    """
    Add a user's identity and Tapis token to the session. 
    Also, look up users roles in Tapis and add those to the session.
    The list of roles are returned.
    """
    session['username'] = username
    session['token'] = token
    # also, look up user's roles
    t = Tapis(base_url=config['tapis_base_url'], access_token=token)
    try:
        result = t.sk.getUserRoles(user=username, tenant=config['tenant'])
        session['roles'] = result.names
    except Exception as e:
        raise Exception(f"Error getting user's roles; debug: {e}")
    return result.names


def clear_session():
    """
    Remove all data on the session; this function is called on logout.
    """
    session.pop('username', None)
    session.pop('token', None)
    session.pop('roles', None)


# test-auth:
#     forwardAuth:
#     #address: "https://tacc.develop.tapis.io/v3/oauth2/idp" 
#     #address: "https://icicleai.tapis.io/v3/oauth2/authorize?client_id=va-api-prod-client&redirect_uri=https://vaapibackend.pods.icicle.tapis.io/oauth2/callback&response_type=code"
#     tls:
#         insecureSkipVerify: true

# # Your client credentials
# client_id: va-api-prod-client
# client_key: 8dc2ac4051a6774813af38004dad2ba960536c8e7e62442e10ba29deed85924c
# # The Tapis base URL and tenant id
# tapis_base_url: https://icicleai.tapis.io
# tenant: icicleai

