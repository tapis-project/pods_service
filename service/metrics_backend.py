"""
Pluggable, OPTIONAL metrics backend for pods_service.

Provides per-pod cpu/mem usage from an external metrics source, selected by
the `metrics_backend` config value:

    "none"           (default) — no backend; every query reports NotConfigured.
    "grafana"        — PromQL via a Grafana datasource proxy (Thanos/Prometheus
                       behind Grafana). Needs grafana_url, grafana_token,
                       grafana_prom_ds_uid, metrics_k8s_namespace.
    "metrics-server" — k8s metrics-server (likely future default) — NOT YET
                       IMPLEMENTED here; stub raises MetricsNotConfigured.

Design rules (hard requirements — keep these invariants when editing):
  * Metrics must NEVER take the service down. Short timeouts (3 s connect /
    5 s read), at most ONE retry, and every failure is caught by the safe
    entry points (`pod_usage_dict`, `bulk_usage_dict`, `backend_health`),
    which degrade to {"unavailable": "<reason>"} — never raise to callers.
    Metrics being down is NORMAL (server changes, maintenance).
  * Frugal: an in-process TTL cache (~45 s) fronts every query; bulk callers
    (health loop, admin views) must use `get_bulk()` — ONE query for all pods
    matching a k8_name pattern — never N per-pod queries.
  * Grafana service-account tokens EXPIRE. On 401/403 the error and the
    admin-health guidance carry TOKEN_GUIDANCE so operators know how to fix it.
  * This module must stay import-light (no kubernetes/stores/g imports) so it
    is unit-testable outside the cluster, and because it is imported by both
    the API path and (potentially) health loops. NEVER read `g` context here —
    callers pass everything explicitly.

cAdvisor label facts (verified against grafana.tacc.cloud Thanos, 2026-07-06):
    container_memory_usage_bytes / container_cpu_usage_seconds_total
    container_label_io_kubernetes_pod_namespace = "tapisdev" (develop)
    container_label_io_kubernetes_pod_name      = "pods-{site}-{tenant}-{pod_id}"
    filter container_label_io_kubernetes_container_name != "" (drops pause rows)
"""
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

import requests

try:
    from tapisservice.logs import get_logger
    logger = get_logger(__name__)
except Exception:  # unit tests run outside the service image
    import logging
    logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# Exact string surfaced in errors + admin health when Grafana
# answers 401/403. Grafana service-account tokens expire; this is expected.
TOKEN_GUIDANCE = (
    "Grafana token invalid or expired - generate a new service-account token "
    "(Grafana -> Administration -> Users and access -> Service accounts) and "
    "update the 'grafana_token' config value."
)

CONNECT_TIMEOUT_S = 3.05   # connect timeout (hard req: 3-5 s, never longer)
READ_TIMEOUT_S = 5.0       # read timeout
MAX_RETRIES = 1            # at most ONE retry on transient failure
CACHE_TTL_S = 45.0         # per-pod / bulk result cache (hard req: 30-60 s)
ERROR_CACHE_TTL_S = 15.0   # failures cached briefly too, so a down backend
                           # isn't hammered by every request in a hot loop
CPU_RATE_WINDOW = "5m"     # rate() window for cpu cores

# Range (time-series / sparkline) queries. History moves slowly, so it is
# cached much longer than instant usage — every dashboard client in a tenant
# shares ONE query_range per (window, step) per TTL.
RANGE_CACHE_TTL_S = 180.0
RANGE_WINDOW_S_DEFAULT = 3600      # 1 h of history
RANGE_STEP_S_DEFAULT = 120         # 30 points/pod at the default window
RANGE_WINDOW_S_MAX = 86400         # clamp: never ask Thanos for > 24 h
RANGE_MAX_POINTS = 400             # clamp: step raised until points <= this


# ── Result / error types ─────────────────────────────────────────────────────

