"""
Entity-agnostic tapis_auth machinery — the Traefik forwardAuth / OAuth2 browser flow
plus its helpers, extracted from api_pods_podid_func so more than one resource type can
gate its public hostname behind Tapis auth.

Consumers build a TapisAuthEntity (auth config + permissions + public hostname + OAuth
client identity) and delegate their forwardAuth endpoints to run_tapis_auth_check() /
run_tapis_auth_callback():
  - pod networking auth:  GET /pods/{pod_id_net}/auth[/callback]   (api_pods_podid_func)
  - node route auth:      GET /pods/routes/{route_id}/auth[/callback] (api_nodes)

The flow (unchanged from the pod-only implementation):
  1) Traefik forwardAuth hits the entity's /auth endpoint.
  2) A valid X-Tapis-Token cookie/header + allowed-users check -> 200 with the
     entity's configured response headers.
  3) Otherwise browser navigations are bounced through the tenant's OAuth2 authorize
     flow using a service-managed client; /auth/callback exchanges the code, validates
     the token, re-checks allowed users, sets token cookies, and redirects to the
     entity's return path.
Cross-tenant access is allowed only for tenants granted via 'tenant.<id>' permission
entries on the entity.
"""

import re

import jwt
import requests
from dataclasses import dataclass
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from codes import READ, USER, ADMIN, PermissionLevel
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from log_redaction import scrub_headers, scrub_cookies
from tapisservice.logs import get_logger
from __init__ import t, BadRequestError

logger = get_logger(__name__)


# Special group strings for tapis_auth_allowed_users.
# These resolve against the entity's permissions list at auth time.
# Maps each group string to the minimum PermissionLevel required.
AUTH_GROUP_LEVEL_MAP = {
    "AUTHORIZED_READS": READ,         # READ, USER, ADMIN, APPROVEDADMIN
    "AUTHORIZED_USERS": USER,         # USER, ADMIN, APPROVEDADMIN
    "AUTHORIZED_ADMINS": ADMIN,       # ADMIN, APPROVEDADMIN
}


def check_tapis_auth_allowed(username: str, tapis_auth_allowed_users: list, entity_permissions: dict) -> bool:
    """Check if a username is allowed by tapis_auth_allowed_users.

    Supports:
    - "*" wildcard: all authenticated users allowed.
    - Literal usernames: exact match (case-insensitive).
    - Special group strings that resolve against the entity's permissions:
        AUTHORIZED_READS  -> users with READ or higher permission
        AUTHORIZED_USERS  -> users with USER or higher permission
        AUTHORIZED_ADMINS -> users with ADMIN or higher (including APPROVEDADMIN)

    Args:
        username: The authenticated Tapis username.
        tapis_auth_allowed_users: The entity's tapis_auth_allowed_users list.
        entity_permissions: Dict from <entity>.get_permissions(), e.g. {"user1": "ADMIN", "user2": "READ"}.

    Returns:
        True if the user is allowed, False otherwise.
    """
    if not tapis_auth_allowed_users:
        return True  # empty list = no restriction

    username_lower = username.lower()

    # Check for wildcard
    if "*" in tapis_auth_allowed_users:
        return True

    # Check for literal username match
    if username_lower in [u.lower() for u in tapis_auth_allowed_users if u not in AUTH_GROUP_LEVEL_MAP and u != "*"]:
        return True

    # Check special group strings against entity permissions
    user_perm_str = entity_permissions.get(username_lower) or entity_permissions.get(username)
    if user_perm_str:
        user_level = PermissionLevel(user_perm_str)
        for group_str in tapis_auth_allowed_users:
            required_level = AUTH_GROUP_LEVEL_MAP.get(group_str)
            if required_level is not None and user_level >= required_level:
                return True

    return False


