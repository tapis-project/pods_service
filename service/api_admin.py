"""
Admin-only diagnostic endpoints for the Pods service.
"""
import time
import json
import pika
from datetime import datetime, timezone

from fastapi import APIRouter
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.logs import get_logger
from tapisservice.config import conf

from stores import pg_store, get_site_rabbitmq_uri, SITE_TENANT_DICT
from kubernetes import client as k8s_client
from kubernetes_utils import (
    get_traefik_pod_name, get_traefik_logs, NAMESPACE, k8,
    get_all_pod_k8s_metrics,
)

_auth_v1 = k8s_client.AuthorizationV1Api()

# Every (verb, resource, subresource) the pods-service service account needs.
# A missing permission here is likely to break pod spawning or volume mounting.
_REQUIRED_PERMISSIONS = [
    # Pods
    ("get",    "pods",                  None),
    ("list",   "pods",                  None),
    ("watch",  "pods",                  None),
    ("create", "pods",                  None),
    ("delete", "pods",                  None),
    ("get",    "pods",                  "log"),
    ("get",    "pods",                  "exec"),
    ("create", "pods",                  "exec"),
    # Services
    ("get",    "services",              None),
    ("list",   "services",              None),
    ("create", "services",              None),
    ("delete", "services",              None),
    # ConfigMaps — update/patch required for ephemeral config volumes
    ("get",    "configmaps",            None),
    ("list",   "configmaps",            None),
    ("create", "configmaps",            None),
    ("update", "configmaps",            None),
    ("patch",  "configmaps",            None),
    ("delete", "configmaps",            None),
    # PVCs
    ("get",    "persistentvolumeclaims", None),
    ("list",   "persistentvolumeclaims", None),
    ("create", "persistentvolumeclaims", None),
    ("delete", "persistentvolumeclaims", None),
    # Events (read-only)
    ("get",    "events",                None),
    ("list",   "events",                None),
    # Namespaces (read)
    ("get",    "namespaces",            None),
    ("list",   "namespaces",            None),
]
from traffic_utils import parse_traefik_access_logs, entry_to_traffic_record

logger = get_logger(__name__)
router = APIRouter()


def _check_database() -> dict:
    """Probe every tenant pg_store with a trivial query."""
    from sqlalchemy import text
    results = []
    failed = 0
    for site, tenants in pg_store.items():
        for tenant, store in tenants.items():
            label = f"{site}/{tenant}"
            try:
                store.run("execute", text("SELECT 1"))
                results.append({"store": label, "ok": True})
            except Exception as e:
                results.append({"store": label, "ok": False, "error": str(e)})
                failed += 1
    total = len(results)
    if total == 0:
        return {"status": "error", "message": "No pg_store entries found — stores never initialized.", "stores": []}
    if failed == 0:
        return {"status": "ok", "message": f"All {total} tenant database(s) reachable.", "stores": results}
    return {"status": "error", "message": f"{failed}/{total} tenant database(s) unreachable.", "stores": results}


def _check_rabbitmq() -> dict:
    """Try a short-lived blocking connection to each site's RabbitMQ vhost."""
    results = []
    failed = 0
    for site in SITE_TENANT_DICT:
        label = f"site={site}"
        try:
            uri = get_site_rabbitmq_uri(site)
            params = pika.URLParameters(uri)
            params.socket_timeout = 3
            conn = pika.BlockingConnection(params)
            conn.close()
            results.append({"site": site, "ok": True})
        except Exception as e:
            results.append({"site": site, "ok": False, "error": str(e)})
            failed += 1
    total = len(results)
    if total == 0:
        return {"status": "error", "message": "No sites configured.", "sites": []}
    if failed == 0:
        return {"status": "ok", "message": f"All {total} RabbitMQ vhost(s) reachable.", "sites": results}
    return {"status": "error", "message": f"{failed}/{total} RabbitMQ vhost(s) unreachable.", "sites": results}


