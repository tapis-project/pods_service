"""
Utilities for parsing Traefik JSON access logs and mapping them to pod traffic records.

Traefik access log JSON keys (when --accesslog.format=json):
  "time"            ISO8601 timestamp
  "RequestMethod"   HTTP method
  "RequestPath"     Request path + query (sensitive query-param values masked before storage)
  "OriginStatus"    Upstream HTTP status code
  "Duration"        Request duration in nanoseconds (int)
  "ClientHost"      Source IP (may include port: "1.2.3.4:5678")
  "RouterName"      e.g. "pods-tacc-tacc-mypod@file"
  "entryPointName"  e.g. "web"
  "request_X-Tapis-User"   value of X-Tapis-User header (when accesslog.fields.headers configured)
  "request_X-Tapis-Token"  redacted or absent
"""
import json
import re
from datetime import datetime, timezone
from typing import Optional

from tapisservice.logs import get_logger

logger = get_logger(__name__)

# Router naming convention in Traefik access logs:
#   pods-{site}-{tenant}-{pod_id}@{entrypoint}@file
# Traefik appends @{entrypoint} (e.g. @http, @https, @web) before @file.
# We must strip both suffixes to recover the bare pod_id.
_ROUTER_RE = re.compile(
    r'^pods-(?P<site>[^-]+)-(?P<tenant>[^-]+)-(?P<pod_id>.+?)(?:@\w+)?(?:@file)?$'
)


def _clean_pod_id(raw_pod_id: str) -> str:
    """Strip any trailing @{word} entrypoint suffix Traefik appends to router names."""
    return raw_pod_id.split('@')[0] if '@' in raw_pod_id else raw_pod_id

# Secret hygiene for persisted traffic. Traefik is configured with
# accesslog headers defaultmode=keep, so anonymous requests carry live
# credentials — most notably the access-gate session cookie and the
# ?access=<secret> link-redemption param. These sane defaults deny-list the
# obvious credential carriers before anything is written to traffic_logs.
# Matched case-insensitively against the bare header/param name.
# WAITLIST: make both sets configurable — a tenant-level default plus per-pod
# owner overrides — so operators can opt into (or further restrict) what gets
# captured, instead of this fixed list.
_SENSITIVE_HEADERS = frozenset({
    'x-tapis-token',
    'cookie',
    'set-cookie',
    'authorization',
    'proxy-authorization',
})
_SENSITIVE_QUERY_PARAMS = frozenset({
    'access', 'token', 'secret', 'password', 'apikey', 'api_key', 'jwt', 'key',
})


def redact_path(path: str) -> str:
    """Mask sensitive query-param values in a request path before storage.
    Traefik's RequestPath includes the query string, and the access gate shares
    link secrets as ``?access=<secret>``. Path and non-sensitive params are kept
    for observability; only flagged param values become ``REDACTED``."""
    if not path or '?' not in path:
        return path
    base, _, query = path.partition('?')
    kept = []
    for pair in query.split('&'):
        if not pair:
            continue
        name, sep, _val = pair.partition('=')
        if sep and name.lower() in _SENSITIVE_QUERY_PARAMS:
            kept.append(f"{name}=REDACTED")
        else:
            kept.append(pair)
    return f"{base}?{'&'.join(kept)}" if kept else base


def parse_traefik_access_logs(raw: str) -> list[dict]:
    """Parse newline-separated JSON Traefik access log lines into structured dicts.

    Returns a list of parsed entry dicts; unparseable lines are silently skipped.
    """
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Must have at minimum a timestamp and router to be useful
        if 'time' not in obj or 'RouterName' not in obj:
            continue
        entries.append(obj)
    return entries


def extract_pod_id_from_router(router_name: str) -> Optional[str]:
    """Extract pod_id from a Traefik router name.

    Router names in access logs follow: pods-{site}-{tenant}-{pod_id}@{entrypoint}@file
    Returns the bare pod_id (no @entrypoint suffix), or None if no match.
    """
    m = _ROUTER_RE.match(router_name or '')
    return _clean_pod_id(m.group('pod_id')) if m else None


def extract_tenant_site_from_router(router_name: str) -> tuple[Optional[str], Optional[str]]:
    """Return (tenant_id, site_id) from a Traefik router name, or (None, None)."""
    m = _ROUTER_RE.match(router_name or '')
    if not m:
        return None, None
    return m.group('tenant'), m.group('site')


def parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse Traefik's ISO8601 timestamp to a UTC datetime."""
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def entry_to_traffic_record(entry: dict) -> Optional[dict]:
    """Convert a parsed Traefik log entry dict to a TrafficLog field dict.

    Returns None if required fields are missing or router doesn't map to a pod.
    """
    router_name = entry.get('RouterName', '')
    pod_id = extract_pod_id_from_router(router_name)
    if not pod_id:
        return None

    tenant_id, site_id = extract_tenant_site_from_router(router_name)
    if not tenant_id or not site_id:
        return None

    ts = parse_ts(entry.get('time', ''))
    if not ts:
        return None

    # Duration in Traefik JSON is nanoseconds (int)
    duration_ns = entry.get('Duration', 0)
    duration_ms = round(duration_ns / 1_000_000, 3) if duration_ns else 0.0

    # Source IP — may include port
    client_host = entry.get('ClientHost', '')
    source_ip = client_host.rsplit(':', 1)[0] if ':' in client_host else client_host

    # User identity: X-Tapis-User header injected by tapis_auth forwardAuth, or None.
    username = entry.get('request_X-Tapis-User') or None

    # Access-gate visitors aren't Tapis users, so they have no X-Tapis-User. The gate
    # forwardAuth stamps X-Tapis-Gate-Code with the code's label; surface it as a
    # namespaced "gate:<label>" username so gated traffic is attributable in the table.
    if not username:
        gate_code = entry.get('request_X-Tapis-Gate-Code')
        if gate_code:
            username = f"gate:{gate_code}"

    # Collect remaining headers as raw_headers when no tapis_auth user present
    raw_headers: Optional[dict] = None
    if not username:
        collected = {}
        for k, v in entry.items():
            if not k.startswith('request_'):
                continue
            name = k.removeprefix('request_')
            if name.lower() in _SENSITIVE_HEADERS:
                continue
            collected[name] = v
        raw_headers = collected or None

    return {
        'pod_id': pod_id,
        'tenant_id': tenant_id,
        'site_id': site_id,
        'ts': ts,
        'method': entry.get('RequestMethod', ''),
        'path': redact_path(entry.get('RequestPath', '')),
        'status_code': entry.get('OriginStatus', 0),
        'duration_ms': duration_ms,
        'source_ip': source_ip,
        'username': username,
        'entry_point': entry.get('entryPointName', ''),
        'router_name': router_name,
        'raw_headers': raw_headers,
    }