def get_allowed_tenants_from_permissions(entity_permissions: dict) -> list:
    """Extract allowed tenant IDs from an entity's permissions.

    Scans permissions for entries matching the 'tenant.<tenant_id>' pattern
    and returns a list of the tenant IDs.

    Args:
        entity_permissions: Dict from <entity>.get_permissions(), e.g. {"user1": "ADMIN", "tenant.public": "USER"}.

    Returns:
        List of tenant ID strings, e.g. ["public", "dev"].
    """
    tenants = []
    for key in entity_permissions:
        if key.startswith("tenant."):
            tenant_id = key[len("tenant."):]
            if tenant_id:
                tenants.append(tenant_id)
    return tenants


def check_tapis_auth_tenant_allowed(token_tenant_id: str, request_tenant_id: str, tapis_auth_allowed_tenants: list) -> bool:
    """Check if a token's tenant is allowed to access an entity.

    The entity's own tenant (request_tenant_id) is always allowed.
    Additional tenants can be allowed via the entity's permissions list
    (extracted by get_allowed_tenants_from_permissions()).

    Args:
        token_tenant_id: The tenant_id from the JWT token (e.g. 'public').
        request_tenant_id: The entity's host tenant from the URL (e.g. 'tacc').
        tapis_auth_allowed_tenants: List of additional tenant IDs allowed (from entity permissions).

    Returns:
        True if the token's tenant is allowed, False otherwise.
    """
    if not token_tenant_id:
        return True  # no token tenant info = allow (will be caught by token validation)
    # Entity's own tenant is always allowed
    if token_tenant_id == request_tenant_id:
        return True
    # Check against allowed tenants list
    if token_tenant_id in tapis_auth_allowed_tenants:
        return True
    return False


def get_token_tenant_id(token: str) -> str:
    """
    Extract the tenant_id from a Tapis JWT without full validation.
    Returns the tenant_id string or None if extraction fails.
    """
    try:
        # Unverified peek is contained: the extracted tenant_id must resolve
        # through t.tenant_cache (a fixed registry — no attacker-steered URLs),
        # and the token is then actually validated by the userinfo call.
        # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
        claims = jwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
        return claims.get('tapis/tenant_id')
    except Exception as e:
        logger.debug(f"Could not extract tenant_id from token: {e}")
        return None


def validate_token(request: Request, token: str = None):
    """
    Validate a Tapis JWT from cookies or headers by making a call to the get_userinfo endpoint.
    For cross-tenant tokens, the userinfo call is made to the token's tenant, not the request tenant.
    Returns authorized:bool, username:str, roles:List[str]
    """
    logger.debug(f"Validating token from request: cookies={scrub_cookies(request.cookies)}, headers={scrub_headers(request.headers)}")
    token = token or request.cookies.get('X-Tapis-Token') or request.headers.get('X-Tapis-Token') or request.headers.get('x-tapis-token') or request.headers.get('X-TAPIS-TOKEN')
    if not token:
        logger.debug("Token not found in cookies or headers.")
        return False, None, None

    # Determine the correct base URL for userinfo.
    # The token may be from a different tenant than the entity (cross-tenant auth),
    # so we must call userinfo on the token's tenant, not the request base_url.
    token_tenant = get_token_tenant_id(token)
    if token_tenant:
        try:
            tenant_base_url = t.tenant_cache.get_tenant_config(tenant_id=token_tenant).base_url
            url = f"{tenant_base_url}/v3/oauth2/userinfo"
        except Exception as e:
            logger.warning(f"Could not resolve base_url for token tenant '{token_tenant}', falling back to request base_url: {e}")
            url = f"{request.base_url}v3/oauth2/userinfo".replace('http://', 'https://')
    else:
        url = f"{request.base_url}v3/oauth2/userinfo".replace('http://', 'https://')

    logger.debug(f"Running get_userinfo with url: {url} (token_tenant: {token_tenant})")
    headers = {'X-Tapis-Token': token}
    try:
        # This runs inside the traefik forwardAuth path (pre-auth, every proxied
        # request) — without a timeout, one hung tenant host pins API workers.
        # Both url branches above force https (tenant_cache base_url / replace).
        # nosemgrep: python.lang.security.audit.insecure-transport.requests.request-with-http.request-with-http
        rsp = requests.get(url, headers=headers, timeout=(3.05, 10))
        rsp.raise_for_status()
        username = rsp.json()['result'].get('username')
        email = rsp.json()['result'].get('email')
        name = rsp.json()['result'].get('name')
        roles = rsp.json()['result'].get('roles', [])
        logger.info(f"Token validated successfully. Username: {username}, Email: {email}, Name: {name}")
        return True, username, roles
    except Exception as e:
        logger.error(f"Error with request to userinfo and parsing: {e}")
        return False, None, None