@dataclass
class PodMetrics:
    """One pod's live usage snapshot from the metrics backend."""
    cpu_cores: Optional[float]        # rate of cpu-seconds (cores); None if absent
    mem_bytes: Optional[float]        # working memory bytes; None if absent
    mem_limit_bytes: Optional[float] = None  # spec limit if the backend exposes it
    ts: float = 0.0                   # sample unix timestamp (backend-reported)
    source: str = ""                  # "grafana" | ...

    def to_dict(self) -> dict:
        d = {
            "cpu_cores": round(self.cpu_cores, 4) if self.cpu_cores is not None else None,
            "mem_bytes": int(self.mem_bytes) if self.mem_bytes is not None else None,
            "ts":        self.ts,
            "source":    self.source,
        }
        # Friendly derived units (UI convenience, same info)
        d["cpu_m"] = round(self.cpu_cores * 1000, 1) if self.cpu_cores is not None else None
        d["mem_mb"] = round(self.mem_bytes / (1024 * 1024), 1) if self.mem_bytes is not None else None
        if self.mem_limit_bytes is not None:
            d["mem_limit_bytes"] = int(self.mem_limit_bytes)
        return d


class MetricsError(Exception):
    """Base for all metrics-backend failures. Callers on hot paths must use the
    safe wrappers (pod_usage_dict/bulk_usage_dict) instead of catching this."""


class MetricsNotConfigured(MetricsError):
    """Backend absent/unimplemented — a NORMAL condition, not a fault."""


class MetricsAuthError(MetricsError):
    """Backend answered 401/403 — token invalid/expired. Message carries
    TOKEN_GUIDANCE so it reaches operators verbatim."""


class MetricsUnavailable(MetricsError):
    """Backend configured but unreachable / erroring — also NORMAL (maintenance)."""


# ── TTL cache (in-process, thread-safe) ──────────────────────────────────────