def _check_traefik() -> dict:
    """Check Traefik pod status and whether access logging is configured correctly."""
    try:
        pods = k8.list_namespaced_pod(namespace=NAMESPACE, label_selector="app=pods-traefik")
    except Exception as e:
        return {"status": "error", "message": f"Cannot reach Kubernetes API: {e}"}

    if not pods.items:
        return {"status": "error", "message": "No pod found with label app=pods-traefik."}

    pod = pods.items[0]
    pod_name = pod.metadata.name
    phase = pod.status.phase if pod.status else "Unknown"

    # Pull args from the container spec to check accesslog flags
    args = []
    try:
        container = pod.spec.containers[0]
        args = container.args or []
    except Exception:
        pass

    has_accesslog = "--accesslog=true" in args or "--accesslog" in args
    has_json = "--accesslog.format=json" in args
    has_user_header = any("X-Tapis-User" in a for a in args)

    flag_issues = []
    if not has_accesslog:
        flag_issues.append("--accesslog=true missing")
    if not has_json:
        flag_issues.append("--accesslog.format=json missing")
    if not has_user_header:
        flag_issues.append("--accesslog.fields.headers.names.X-Tapis-User=keep missing")

    if phase != "Running":
        status = "error"
        msg = f"Pod '{pod_name}' is in phase '{phase}', not Running."
    elif flag_issues:
        status = "warning"
        msg = f"Pod '{pod_name}' is Running but access log flags not set: {', '.join(flag_issues)}. Traffic collection will not work."
    else:
        status = "ok"
        msg = f"Pod '{pod_name}' is Running with correct access log flags."

    # Sample recent logs to see if JSON access log lines are flowing
    raw = get_traefik_logs(lines=50)
    raw_lines = raw.splitlines() if raw else []
    entries = parse_traefik_access_logs(raw) if raw else []
    pod_entries = [e for e in entries if entry_to_traffic_record(e) is not None]
    router_names = list({e.get("RouterName", "") for e in entries if e.get("RouterName")})

    log_sample = {
        "raw_lines_fetched": len(raw_lines),
        "json_parseable_lines": len(entries),
        "pod_router_lines": len(pod_entries),
        "router_names_seen": router_names[:10],
        "raw_sample": raw_lines[:5] if raw_lines else [],
    }
    if raw_lines and len(entries) == 0:
        log_sample["warning"] = "Traefik stdout has lines but none are valid JSON — accesslog.format=json may not be set on the running pod."
    elif entries and len(pod_entries) == 0:
        log_sample["warning"] = (
            f"JSON lines found but none match pods-{{site}}-{{tenant}}-{{pod_id}}@file router pattern. "
            f"Router names seen: {router_names[:5]}"
        )

    return {
        "status": status,
        "message": msg,
        "pod_name": pod_name,
        "phase": phase,
        "accesslog_flags": {
            "accesslog_enabled": has_accesslog,
            "format_json": has_json,
            "x_tapis_user_header": has_user_header,
        },
        "recent_log_sample": log_sample,
    }