def build_auth_response_headers(auth_cfg: dict, tenant_id: str, site_id: str, username: str = "nouser"):
    """
    Build the tapis_auth response headers from an entity's auth config.

    If <entity>.tapis_auth_response_headers is:
    {
        "X-Tapis-Username": <<tapisusername>>@tapis.io",
        "FROM": "pods auth endpoint from <<tenant>>.<<site>>",
        "OAUTH2_USERNAME_KEY": "username"
    }

    Then we set headers from auth calls to the backend container as set by user.
    Users can specify <<tapisusername>>, <<tapistenantid>>, or <<tapissiteid>> for replacement.

    Final headers to pass to the backend:
    headers = {
        "X-Tapis-Username": myuser@tapis.io,
        "FROM": "pods auth endpoint from tacc.tacc",
        "OAUTH2_USERNAME_KEY": "username"
    }
    """
    tapis_auth_response_headers = auth_cfg.get("tapis_auth_response_headers", {})
    final_headers = {}
    if tapis_auth_response_headers:
        for header, value in tapis_auth_response_headers.items():
            if "<<tapisusername>>" in value:
                value = value.replace("<<tapisusername>>", username)
            if "<<tapistenantid>>" in value:
                value = value.replace("<<tapistenantid>>", tenant_id)
            if "<<tapissiteid>>" in value:
                value = value.replace("<<tapissiteid>>", site_id)
            # We should rarely ever send token, leaving commented for now.
            # Only some admins should be able to. No use case yet.
            #if "<<token>>" in value:
            #    value = value.replace("<<token>>", "token")
            final_headers[header] = value
    return final_headers


# ---------------------------------------------------------------------------
# Field validators — shared by every model that carries the tapis_auth config
# (pod Networking, node Route). `prefix` keeps error messages pointing at the
# caller's field namespace (e.g. "networking.tapis_auth_return_path").
# ---------------------------------------------------------------------------

def validate_tapis_auth_response_headers(v, prefix: str = "networking"):
    if v:
        if not isinstance(v, dict):
            raise TypeError(f"{prefix}.tapis_auth_response_headers must be dict. Got '{type(v).__name__}'.")
        for header_name, header_val in v.items():
            if not isinstance(header_name, str):
                raise TypeError(f"{prefix}.tapis_auth_response_headers key type must be str. Got '{type(header_name).__name__}', key: '{header_name}'.")
            if not isinstance(header_val, str):
                raise TypeError(f"{prefix}.tapis_auth_response_headers val type must be str. Got '{type(header_val).__name__}', value: '{header_val}'.")
    return v


def validate_tapis_auth_return_path(v, prefix: str = "networking"):
    if v:
        if not v.startswith('/'):
            raise ValueError(f"{prefix}.tapis_auth_return_path should start with '/'. Got {v}")
        # Regex match to ensure url is safe with only [A-z0-9.-/] chars.
        res = re.fullmatch(r'(?:[A-Za-z0-9.\-_\/]+)', v)
        if not res:
            raise ValueError(f"{prefix}.tapis_auth_return_path should start with '/' and can contain alphanumeric characters, periods, forward-slash, underscores, and hyphens. Got {v}")
        if len(v) > 180:
            raise ValueError(f"{prefix}.tapis_auth_return_path length must be below 180 characters. Got length: {len(v)}")
    return v


def validate_tapis_auth_allowed_users(v, prefix: str = "networking"):
    if v:
        if not isinstance(v, list):
            raise TypeError(f"{prefix}.apis_auth_allowed_users must be list. Got '{type(v).__name__}'.")
        for user in v:
            if not isinstance(user, str):
                raise TypeError(f"{prefix}.tapis_auth_allowed_users must be list of str. Got '{type(user).__name__}'.")
    return v