class _TTLCache:
    """Tiny thread-safe TTL cache. Successful results live CACHE_TTL_S;
    failures live ERROR_CACHE_TTL_S so a down backend isn't re-queried
    on every request."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[str, tuple] = {}   # key -> (expires_at, value, is_error)

    def get(self, key: str):
        """Returns (hit, value, is_error)."""
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry and entry[0] > now:
                return True, entry[1], entry[2]
            if entry:
                del self._data[key]
        return False, None, False

    def put(self, key: str, value, is_error: bool = False, ttl: Optional[float] = None):
        if ttl is None:
            ttl = ERROR_CACHE_TTL_S if is_error else CACHE_TTL_S
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value, is_error)

    def clear(self):
        with self._lock:
            self._data.clear()


# ── Providers ────────────────────────────────────────────────────────────────

@dataclass
class _ProviderStatus:
    """Rolling health of the backend, surfaced by GET /pods/admin/health."""
    last_ok_ts: Optional[str] = None      # iso8601 of last successful query
    last_error: Optional[str] = None      # message of most recent failure
    last_error_ts: Optional[str] = None
    guidance: Optional[str] = None        # operator fix-it text (set on 401/403)


class MetricsProvider:
    """Abstract base. Subclasses implement _query_pod/_query_bulk/check_reachable.

    Public entry points (get_pod / get_bulk) add the TTL cache and status
    bookkeeping. They RAISE MetricsError subclasses — API/health-loop callers
    must go through pod_usage_dict()/bulk_usage_dict()/backend_health(), which
    never raise.
    """
    name = "abstract"

    def __init__(self):
        self._cache = _TTLCache()
        self.status = _ProviderStatus()

    # -- to implement -----------------------------------------------------
    def _query_pod(self, k8_name: str) -> PodMetrics:
        raise NotImplementedError

    def _query_bulk(self, k8_name_pattern: str) -> Dict[str, PodMetrics]:
        raise NotImplementedError

    def _query_bulk_range(self, k8_name_pattern: str, window_s: int, step_s: int) -> Dict[str, dict]:
        """History series for every matching pod. PromQL providers override;
        backends without range support surface it as a NORMAL condition."""
        raise MetricsNotConfigured(
            f"metrics backend '{self.name}' does not support history/range queries")

    def check_reachable(self) -> None:
        """Cheap connectivity probe; raise MetricsError on failure."""
        raise NotImplementedError

    # -- shared machinery ---------------------------------------------------
    def _record_ok(self):
        self.status.last_ok_ts = _utcnow_iso()
        self.status.guidance = None

    def _record_error(self, exc: Exception):
        self.status.last_error = str(exc)
        self.status.last_error_ts = _utcnow_iso()
        if isinstance(exc, MetricsAuthError):
            self.status.guidance = TOKEN_GUIDANCE

    def get_pod(self, k8_name: str) -> PodMetrics:
        """Cached single-pod usage. Raises MetricsError on failure."""
        key = f"pod:{k8_name}"
        hit, value, is_error = self._cache.get(key)
        if hit:
            if is_error:
                raise value
            return value
        try:
            result = self._query_pod(k8_name)
        except MetricsError as e:
            self._record_error(e)
            self._cache.put(key, e, is_error=True)
            raise
        self._record_ok()
        self._cache.put(key, result)
        return result

    def get_bulk(self, k8_name_pattern: str) -> Dict[str, PodMetrics]:
        """Cached bulk usage — ONE backend query for every pod whose k8_name
        matches the regex pattern (e.g. 'pods-tacc-.*'). This is what sweeps
        (health loop, admin views) must call — never get_pod() in a loop."""
        key = f"bulk:{k8_name_pattern}"
        hit, value, is_error = self._cache.get(key)
        if hit:
            if is_error:
                raise value
            return value
        try:
            result = self._query_bulk(k8_name_pattern)
        except MetricsError as e:
            self._record_error(e)
            self._cache.put(key, e, is_error=True)
            raise
        self._record_ok()
        self._cache.put(key, result)
        # Warm the per-pod cache from the bulk answer (free single lookups).
        for k8_name, pm in result.items():
            self._cache.put(f"pod:{k8_name}", pm)
        return result

    def get_bulk_range(self, k8_name_pattern: str,
                       window_s: int = RANGE_WINDOW_S_DEFAULT,
                       step_s: int = RANGE_STEP_S_DEFAULT) -> Dict[str, dict]:
        """Cached bulk HISTORY — one query_range for every pod matching the
        pattern, cached RANGE_CACHE_TTL_S. All sparkline consumers (fleet
        dashboard, every per-pod page) share this single backend query.
        Inputs are clamped so a caller can never make Thanos do heavy work."""
        window_s = max(300, min(int(window_s), RANGE_WINDOW_S_MAX))
        step_s = max(30, int(step_s))
        if window_s // step_s > RANGE_MAX_POINTS:
            step_s = window_s // RANGE_MAX_POINTS
        key = f"range:{k8_name_pattern}:{window_s}:{step_s}"
        hit, value, is_error = self._cache.get(key)
        if hit:
            if is_error:
                raise value
            return value
        try:
            result = self._query_bulk_range(k8_name_pattern, window_s, step_s)
        except MetricsError as e:
            self._record_error(e)
            self._cache.put(key, e, is_error=True)
            raise
        self._record_ok()
        self._cache.put(key, result, ttl=RANGE_CACHE_TTL_S)
        return result


class NoneProvider(MetricsProvider):
    """Default when metrics_backend is unset/'none'. Everything says so clearly."""
    name = "none"
    _MSG = ("no metrics backend configured (metrics_backend=none) - cpu/mem "
            "usage disabled; set metrics_backend to 'grafana' "
            "in the service config to enable")

    def _query_pod(self, k8_name):
        raise MetricsNotConfigured(self._MSG)

    def _query_bulk(self, k8_name_pattern):
        raise MetricsNotConfigured(self._MSG)

    def check_reachable(self):
        raise MetricsNotConfigured(self._MSG)


class MetricsServerProvider(MetricsProvider):
    """Stub for the k8s metrics-server backend (likely future default once the
    cluster installs metrics-server). Deliberately unimplemented."""
    name = "metrics-server"
    _MSG = ("metrics_backend='metrics-server' is not implemented yet - the "
            "in-cluster metrics-server integration is planned but not built; "
            "use 'grafana' for now")

    def _query_pod(self, k8_name):
        raise MetricsNotConfigured(self._MSG)

    def _query_bulk(self, k8_name_pattern):
        raise MetricsNotConfigured(self._MSG)

    def check_reachable(self):
        raise MetricsNotConfigured(self._MSG)


class PromQLProvider(MetricsProvider):
    """PromQL-over-HTTP implementation, consumed via the Grafana datasource
    proxy (GrafanaProvider). Kept separate so another PromQL endpoint could
    subclass it later (base URL + auth header are the only variables).

    Queries (cAdvisor series; labels verified against TACC Thanos):
      mem: sum by(pod) (container_memory_usage_bytes{ns, pod, container!=""})
      cpu: sum by(pod) (rate(container_cpu_usage_seconds_total{...}[5m]))
    """
    name = "promql"

    def __init__(self, query_url: str, k8s_namespace: str,
                 headers: Optional[dict] = None, session: Optional[requests.Session] = None):
        super().__init__()
        # query_url is the full .../api/v1/query endpoint; the range endpoint
        # lives beside it.
        self.query_url = query_url
        self.query_range_url = query_url + "_range"
        self.k8s_namespace = k8s_namespace
        self.headers = headers or {}
        self.session = session or requests.Session()

    # -- HTTP ---------------------------------------------------------------
    def _http_query(self, promql: str) -> list:
        """POST one instant query; returns the result vector (list).
        Timeouts 3/5 s, ONE retry on transient network failure only."""
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self.session.post(
                    self.query_url,
                    data={"query": promql},
                    headers=self.headers,
                    timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                )
                if resp.status_code in (401, 403):
                    # Token problems are NOT retried — retrying won't help.
                    raise MetricsAuthError(
                        f"{self.name} backend returned HTTP {resp.status_code}. "
                        + TOKEN_GUIDANCE)
                if resp.status_code != 200:
                    raise MetricsUnavailable(
                        f"{self.name} backend returned HTTP {resp.status_code}: "
                        f"{resp.text[:200]}")
                body = resp.json()
                if body.get("status") != "success":
                    raise MetricsUnavailable(
                        f"{self.name} query failed: {str(body)[:200]}")
                return body.get("data", {}).get("result", [])
            except MetricsError:
                raise
            except (requests.exceptions.RequestException, ValueError) as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    continue   # single retry
        raise MetricsUnavailable(
            f"{self.name} backend unreachable: {type(last_exc).__name__}: {last_exc}")

    def _http_query_range(self, promql: str, start: float, end: float, step_s: int) -> list:
        """POST one range query; returns the result matrix (list). Same
        timeout/retry/auth discipline as _http_query."""
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self.session.post(
                    self.query_range_url,
                    data={"query": promql, "start": start, "end": end, "step": step_s},
                    headers=self.headers,
                    timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                )
                if resp.status_code in (401, 403):
                    raise MetricsAuthError(
                        f"{self.name} backend returned HTTP {resp.status_code}. "
                        + TOKEN_GUIDANCE)
                if resp.status_code != 200:
                    raise MetricsUnavailable(
                        f"{self.name} backend returned HTTP {resp.status_code}: "
                        f"{resp.text[:200]}")
                body = resp.json()
                if body.get("status") != "success":
                    raise MetricsUnavailable(
                        f"{self.name} range query failed: {str(body)[:200]}")
                return body.get("data", {}).get("result", [])
            except MetricsError:
                raise
            except (requests.exceptions.RequestException, ValueError) as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    continue
        raise MetricsUnavailable(
            f"{self.name} backend unreachable: {type(last_exc).__name__}: {last_exc}")

    # -- PromQL construction --------------------------------------------------
    _POD_LABEL = "container_label_io_kubernetes_pod_name"
    _NS_LABEL = "container_label_io_kubernetes_pod_namespace"
    _CONTAINER_LABEL = "container_label_io_kubernetes_container_name"

    def _selector(self, pod_match: str, regex: bool) -> str:
        op = "=~" if regex else "="
        return (f'{{{self._NS_LABEL}="{self.k8s_namespace}",'
                f'{self._POD_LABEL}{op}"{pod_match}",'
                f'{self._CONTAINER_LABEL}!=""}}')

    def _mem_query(self, pod_match: str, regex: bool) -> str:
        return (f'sum by({self._POD_LABEL}) '
                f'(container_memory_usage_bytes{self._selector(pod_match, regex)})')

    def _cpu_query(self, pod_match: str, regex: bool) -> str:
        return (f'sum by({self._POD_LABEL}) '
                f'(rate(container_cpu_usage_seconds_total'
                f'{self._selector(pod_match, regex)}[{CPU_RATE_WINDOW}]))')

    # -- vector parsing --------------------------------------------------------
    def _vector_to_map(self, vector: list) -> Dict[str, tuple]:
        """{pod_name: (value_float, ts_float)} from a PromQL instant vector."""
        out = {}
        for row in vector:
            pod = row.get("metric", {}).get(self._POD_LABEL)
            val = row.get("value")   # [ts, "str_val"]
            if not pod or not val or len(val) != 2:
                continue
            try:
                out[pod] = (float(val[1]), float(val[0]))
            except (TypeError, ValueError):
                continue
        return out

    # -- provider interface ----------------------------------------------------
    def _query_pod(self, k8_name: str) -> PodMetrics:
        mem = self._vector_to_map(self._http_query(self._mem_query(k8_name, regex=False)))
        cpu = self._vector_to_map(self._http_query(self._cpu_query(k8_name, regex=False)))
        if k8_name not in mem and k8_name not in cpu:
            raise MetricsUnavailable(
                f"no series found for pod '{k8_name}' in namespace "
                f"'{self.k8s_namespace}' (pod not running, or wrong "
                f"metrics_k8s_namespace config)")
        mem_v = mem.get(k8_name)
        cpu_v = cpu.get(k8_name)
        return PodMetrics(
            cpu_cores=cpu_v[0] if cpu_v else None,
            mem_bytes=mem_v[0] if mem_v else None,
            ts=(mem_v or cpu_v)[1],
            source=self.name,
        )

    def _matrix_to_map(self, matrix: list, value_fn) -> Dict[str, list]:
        """{pod_name: [[ts, value], ...]} from a PromQL range matrix."""
        out: Dict[str, list] = {}
        for row in matrix:
            pod = row.get("metric", {}).get(self._POD_LABEL)
            values = row.get("values") or []
            if not pod:
                continue
            series = []
            for pair in values:
                try:
                    series.append([float(pair[0]), value_fn(float(pair[1]))])
                except (TypeError, ValueError, IndexError):
                    continue
            if series:
                out[pod] = series
        return out

    def _query_bulk_range(self, k8_name_pattern: str, window_s: int, step_s: int) -> Dict[str, dict]:
        end = time.time()
        start = end - window_s
        mem = self._matrix_to_map(
            self._http_query_range(self._mem_query(k8_name_pattern, regex=True), start, end, step_s),
            value_fn=lambda v: int(v))
        cpu = self._matrix_to_map(
            self._http_query_range(self._cpu_query(k8_name_pattern, regex=True), start, end, step_s),
            value_fn=lambda v: round(v, 4))
        out: Dict[str, dict] = {}
        for pod in set(mem) | set(cpu):
            out[pod] = {"cpu": cpu.get(pod, []), "mem": mem.get(pod, [])}
        return out

    def _query_bulk(self, k8_name_pattern: str) -> Dict[str, PodMetrics]:
        mem = self._vector_to_map(self._http_query(self._mem_query(k8_name_pattern, regex=True)))
        cpu = self._vector_to_map(self._http_query(self._cpu_query(k8_name_pattern, regex=True)))
        out: Dict[str, PodMetrics] = {}
        for pod in set(mem) | set(cpu):
            mem_v, cpu_v = mem.get(pod), cpu.get(pod)
            out[pod] = PodMetrics(
                cpu_cores=cpu_v[0] if cpu_v else None,
                mem_bytes=mem_v[0] if mem_v else None,
                ts=(mem_v or cpu_v)[1],
                source=self.name,
            )
        return out

    def check_reachable(self) -> None:
        """Cheapest possible probe: `vector(1)`. Raises on failure."""
        key = "check:reachable"
        hit, value, is_error = self._cache.get(key)
        if hit:
            if is_error:
                raise value
            return
        try:
            self._http_query("vector(1)")
        except MetricsError as e:
            self._record_error(e)
            self._cache.put(key, e, is_error=True)
            raise
        self._record_ok()
        self._cache.put(key, True)


class GrafanaProvider(PromQLProvider):
    """PromQL through the Grafana datasource proxy (Thanos behind Grafana).
    Verified path: {grafana_url}/api/datasources/proxy/uid/{ds_uid}/api/v1/query
    with 'Authorization: Bearer <service-account token>'.

    NOTE: Grafana service-account tokens EXPIRE. 401/403 from any query maps to
    MetricsAuthError carrying TOKEN_GUIDANCE.
    """
    name = "grafana"

    def __init__(self, grafana_url: str, token: str, ds_uid: str, k8s_namespace: str,
                 session: Optional[requests.Session] = None):
        query_url = (f"{grafana_url.rstrip('/')}/api/datasources/proxy/uid/"
                     f"{ds_uid}/api/v1/query")
        super().__init__(query_url, k8s_namespace,
                         headers={"Authorization": f"Bearer {token}"},
                         session=session)
        # Kept for explore_hint() — lets UIs build Grafana Explore deep-links.
        self.grafana_url = grafana_url.rstrip('/')
        self.ds_uid = ds_uid


# ── Provider factory (config-driven singleton) ───────────────────────────────

_provider_lock = threading.Lock()
_provider: Optional[MetricsProvider] = None


def get_provider() -> MetricsProvider:
    """Singleton provider built from service config. NEVER raises — a bad or
    missing config yields a NoneProvider whose errors explain what's wrong."""
    global _provider
    if _provider is not None:
        return _provider
    with _provider_lock:
        if _provider is not None:
            return _provider
        _provider = _build_provider_from_conf()
        return _provider


