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


@router.get("/pods/traefik-config",
    tags=["Misc"],
    summary="traefik_config_public",
    operation_id="traefik_config_public")
async def api_traefik_config_public():
    """
    Ingress-reachable alias of /traefik-config (the bare path never matches the public
    PathPrefix(/v3/pods) router). Same NOT-API/tokenless posture; used by the TapisUI
    Routing panel as the missing traefik dashboard.
    """
    return await api_traefik_config()

# ── Pod status landing page (splash / not-found) ──────────────────────────────
# One anonymized page that reflects the *actual* pod status so a stopped/errored/
# finished pod doesn't show a misleading "starting up…" forever. It looks the pod up
# by its hostname (already visible to the visitor in the URL) and reveals only the
# coarse status — never image, config, env, secrets, or logs.

# kind -> (title, message, badge, http_status, refresh?, glyph or None=spinner)
_POD_STATUS_PAGES = {
    "starting": ("Service is starting up",
                 "Your pod is starting. This page refreshes automatically and will load the app once it's ready.",
                 "starting", 503, True, None),
    "securing": ("Finishing startup",
                 "The pod is running and finishing its health checks and secure connection. Almost there.",
                 "securing", 503, True, None),
    "deleting": ("This pod is shutting down",
                 "The pod is being stopped. If you didn't expect this, check the Tapis Pods dashboard.",
                 "stopping", 503, True, None),
    "stopped":  ("This pod is stopped",
                 "The pod isn't running right now. Start it from the Tapis Pods dashboard, then reload this page.",
                 "stopped", 503, False, "⏸"),
    "error":    ("This pod failed to start",
                 "The pod hit an error and isn't serving traffic. Check its logs in the Tapis Pods dashboard.",
                 "error", 503, False, "⚠️"),
    "complete": ("This pod has finished",
                 "The pod ran to completion and is no longer serving traffic.",
                 "finished", 503, False, "✅"),
    "notfound": ("No pod at this address",
                 "This URL doesn't point to a known pod. It may have been deleted, or the address may be mistyped.",
                 "not found", 404, False, "🔌"),
}


def _kind_for_pod_status(status):
    """Map a pod.status string (or None) to a landing-page kind."""
    if status is None:
        return "notfound"
    s = str(status).upper()
    if s == "AVAILABLE":
        return "securing"          # running but behind readiness/cert gate
    if s in ("STOPPED", "OFF"):
        return "stopped"
    if s == "ERROR":
        return "error"
    if s == "COMPLETE":
        return "complete"
    if s == "DELETING":
        return "deleting"
    return "starting"              # REQUESTED / SPAWNER SETUP / CREATING / RESTART / unknown


def _pod_status_from_host(host):
    """Look up a pod's status from its hostname. Returns the status string, or None if the
    host can't be parsed or no such pod. Reads only pod.status."""
    if not host or ".pods." not in host:
        return None
    host = host.split(",")[0].strip().split(":")[0]  # first value, drop port
    left, right = host.split(".pods.", 1)
    pod_id = left.split("-", 1)[0].strip().lower()    # strip optional -<networking> suffix
    tenant = right.split(".", 1)[0].strip().lower()
    if not pod_id or not tenant:
        return None
    try:
        from models_pods import Pod
        pod = Pod.db_get_with_pk(pod_id, tenant=tenant, site=conf.site_id)
        return pod.status if pod else None
    except Exception:
        return None


def _render_pod_status(request: Request) -> HTMLResponse:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    kind = _kind_for_pod_status(_pod_status_from_host(host))
    title, message, badge, code, do_refresh, glyph = _POD_STATUS_PAGES[kind]
    refresh = int(getattr(conf, "cert_splash_refresh_seconds", 10) or 10)
    refresh_meta = f'<meta http-equiv="refresh" content="{refresh}">' if do_refresh else ""
    icon = '<div class="spinner"></div>' if glyph is None else f'<div class="glyph">{glyph}</div>'
    refresh_note = (f'<p class="hint">This page refreshes automatically every {refresh} seconds.</p>'
                    if do_refresh else "")
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {refresh_meta}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0;
         display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}}
    .card{{background:#1a1d27;border:1px solid #2d3148;border-radius:12px;
          padding:2.5rem 3rem;max-width:440px;width:100%;text-align:center}}
    .spinner{{width:40px;height:40px;border:3px solid #2d3148;
             border-top-color:#6366f1;border-radius:50%;
             animation:spin 0.9s linear infinite;margin:0 auto 1.5rem}}
    .glyph{{font-size:2.5rem;margin-bottom:1rem}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
    h1{{font-size:1.25rem;font-weight:600;margin-bottom:.5rem;color:#f1f5f9}}
    p{{font-size:.9rem;color:#94a3b8;line-height:1.5;margin-bottom:.75rem}}
    .badge{{display:inline-block;background:#1e2035;border:1px solid #3730a3;
           color:#818cf8;font-size:.75rem;padding:.2rem .6rem;border-radius:999px;margin-top:.5rem}}
    .hint{{font-size:.75rem;color:#475569;margin-top:1.25rem}}
  </style>
</head>
<body>
  <div class="card">
    {icon}
    <h1>{title}</h1>
    <p>{message}</p>
    <span class="badge">{badge}</span>
    {refresh_note}
  </div>
</body>
</html>"""
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate", "X-Robots-Tag": "noindex, nofollow"}
    if do_refresh:
        headers["Retry-After"] = str(refresh)
    return HTMLResponse(content=html, status_code=code, headers=headers)


@router.get(
    "/pod-splash",
    tags=["Misc"],
    summary="pod_splash",
    operation_id="pod_splash",
    include_in_schema=False)
async def pod_splash(request: Request):
    """Status-aware landing page Traefik routes to while a pod isn't serving its real app
    (readiness/cert gate, or stopped/errored). Anonymized — reveals only coarse pod status."""
    return _render_pod_status(request)


@router.get(
    "/pod-not-found",
    tags=["Misc"],
    summary="pod_not_found",
    operation_id="pod_not_found",
    include_in_schema=False)
async def pod_not_found(request: Request):
    """Served by Traefik's catch-all for hosts with no pod router (deleted/mistyped). Same
    status-aware page; an unparseable/unknown host resolves to 'No pod at this address'.
    Note: a brand-new domain that never had a cert can't reach this page (the browser fails
    the TLS handshake on the default cert first)."""
    return _render_pod_status(request)


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