def validate_tapis_auth_excluded_paths(v, prefix: str = "networking"):
    if v:
        if not isinstance(v, list):
            raise TypeError(f"{prefix}.tapis_auth_excluded_paths must be list. Got '{type(v).__name__}'.")
        if len(v) > 50:
            raise ValueError(f"{prefix}.tapis_auth_excluded_paths must have at most 50 entries. Got {len(v)}.")
        for path in v:
            if not isinstance(path, str):
                raise TypeError(f"{prefix}.tapis_auth_excluded_paths must be list of str. Got '{type(path).__name__}'.")
            if not path.startswith('/'):
                raise ValueError(f"{prefix}.tapis_auth_excluded_paths values must start with '/'. Got '{path}'.")
            if not path.isascii():
                raise ValueError(f"{prefix}.tapis_auth_excluded_paths values must be ASCII. Got '{path}'.")
            if len(path) > 256:
                raise ValueError(f"{prefix}.tapis_auth_excluded_paths values must be less than 256 characters. Got length {len(path)}.")
    return v


def validate_tapis_auth_excluded_path_regex(v, prefix: str = "networking"):
    if v:
        if not isinstance(v, list):
            raise TypeError(f"{prefix}.tapis_auth_excluded_path_regex must be list. Got '{type(v).__name__}'.")
        if len(v) > 20:
            raise ValueError(f"{prefix}.tapis_auth_excluded_path_regex must have at most 20 entries. Got {len(v)}.")
        for pattern in v:
            if not isinstance(pattern, str):
                raise TypeError(f"{prefix}.tapis_auth_excluded_path_regex must be list of str. Got '{type(pattern).__name__}'.")
            if not pattern.isascii():
                raise ValueError(f"{prefix}.tapis_auth_excluded_path_regex values must be ASCII. Got '{pattern}'.")
            if len(pattern) > 512:
                raise ValueError(f"{prefix}.tapis_auth_excluded_path_regex values must be less than 512 characters. Got length {len(pattern)}.")
            # Validate the regex compiles
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"{prefix}.tapis_auth_excluded_path_regex contains invalid regex '{pattern}': {e}")
    return v


# ---------------------------------------------------------------------------
# The forwardAuth flow itself
# ---------------------------------------------------------------------------

@dataclass
class TapisAuthEntity:
    """Everything the shared flow needs to know about the thing being protected."""
    auth_cfg: dict        # tapis_auth, tapis_auth_response_headers, tapis_auth_allowed_users, tapis_auth_return_path
    permissions: dict     # {user_or_tenant_entry: LEVEL} — group + tenant.* resolution
    public_url: str       # hostname Traefik matches, e.g. myroute.pods.tacc.develop.tapis.io
    auth_path: str        # path under /v3/ of the entity's auth endpoint, e.g. "pods/mypod/auth"
    client_id: str        # service-managed OAuth client id, unique per entity
    label: str            # human label for error messages, e.g. "pod 'mypod'" / "route 'myroute'"

    @property
    def tapis_domain(self) -> str:
        # e.g. myroute.pods.tacc.develop.tapis.io -> tacc.develop.tapis.io
        return self.public_url.split('.pods.', 1)[1]

    @property
    def auth_url(self) -> str:
        return f"https://{self.tapis_domain}/v3/{self.auth_path}"

    @property
    def auth_callback_url(self) -> str:
        return f"https://{self.tapis_domain}/v3/{self.auth_path}/callback"


def check_cross_tenant_request(entity: TapisAuthEntity):
    """Header-level cross-tenant validation (flagged by auth.py NEED-BASEURL handling).
    Returns a 403 JSONResponse when the token's tenant is not granted, else None."""
    tapis_auth_allowed_tenants = get_allowed_tenants_from_permissions(entity.permissions)
    cross_tenant = getattr(g, 'cross_tenant_request', False)
    if cross_tenant:
        token_tenant_id = getattr(g, 'token_tenant_id', None)
        if not check_tapis_auth_tenant_allowed(token_tenant_id, g.request_tenant_id, tapis_auth_allowed_tenants):
            logger.info(f"Cross-tenant request rejected. token_tenant: {token_tenant_id}, entity_tenant: {g.request_tenant_id}, allowed: {tapis_auth_allowed_tenants}")
            return JSONResponse(
                content=f"Cross-tenant auth not allowed. Token tenant '{token_tenant_id}' is not in the permissions for {entity.label} (set via tenant.<tenant_id> permission entries).",
                status_code=403
            )
        logger.info(f"Cross-tenant request allowed. token_tenant: {token_tenant_id}, entity_tenant: {g.request_tenant_id}")
    return None