def reset_provider():
    """Test hook / config-reload hook."""
    global _provider
    with _provider_lock:
        _provider = None


def _build_provider_from_conf() -> MetricsProvider:
    try:
        from tapisservice.config import conf
        backend = (conf.get("metrics_backend", "none") or "none").strip().lower()
        namespace = conf.get("metrics_k8s_namespace", "") or ""
        if backend == "grafana":
            url = conf.get("grafana_url", "") or ""
            token = conf.get("grafana_token", "") or ""
            ds_uid = conf.get("grafana_prom_ds_uid", "") or ""
            missing = [k for k, v in [("grafana_url", url), ("grafana_token", token),
                                      ("grafana_prom_ds_uid", ds_uid),
                                      ("metrics_k8s_namespace", namespace)] if not v]
            if missing:
                logger.warning(f"metrics_backend=grafana but missing config: {missing}; disabling metrics")
                return _misconfigured(f"metrics_backend=grafana missing config value(s): {', '.join(missing)}")
            return GrafanaProvider(url, token, ds_uid, namespace)
        if backend == "metrics-server":
            return MetricsServerProvider()
        if backend not in ("none", ""):
            logger.warning(f"unknown metrics_backend '{backend}'; disabling metrics")
            return _misconfigured(f"unknown metrics_backend '{backend}' (expected none|grafana|metrics-server)")
        return NoneProvider()
    except Exception as e:
        # Config layer itself failed — metrics must never take the service down.
        logger.warning(f"metrics backend config error: {e}; disabling metrics")
        return _misconfigured(f"metrics backend config error: {e}")