def _check_traffic_ingestion() -> dict:
    """
    Check traffic ingestion health by querying the traffic_logs table directly.
    The health loop runs in a separate process, so in-memory globals are not shared.
    """
    from sqlmodel import select, func
    from models_traffic import TrafficLog

    per_pod = []
    total_rows = 0
    newest_ts = None
    errors = []

    for site, tenants in pg_store.items():
        for tenant, store in tenants.items():
            if tenant in ("siteadmintable", "defaulttables"):
                continue
            try:
                # Most recent row per pod in this tenant
                stmt = (
                    select(TrafficLog.pod_id, func.max(TrafficLog.ts).label("latest_ts"), func.count().label("cnt"))
                    .where(TrafficLog.tenant_id == tenant, TrafficLog.site_id == site)
                    .group_by(TrafficLog.pod_id)
                    .order_by(func.max(TrafficLog.ts).desc())
                )
                rows = store.run("execute", stmt, all=True)
                for row in rows:
                    total_rows += row.cnt
                    ts = row.latest_ts
                    if newest_ts is None or ts > newest_ts:
                        newest_ts = ts
                    age_s = (datetime.utcnow() - ts).total_seconds()
                    per_pod.append({
                        "site": site,
                        "tenant": tenant,
                        "pod_id": row.pod_id,
                        "total_rows": row.cnt,
                        "latest_ts": ts.isoformat(),
                        "age_seconds": round(age_s, 1),
                    })
            except Exception as e:
                errors.append(f"{site}/{tenant}: {e}")

    if errors:
        return {
            "status": "error",
            "message": f"DB query errors: {errors}",
            "per_pod": per_pod,
        }

    if not per_pod:
        return {
            "status": "warning",
            "message": "No traffic_logs rows found in any tenant. Health loop may not be running, or Traefik access logs are not being parsed.",
            "per_pod": [],
            "total_rows": 0,
        }

    newest_age = (datetime.utcnow() - newest_ts).total_seconds() if newest_ts else None
    if newest_age is not None and newest_age > 120:
        status = "warning"
        msg = f"Most recent traffic row is {newest_age:.0f}s old — ingestion may have stalled."
    else:
        status = "ok"
        msg = f"Ingesting traffic for {len(per_pod)} pod(s), {total_rows} total rows. Newest entry {newest_age:.0f}s ago."

    return {
        "status": status,
        "message": msg,
        "total_rows": total_rows,
        "per_pod": per_pod,
    }


def _check_rbac() -> dict:
    """
    Use SelfSubjectAccessReview to verify that the pods-service service account
    has every permission it requires. Missing permissions (especially
    configmaps:update/patch) cause silent failures like ephemeral config volumes
    not being mounted after a pod restart.
    """
    results = []
    failed = []

    for verb, resource, subresource in _REQUIRED_PERMISSIONS:
        label = f"{verb} {resource}" + (f"/{subresource}" if subresource else "")
        try:
            attrs = k8s_client.V1ResourceAttributes(
                namespace=NAMESPACE,
                verb=verb,
                resource=resource,
                subresource=subresource or "",
            )
            review = k8s_client.V1SelfSubjectAccessReview(
                spec=k8s_client.V1SelfSubjectAccessReviewSpec(
                    resource_attributes=attrs
                )
            )
            resp = _auth_v1.create_self_subject_access_review(body=review)
            allowed = resp.status.allowed
            reason = resp.status.reason or ""
            results.append({"permission": label, "allowed": allowed, "reason": reason})
            if not allowed:
                failed.append(label)
        except Exception as e:
            results.append({"permission": label, "allowed": None, "error": str(e)})
            failed.append(f"{label} (check error)")

    if failed:
        return {
            "status": "error",
            "message": f"Missing {len(failed)} permission(s): {', '.join(failed)}",
            "namespace": NAMESPACE,
            "permissions": results,
            "failed": failed,
        }
    return {
        "status": "ok",
        "message": f"All {len(results)} required permission(s) granted.",
        "namespace": NAMESPACE,
        "permissions": results,
        "failed": [],
    }