def run_tapis_auth_check(request: Request, entity: TapisAuthEntity):
    """The forwardAuth /auth endpoint body: validate an attached token (200 + configured
    headers), or bounce browser navigations into the tenant OAuth2 authorize flow."""
    tapis_auth_allowed_tenants = get_allowed_tenants_from_permissions(entity.permissions)

    cross_tenant_rejection = check_cross_tenant_request(entity)
    if cross_tenant_rejection:
        return cross_tenant_rejection

    ## We now want to check if session/headers have a valid Tapis token for the current site/tenant. If so, we can return 200.
    ## Session and headers can both be manually modified, this is where we must validate the token is valid via a call to get_userinfo.
    try:
        authorized, username, roles = validate_token(request)
        # check if user is allowed to access the entity
        tapis_auth_allowed_users = entity.auth_cfg.get("tapis_auth_allowed_users", [])
        if authorized:
            logger.debug(f"User authenticated: {username}")

            # Additional cross-tenant check on the actual token (not just header-level from auth.py).
            # This catches cases where the token in cookies is from a different tenant than the entity.
            # check_tapis_auth_tenant_allowed allows the entity's own tenant automatically.
            token_str = request.cookies.get('X-Tapis-Token') or request.headers.get('X-Tapis-Token') or request.headers.get('x-tapis-token')
            if token_str:
                token_tenant = get_token_tenant_id(token_str)
                if token_tenant and not check_tapis_auth_tenant_allowed(token_tenant, g.request_tenant_id, tapis_auth_allowed_tenants):
                    # Explicit 403 (a raise here would be swallowed by the except below and
                    # the user would fall into the OAuth redirect path with no explanation).
                    logger.info(f"Cross-tenant token rejected for {entity.label}. token_tenant: {token_tenant}, entity_tenant: {g.request_tenant_id}, allowed: {tapis_auth_allowed_tenants}")
                    return JSONResponse(
                        content=f"Pods: token tenant '{token_tenant}' is not allowed for {entity.label}. Entity tenant: '{g.request_tenant_id}'; extra tenants allowed via permissions: {tapis_auth_allowed_tenants}.",
                        status_code=403)

            tapis_auth_headers = build_auth_response_headers(
                auth_cfg=entity.auth_cfg,
                username=username,
                tenant_id=g.request_tenant_id,
                site_id=g.site_id
            )
            if tapis_auth_allowed_users:
                if not check_tapis_auth_allowed(username, tapis_auth_allowed_users, entity.permissions):
                    # Explicit 403 for allowlist rejection — a raise would be logged and
                    # swallowed below, so denied users would be re-sent through the OAuth
                    # flow / told "not authenticated" instead of "authenticated but not allowed".
                    logger.info(f"User '{username}' rejected by tapis_auth_allowed_users for {entity.label}.")
                    return JSONResponse(
                        content=f"Pods: user '{username}' is authenticated but not in the tapis_auth_allowed_users for {entity.label}.",
                        status_code=403)
            return JSONResponse(content=ok("Already authenticated"), status_code=200, headers=tapis_auth_headers)
    except Exception as e:
        logger.debug(f"Authentication failed: {getattr(e, 'detail', None) or e}")

    ## A token was actually attached (header or cookie) but failed validation.
    ## API/XHR callers get an explicit 403 here — bouncing them into the browser
    ## OAuth redirect would just hand them the authorize page HTML. Browser
    ## navigations (e.g. a stale cookie) still fall through to the redirect so
    ## humans re-login seamlessly. Tokenless requests always fall through too:
    ## plenty of traffic lands on traefik that is simply meant to fail, and the
    ## OAuth bounce is the expected failure mode there.
    logger.debug(f"request_info dump: {scrub_headers(request.headers)}, {scrub_cookies(request.cookies)}, {request.query_params}")
    ## Starlette headers are case-insensitive, so this covers every casing.
    token_attached = bool(
        request.headers.get('X-Tapis-Token')
        or request.cookies.get('X-Tapis-Token'))
    if token_attached:
        accept_header = request.headers.get('accept', '')
        is_browser_nav = (
            'text/html' in accept_header
            and request.headers.get('sec-fetch-mode', 'navigate') == 'navigate')
        if not is_browser_nav:
            logger.debug("Token attached but not authenticated; non-browser client. Returning 403.")
            return JSONResponse(
                content="Pods Service tapis_auth - token attached but not valid (expired, wrong tenant, or malformed). Re-authenticate and retry with a valid X-Tapis-Token.",
                status_code=403)
        logger.debug("Token attached but not authenticated; browser navigation. Falling through to OAuth redirect.")

    if not entity.auth_cfg.get("tapis_auth", False):
        return JSONResponse(content=f"{entity.label} does not have tapis_auth configured. Leave or remedy. Initial Auth", status_code=403)

    tapis_tenant = entity.tapis_domain.split('.')[0]
    client_display_name = f"Tapis Pods Service: {entity.label}"
    client_description = f"Tapis Pods Service: {entity.label}"

    logger.debug(f"auth check for {entity.label} - headers: {scrub_headers(request.headers)}, request.cookies: {scrub_cookies(request.cookies)}, tenant_id: {g.request_tenant_id}, derived_tenant_id: {tapis_tenant}, site_id: {g.site_id}")

    td = None
    # Create tapis client or update tapis client if needed
    try:
        logger.debug(f"Creating client_id: {entity.client_id}, tenant: {tapis_tenant}")
        res, td = t.authenticator.create_client(
            client_id = entity.client_id,
            callback_url = entity.auth_callback_url,
            display_name = client_display_name,
            description = client_description,
            _x_tapis_tenant = tapis_tenant,
            _x_tapis_user = "_tapis_pods",
            _tapis_debug = False
        )
    except BadRequestError as e: # Exceptions in 3 shouldn't have e.message (only e.args), but this one does.
        logger.debug(f"Got error creating client: {e.message}")
        if "This change would violate uniqueness constraints" in e.message:
            logger.debug(f"Client already exists, updating client_id: {entity.client_id}, tenant: {tapis_tenant}")
            try:
                res, td = t.authenticator.update_client(
                    client_id = entity.client_id,
                    callback_url = entity.auth_callback_url,
                    display_name = client_display_name,
                    description = client_description,
                    _x_tapis_tenant = tapis_tenant,
                    _x_tapis_user = "_tapis_pods",
                    _tapis_debug = False
                )
                success_msg = f"Client {entity.client_id} updated successfully."
                logger.info(success_msg)
            except Exception as e:
                msg = (f"Error updating client_id: {entity.client_id}. e: {e.args}, e: {e}, dir(e): {dir(e)}")
                logger.warning(msg)
                return JSONResponse(content = msg, status_code = 500)
        else:
            msg = (f"Error creating client_id: {entity.client_id}. e.message: {e.message}, e.request: {e.request}, e.response: {e.response}, tapis_debug = {td}")
            logger.warning(msg)

    oauth2_url = f"https://{entity.tapis_domain}/v3/oauth2/authorize?client_id={entity.client_id}&redirect_uri={entity.auth_callback_url}&response_type=code"
    logger.debug(f"oauth2 url is: {oauth2_url}")
    return RedirectResponse(url=oauth2_url, status_code=302)