def _misconfigured(msg: str) -> MetricsProvider:
    p = NoneProvider()
    p._MSG = msg   # instance override — clearer than the generic 'none' text
    return p


# ── Safe entry points (what routes / health loop actually call) ──────────────

def pod_usage_dict(k8_name: str, provider: Optional[MetricsProvider] = None) -> dict:
    """cpu/mem usage for one pod as a plain dict. NEVER raises: on any failure
    returns {"unavailable": "<reason>"} — HTTP callers keep returning 200."""
    provider = provider or get_provider()
    try:
        return provider.get_pod(k8_name).to_dict()
    except MetricsError as e:
        return {"unavailable": str(e)}
    except Exception as e:   # belt and braces — metrics never crash a caller
        logger.warning(f"pod_usage_dict({k8_name}) unexpected error: {e}")
        return {"unavailable": f"unexpected metrics error: {e}"}


def bulk_usage_dict(k8_name_pattern: str, provider: Optional[MetricsProvider] = None) -> dict:
    """Bulk usage for all pods matching the regex pattern (ONE backend query —
    this is the sweep/health-loop entry point). NEVER raises.
    Returns {"pods": {k8_name: usage}} or {"unavailable": "<reason>"}."""
    provider = provider or get_provider()
    try:
        result = provider.get_bulk(k8_name_pattern)
        return {"pods": {name: pm.to_dict() for name, pm in result.items()}}
    except MetricsError as e:
        return {"unavailable": str(e)}
    except Exception as e:
        logger.warning(f"bulk_usage_dict({k8_name_pattern}) unexpected error: {e}")
        return {"unavailable": f"unexpected metrics error: {e}"}