@router.get(
    "/pods/admin/debug-traffic",
    tags=["Admin"],
    summary="admin_debug_traffic",
    operation_id="admin_debug_traffic",
)
async def admin_debug_traffic():
    """
    Admin-only. Runs the full Traefik → traffic_logs ingestion pipeline step-by-step
    from this pod and returns every intermediate result so you can pinpoint exactly
    where ingestion is failing, without needing to tail the health-loop logs.
    """
    from traffic_utils import _ROUTER_RE

    steps = {}

    # Step 1: fetch raw logs
    raw = get_traefik_logs(lines=100)
    raw_lines = raw.splitlines() if raw else []
    steps["1_raw_fetch"] = {
        "traefik_pod": get_traefik_pod_name() or "(not found)",
        "raw_lines_fetched": len(raw_lines),
        "first_5_lines": raw_lines[:5],
    }
    if not raw_lines:
        return ok(result={"steps": steps, "conclusion": "STOP: get_traefik_logs returned empty."})

    # Step 2: JSON parse
    entries = parse_traefik_access_logs(raw)
    steps["2_json_parse"] = {
        "json_parseable": len(entries),
        "skipped_non_json": len(raw_lines) - len(entries),
        "sample_entry": entries[0] if entries else None,
    }
    if not entries:
        return ok(result={"steps": steps, "conclusion": "STOP: No JSON-parseable lines. Traefik accesslog.format=json may not be active on the running pod."})

    # Step 3: router matching
    router_names = list({e.get("RouterName", "") for e in entries if e.get("RouterName")})
    matched = [e for e in entries if entry_to_traffic_record(e) is not None]
    unmatched_routers = list({e.get("RouterName", "") for e in entries if entry_to_traffic_record(e) is None and e.get("RouterName")})
    steps["3_router_match"] = {
        "all_router_names": router_names[:20],
        "matched_pod_entries": len(matched),
        "unmatched_entries": len(entries) - len(matched),
        "unmatched_router_samples": unmatched_routers[:10],
        "router_regex": _ROUTER_RE.pattern,
        "sample_matched_record": entry_to_traffic_record(matched[0]) if matched else None,
    }
    if not matched:
        return ok(result={"steps": steps, "conclusion": f"STOP: No entries matched router pattern. Router names seen: {router_names[:10]}"})

    # Step 4: group by store
    by_store: dict = {}
    for entry in matched:
        rec = entry_to_traffic_record(entry)
        key = f"{rec['site_id']}/{rec['tenant_id']}"
        by_store.setdefault(key, 0)
        by_store[key] += 1

    available_stores = {f"{site}/{tenant}" for site, tenants in pg_store.items() for tenant in tenants}
    missing_stores = [k for k in by_store if k not in available_stores]
    steps["4_store_lookup"] = {
        "records_by_store": by_store,
        "available_pg_stores": sorted(available_stores),
        "missing_stores": missing_stores,
    }
    if missing_stores:
        return ok(result={"steps": steps, "conclusion": f"STOP: Records reference stores not in pg_store: {missing_stores}"})\

    return ok(result={"steps": steps, "conclusion": "Pipeline looks healthy — all steps passed. If DB still empty, check health-loop pod logs for insert errors (validator issue likely fixed by latest deploy)."})


