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