def run_tapis_auth_callback(request: Request, entity: TapisAuthEntity):
    """The /auth/callback endpoint body: exchange the OAuth2 code for a token, validate
    it, re-check tenant + allowed users, set token cookies, redirect to the return path."""
    tapis_auth_allowed_tenants = get_allowed_tenants_from_permissions(entity.permissions)

    cross_tenant_rejection = check_cross_tenant_request(entity)
    if cross_tenant_rejection:
        return cross_tenant_rejection

    if not entity.auth_cfg.get("tapis_auth", False):
        return JSONResponse(content=f"{entity.label} does not have tapis_auth configured. Leave or remedy. Callback", status_code=403)

    tapis_domain = entity.tapis_domain
    tapis_tenant = tapis_domain.split('.')[0]

    try:
        res, td = t.authenticator.get_client(
            client_id = entity.client_id,
            _x_tapis_tenant = tapis_tenant,
            _x_tapis_user = "_tapis_pods",
            _tapis_debug = False)
    except Exception as e:
        return JSONResponse(content=f"Error retrieving client: {e}", status_code=500)

    code = request.query_params.get('code')
    if not code:
        raise Exception(f"Error: No code in request; debug: {request.query_params}")
    logger.debug(f"auth callback for {entity.label} - tapis_domain: {tapis_domain}, code: {code}")
    url = f"https://{tapis_domain}/v3/oauth2/tokens"
    data = {
        "code": code,
        "redirect_uri": entity.auth_callback_url,
        "grant_type": "authorization_code",
    }

    try:
        response = requests.post(url, data=data, auth=(entity.client_id, res.client_key), timeout=(3.05, 10))
        response.raise_for_status()
        logger.debug(f"auth callback for {entity.label} token request: HTTP {response.status_code}")
        json_resp = response.json()
        token = json_resp['result']['access_token']['access_token']
    except Exception as e:
        raise Exception(f"Error generating Tapis token; debug: {e}")

    try:
        logger.debug(f"auth callback for {entity.label} - token: {token}")

        authorized, username, roles = validate_token(request, token=token)

        logger.debug(f"auth callback for {entity.label} - username: {username}, tapis_domain: {tapis_domain}")

        # Cross-tenant check on the newly obtained token.
        # check_tapis_auth_tenant_allowed allows the entity's own tenant automatically.
        token_tenant = get_token_tenant_id(token)
        if token_tenant and not check_tapis_auth_tenant_allowed(token_tenant, g.request_tenant_id, tapis_auth_allowed_tenants):
            raise Exception(f"Token tenant '{token_tenant}' not in allowed tenants for {entity.label}. Entity tenant: '{g.request_tenant_id}'. Allowed extra tenants (via permissions): {tapis_auth_allowed_tenants}.")
        if token_tenant and token_tenant != g.request_tenant_id:
            logger.info(f"Cross-tenant token accepted in callback. token_tenant: {token_tenant}, entity_tenant: {g.request_tenant_id}")

        tapis_auth_allowed_users = entity.auth_cfg.get("tapis_auth_allowed_users", [])
        if tapis_auth_allowed_users:
            if not check_tapis_auth_allowed(username, tapis_auth_allowed_users, entity.permissions):
                # Explicit 403 — a raise here lands in the outer except and resurfaces
                # as a misleading "Error setting cookies" exception.
                logger.info(f"User '{username}' rejected by tapis_auth_allowed_users for {entity.label} (callback flow).")
                return JSONResponse(
                    content=f"Pods: user '{username}' is authenticated but not in the tapis_auth_allowed_users for {entity.label}.",
                    status_code=403)

        return_path = entity.auth_cfg.get("tapis_auth_return_path", "/") or "/"
        response = RedirectResponse(url=f"https://{entity.public_url}{return_path}", status_code=302)

        # Setting cookies
        domain = conf.get('COOKIE_DOMAIN', f"{tapis_domain}")
        logger.debug(f"About to set cookies. domain: {domain}, public_url: {entity.public_url}")

        response.set_cookie("X-Tapis-Token", token, domain=entity.public_url, secure=True)
        response.set_cookie("X-Tapis-Token", token, domain=domain, secure=True)

        logger.debug(f"auth callback for {entity.label} last bit, response: {response}, public_url: {entity.public_url}")

        return response
    except Exception as e:
        raise Exception(f"Error setting cookies; debug: {e}")