def _check_certs() -> dict:
    """
    Aggregate per-domain TLS certificate (Let's Encrypt/ACME) state across all AVAILABLE
    pods. The health loop records cert_ready/cert_state on each http networking entry, so
    this just rolls those up — no extra probing. Useful for answering "is ACME actually
    issuing certs in this environment?" (e.g. it won't be, locally), and is intentionally
    capped at 'warning' so an environment without ACME doesn't turn overall health red.
    """
    from models_pods import Pod
    from codes import AVAILABLE

    def _parse(v):
        """Parse a UTC ISO timestamp (datetime or str) → datetime, or None."""
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v).replace("Z", ""))
        except Exception:
            return None

    def _secs(a, b):
        """Seconds between two timestamps (a - b), rounded; None if either missing/negative."""
        if not a or not b:
            return None
        d = (a - b).total_seconds()
        return round(d, 1) if d >= 0 else None

    enabled = bool(getattr(conf, 'cert_splash_enabled', True))
    counts = {"ready": 0, "provisioning": 0, "failed": 0, "unknown": 0}
    sampled = []
    errors = []
    prov_secs_all = []      # cert issuance duration (provisioning_started → ready)
    from_create_all = []    # pod create → cert ready

    for site, tenants in SITE_TENANT_DICT.items():
        for tenant in tenants:
            if tenant in ("siteadmintable", "defaulttables"):
                continue
            try:
                pods = Pod.db_get_all(tenant=tenant, site=site)
            except Exception as e:
                errors.append(f"{site}/{tenant}: {e}")
                continue
            for pod in pods:
                if pod.status != AVAILABLE:
                    continue
                created_at = _parse(getattr(pod, 'creation_ts', None))
                available_at = _parse(getattr(pod, 'start_instance_ts', None))
                for net_name, net in (pod.networking or {}).items():
                    nd = net if isinstance(net, dict) else (net.dict() if hasattr(net, 'dict') else dict(net))
                    if nd.get('protocol') != 'http' or not nd.get('url'):
                        continue
                    ready = bool(nd.get('cert_ready'))
                    state = nd.get('cert_state') or ''
                    if ready or state == 'ready':
                        key = 'ready'
                    elif state in ('provisioning', 'failed'):
                        key = state
                    else:
                        key = 'unknown'
                    counts[key] += 1

                    # Timings (only meaningful once the cert is ready and timestamps exist)
                    prov_started = _parse(nd.get('cert_provisioning_started_at'))
                    cert_ready_at = _parse(nd.get('cert_ready_at'))
                    provisioning_seconds = _secs(cert_ready_at, prov_started)
                    from_available_seconds = _secs(cert_ready_at, available_at)
                    from_create_seconds = _secs(cert_ready_at, created_at)
                    if provisioning_seconds is not None:
                        prov_secs_all.append(provisioning_seconds)
                    if from_create_seconds is not None:
                        from_create_all.append(from_create_seconds)

                    if len(sampled) < 30:
                        sampled.append({
                            "site": site, "tenant": tenant, "pod_id": pod.pod_id,
                            "net": net_name, "url": nd.get('url'),
                            "cert_state": state or ('ready' if ready else 'unknown'),
                            "cert_ready": ready,
                            "cert_ready_at": nd.get('cert_ready_at') or None,
                            "provisioning_seconds": provisioning_seconds,
                            "from_available_seconds": from_available_seconds,
                            "from_create_seconds": from_create_seconds,
                        })

    def _stats(xs):
        if not xs:
            return None
        return {"samples": len(xs), "min": round(min(xs), 1),
                "avg": round(sum(xs) / len(xs), 1), "max": round(max(xs), 1)}

    timings = {
        "provisioning_seconds": _stats(prov_secs_all),   # cert issuance duration
        "from_create_seconds": _stats(from_create_all),  # pod create → cert ready
    }

    if errors:
        return {"status": "error", "message": f"DB query errors: {errors}",
                "counts": counts, "timings": timings, "sampled": sampled}
    if not enabled:
        return {"status": "ok", "message": "Cert tracking disabled (cert_splash_enabled=false).",
                "acme_working": None, "counts": counts, "timings": timings, "sampled": sampled}

    total = sum(counts.values())
    acme_working = counts['ready'] > 0
    if total == 0:
        status, msg = "ok", "No AVAILABLE pods with http domains to check."
    elif acme_working and counts['failed'] == 0:
        status, msg = "ok", f"ACME issuing certs: {counts['ready']} ready, {counts['provisioning']} provisioning."
    elif acme_working:
        status, msg = "warning", f"{counts['ready']} cert(s) ready but {counts['failed']} failed to provision."
    else:
        status, msg = "warning", (
            f"No certs confirmed ready ({counts['provisioning']} provisioning, "
            f"{counts['failed']} failed, {counts['unknown']} unknown) — Let's Encrypt/ACME "
            f"may not be reachable here (expected on local/dev)."
        )

    if timings["provisioning_seconds"]:
        msg += f" Avg issue {timings['provisioning_seconds']['avg']}s."

    return {"status": status, "message": msg, "acme_working": acme_working,
            "counts": counts, "timings": timings, "sampled": sampled}


@router.get(
    "/pods/admin/health",
    tags=["Admin"],
    summary="admin_health",
    operation_id="admin_health",
)
async def admin_health():
    """
    Admin-only diagnostic endpoint. Checks database, RabbitMQ, Traefik pod
    status, traffic ingestion state, RBAC, and TLS cert (ACME) provisioning.
    Returns a structured report with per-subsystem status (ok / warning / error)
    and human-readable messages.
    """
    db = _check_database()
    rabbit = _check_rabbitmq()
    traefik = _check_traefik()
    traffic = _check_traffic_ingestion()
    rbac = _check_rbac()
    certs = _check_certs()

    subsystems = {"database": db, "rabbitmq": rabbit, "traefik": traefik, "traffic_ingestion": traffic, "rbac": rbac, "certs": certs}

    # Roll up: any error → error, any warning → warning, else ok
    statuses = [s["status"] for s in subsystems.values()]
    if "error" in statuses:
        overall = "error"
    elif "warning" in statuses:
        overall = "warning"
    else:
        overall = "ok"

    return ok(result={
        "overall_status": overall,
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "subsystems": subsystems,
    }, msg=f"Admin health check complete. Overall: {overall}.")