def bulk_range_dict(k8_name_pattern: str,
                    window_s: int = RANGE_WINDOW_S_DEFAULT,
                    step_s: int = RANGE_STEP_S_DEFAULT,
                    provider: Optional[MetricsProvider] = None) -> dict:
    """History series for all pods matching the regex pattern (ONE backend
    range query, cached RANGE_CACHE_TTL_S). NEVER raises.
    Returns {"pods": {k8_name: {"cpu": [[ts, cores]...], "mem": [[ts, bytes]...]}},
             "window_s": int, "step_s": int} or {"unavailable": "<reason>"}."""
    provider = provider or get_provider()
    try:
        # Re-clamp here so the reported window/step match what was queried.
        window_s = max(300, min(int(window_s), RANGE_WINDOW_S_MAX))
        step_s = max(30, int(step_s))
        if window_s // step_s > RANGE_MAX_POINTS:
            step_s = window_s // RANGE_MAX_POINTS
        result = provider.get_bulk_range(k8_name_pattern, window_s, step_s)
        return {"pods": result, "window_s": window_s, "step_s": step_s}
    except MetricsError as e:
        return {"unavailable": str(e)}
    except Exception as e:
        logger.warning(f"bulk_range_dict({k8_name_pattern}) unexpected error: {e}")
        return {"unavailable": f"unexpected metrics error: {e}"}