@router.get(
    "/pods/admin/metrics",
    tags=["Admin"],
    summary="admin_metrics",
    operation_id="admin_metrics",
)
async def admin_metrics():
    """
    Admin-only. Aggregate compute and status metrics across all pods.

    Fetches live CPU/memory usage from the k8s Metrics API (requires metrics-server)
    and merges with DB records to produce per-pod and cluster-wide summaries.
    Returns gracefully if metrics-server is unavailable.
    """
    from models_pods import Pod

    # ── k8s live metrics (best-effort) ────────────────────────────────────────
    k8s_usage = get_all_pod_k8s_metrics()   # {k8_name: {cpu_m, mem_mb}}
    metrics_available = bool(k8s_usage)

    # ── DB: all pods across all tenants ───────────────────────────────────────
    status_counts: dict = {}
    pod_rows = []

    for site, tenants in SITE_TENANT_DICT.items():
        for tenant in tenants:
            if tenant in ("siteadmintable", "defaulttables"):
                continue
            try:
                pods = Pod.db_get_all(tenant=tenant, site=site)
            except Exception:
                continue
            for pod in pods:
                s = pod.status or "UNKNOWN"
                status_counts[s] = status_counts.get(s, 0) + 1

                res = pod.resources or {}
                usage = k8s_usage.get(pod.k8_name, {})

                pod_rows.append({
                    "pod_id":       pod.pod_id,
                    "k8_name":      pod.k8_name,
                    "tenant":       tenant,
                    "site":         site,
                    "status":       pod.status,
                    "cpu_request_m": int(res.get("cpu_request", 0) or 0),
                    "cpu_limit_m":   int(res.get("cpu_limit", 0) or 0),
                    "mem_request_mb": int(res.get("mem_request", 0) or 0),
                    "mem_limit_mb":   int(res.get("mem_limit", 0) or 0),
                    "cpu_used_m":    usage.get("cpu_m", None),
                    "mem_used_mb":   usage.get("mem_mb", None),
                })

    # ── Cluster totals ────────────────────────────────────────────────────────
    total_cpu_req  = sum(r["cpu_request_m"]  for r in pod_rows)
    total_mem_req  = sum(r["mem_request_mb"] for r in pod_rows)
    total_cpu_used = sum(r["cpu_used_m"]  for r in pod_rows if r["cpu_used_m"]  is not None)
    total_mem_used = sum(r["mem_used_mb"] for r in pod_rows if r["mem_used_mb"] is not None)

    # Top consumers (running pods with live metrics)
    with_metrics = [r for r in pod_rows if r["cpu_used_m"] is not None]
    top_cpu = sorted(with_metrics, key=lambda x: x["cpu_used_m"],  reverse=True)[:12]
    top_mem = sorted(with_metrics, key=lambda x: x["mem_used_mb"], reverse=True)[:12]

    return ok(result={
        "metrics_available":  metrics_available,
        "checked_at":         datetime.utcnow().isoformat() + "Z",
        "status_distribution": status_counts,
        "totals": {
            "total_pods":       len(pod_rows),
            "with_live_metrics": len(with_metrics),
            "cpu_request_m":    total_cpu_req,
            "cpu_used_m":       round(total_cpu_used, 1),
            "mem_request_mb":   total_mem_req,
            "mem_used_mb":      round(total_mem_used, 1),
        },
        "top_cpu": top_cpu,
        "top_mem": top_mem,
    }, msg="Admin metrics retrieved.")