def backend_health(provider: Optional[MetricsProvider] = None) -> dict:
    """Status block for GET /pods/admin/health. NEVER raises.

    Shape: {backend, configured, reachable, last_ok_ts, last_error, guidance}
    - guidance carries TOKEN_GUIDANCE when the backend answered 401/403.
    - an unconfigured backend is configured=False, reachable=False and is a
      NORMAL state, not an error.
    """
    provider = provider or get_provider()
    configured = not isinstance(provider, (NoneProvider, MetricsServerProvider))
    block = {
        "backend":    provider.name,
        "configured": configured,
        "reachable":  False,
        "last_ok_ts": provider.status.last_ok_ts,
        "last_error": provider.status.last_error,
        "guidance":   provider.status.guidance,
    }
    if not configured:
        try:
            provider.check_reachable()
        except MetricsError as e:
            block["last_error"] = str(e)
        return block
    try:
        provider.check_reachable()
        block["reachable"] = True
        block["last_ok_ts"] = provider.status.last_ok_ts
    except MetricsAuthError as e:
        block["last_error"] = str(e)
        block["guidance"] = TOKEN_GUIDANCE
    except MetricsError as e:
        block["last_error"] = str(e)
    except Exception as e:
        block["last_error"] = f"unexpected metrics error: {e}"
    block["last_error_ts"] = provider.status.last_error_ts
    return block


def explore_hint(provider: Optional[MetricsProvider] = None) -> Optional[dict]:
    """Grafana Explore deep-link ingredients for UIs (no secrets — the token
    never leaves the service; Grafana enforces its own login on the link).
    Returns None unless the grafana backend is configured."""
    provider = provider or get_provider()
    if not isinstance(provider, GrafanaProvider):
        return None
    return {
        "grafana_url": provider.grafana_url,
        "ds_uid":      provider.ds_uid,
        "namespace":   provider.k8s_namespace,
        "pod_label":   PromQLProvider._POD_LABEL,
        "ns_label":    PromQLProvider._NS_LABEL,
        "container_label": PromQLProvider._CONTAINER_LABEL,
        "cpu_rate_window": CPU_RATE_WINDOW,
    }


# ── util ─────────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
