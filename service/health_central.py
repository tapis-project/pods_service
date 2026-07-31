"""
health - central
Only ran alongside main api/db/traefik pods.
Takes care of NFS health


Does the following:
1. Does startup work for NFS mount
2. Goes through NFS mount and does cleaning
3. Deals with traefik proxy, logs, and metrics

"""

import os
import socket
import ssl
import subprocess
import time
import random
from datetime import datetime, timedelta
from channels import CommandChannel
from kubernetes import client, config
from kubernetes_utils import get_current_k8_services, get_current_k8_pods, rm_container, rm_pvc, \
     get_current_k8_pods, rm_service, KubernetesError, update_traefik_configmap, get_k8_logs, list_all_containers, run_k8_exec
from codes import AVAILABLE, DELETING, STOPPED, ERROR, REQUESTED, COMPLETE, RESTART, ON, OFF
from stores import pg_store, SITE_TENANT_DICT
from models_pods import Pod
from models_node import Node
from models_node_telemetry import NodeLog, NodeMetric
from models_node_commands import NodeCommand
from models_routes import Route
from sqlalchemy import delete as sa_delete
from models_templates_utils import combine_pod_and_template_recursively
from models_volumes import Volume
from models_snapshots import Snapshot
from volume_utils import files_listfiles, files_delete, files_mkdir
from sqlmodel import select
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapipy.errors import BaseTapyException

logger = get_logger(__name__)


# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()


def add_path(tree, path, file):
    # Function to create a tree of dictionaries that represent the file structure of the NFS system.
    path = path.replace(f"{conf.nfs_base_path}/", "", 1) # must delete {nfs_base_path}/ for parsing
    nodes = path.split('/')
    current = tree
    for node in nodes:
        if file['type'] == "dir":
            current = current.setdefault(node, {})
        elif file['type'] == "file":
            current = current.setdefault(node, file)
    return tree


def check_nfs_files():
    """Go through database for all tenants in this site. Go through all nfs files, ensure there are no files corresponding with
    items that are not in the database.
    """

    logger.info("Top of check_nfs_files.")

    # Get all files recursively in the nfs volume
    all_site_files = files_listfiles(path="/", recurse=True, base_path=conf.nfs_base_path)
    # Take all files and create a dictionary tree that's easier to parse.
    file_tree = {}
    for file in all_site_files:
        add_path(file_tree, file['path'], file)

    for tenant in SITE_TENANT_DICT[conf.site_id]:
        logger.info(f"Top of check_nfs_files for tenant: {tenant}.\n")
        ### Volumes
        # Go through database for tenant. Get all volumes
        tenant_volume_list = Volume.db_get_all(tenant=tenant, site=conf.site_id)
        tenant_volume_dict = {}
        for volume in tenant_volume_list:
            # {volume_id: volume, ...}
            tenant_volume_dict[volume.volume_id] = volume

        # Go through all files entries in the tenant, looking for excess files. Ones who don't have entry in volumes db.
        for folder in file_tree[tenant]['volumes'].keys():
            # Found match
            if tenant_volume_dict.get(folder):
                logger.info(f"Found match for folder: {folder}")
                pass
            # File doesn't match any entries in volumes db. We will delete it
            else:
                logger.warning(f"Couldn't find volume with name: {folder} in database: {tenant_volume_dict}. Deleting it now.\n")
                logger.debug(f"volume dict: {tenant_volume_dict}")
                logger.debug(f"volume files: {file_tree[tenant]['volumes']}")
                files_delete(path=f"/volumes/{folder}", tenant_id=tenant)

        ### Snapshots
        # Go through database for tenant. Get all snapshots
        tenant_snapshot_list = Snapshot.db_get_all(tenant=tenant, site=conf.site_id)
        tenant_snapshot_dict = {}
        for snapshot in tenant_snapshot_list:
            # {snapshot_id: snapshot, ...}
            tenant_snapshot_dict[snapshot.snapshot_id] = snapshot
        
        # Go through all files entries in the tenant, looking for excess files. Ones who don't have entry in snapshots db.
        for folder in file_tree[tenant]['snapshots'].keys():
            # Found match
            if tenant_snapshot_dict.get(folder):
                logger.info(f"Found match for folder: {folder}")
                pass
            # File doesn't match any entries in snapshots db. We will delete it
            else:
                logger.warning(f"Couldn't find snapshot with name: {folder} in database: {tenant_snapshot_dict}. Deleting it now.\n")
                logger.debug(f"snapshot dict: {tenant_snapshot_dict}")
                logger.debug(f"snapshot files: {file_tree[tenant]['snapshots']}")
                files_delete(path=f"/snapshots/{folder}", tenant_id=tenant)



def check_volume_sizes():
    """Measure NFS disk usage for all volumes and snapshots.

    Runs `du -sm` on each object's directory, updates the `size` field in the DB,
    writes a VolumeUsageLog entry, and emits a warning when over size_limit.
    No enforcement action is taken yet — alerts only.
    """
    from models_volume_usage import VolumeUsageLog

    def _measure_mb(path: str) -> float | None:
        if not os.path.exists(path):
            return None
        try:
            result = subprocess.run(
                ["du", "-sm", path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return float(result.stdout.split("\t")[0])
        except Exception as e:
            logger.warning(f"du failed for {path}: {e}")
        return None

    measured = 0
    missing = 0
    failed = 0

    def _measure_object(tenant: str, kind: str, obj_id: str, obj) -> str:
        """Measure ONE volume/snapshot. Returns 'measured'|'missing'|'failed'.
        Isolated so one bad object (db hiccup, missing table, weird path) can
        never abort the rest of the tenant's sweep — that failure mode blanked
        every size after the first over-limit volume when volumeusagelog was
        missing."""
        # NFS layout is {nfs_base_path}/{tenant}/<kind>s/{id} — the tenant
        # segment is REQUIRED (matches volume_utils.files_*); without it every
        # path fails os.path.exists and the sweep silently measures nothing.
        path = os.path.join(conf.nfs_base_path, tenant, f"{kind}s", obj_id)
        size_mb = _measure_mb(path)
        if size_mb is None:
            return "missing"
        try:
            obj.size = int(size_mb)
            # Background measurement — don't stamp update_ts (user_update=False), or every
            # du sweep would look like a user edit on the volume/snapshot.
            obj.db_update(user_update=False)
            VolumeUsageLog.log_measurement(
                object_id=obj_id,
                object_type=kind,
                tenant_id=tenant,
                site_id=conf.site_id,
                size_mb=size_mb,
                size_limit_mb=float(obj.size_limit) if obj.size_limit else None,
            )
            VolumeUsageLog.purge_old(obj_id, kind, tenant, conf.site_id)
            if obj.size_limit and size_mb > float(obj.size_limit):
                logger.warning(
                    f"{kind.capitalize()} {obj_id} ({tenant}) is OVER size limit: "
                    f"{size_mb:.1f} MB > {obj.size_limit} MB (no enforcement yet)")
            else:
                logger.debug(f"{kind.capitalize()} {obj_id}: {size_mb:.1f} MB")
            return "measured"
        except Exception as e:
            logger.warning(f"check_volume_sizes: recording {kind} {obj_id} ({tenant}) failed: {e}")
            return "failed"

    for tenant in SITE_TENANT_DICT[conf.site_id]:
        for kind, model in (("volume", Volume), ("snapshot", Snapshot)):
            try:
                objects = model.db_get_all(tenant=tenant, site=conf.site_id)
            except Exception as e:
                logger.warning(f"check_volume_sizes {kind}s listing error for tenant={tenant}: {e}")
                continue
            for obj in objects:
                obj_id = getattr(obj, f"{kind}_id")
                outcome = _measure_object(tenant, kind, obj_id, obj)
                if outcome == "measured":
                    measured += 1
                elif outcome == "missing":
                    missing += 1
                else:
                    failed += 1

    if failed:
        logger.warning(f"check_volume_sizes: {failed} object(s) measured but FAILED to record — see warnings above.")
    if missing and not measured:
        # Every single path was absent — that's not "empty volumes", that's a
        # wrong mount/layout. Shout so it can't fail silently again.
        logger.error(
            f"check_volume_sizes measured 0 of {missing} objects — every NFS path "
            f"under {conf.nfs_base_path} was missing. Check the NFS mount and the "
            f"{{base}}/{{tenant}}/volumes layout.")
    else:
        logger.info(f"check_volume_sizes: measured {measured}, missing {missing}, failed {failed}.")


def check_nfs_tapis_system():
    """Ensures nfs is up and tapis is connected to it.
    This central health instance needs to be in the same K8 namespace as api and pods-nfs(?).
    We grab the nfs ssh ip.
    """
    logger.info("Top of check_nfs_tapis_system.")

    # Get K8 pod name named pods-nfs
    k8_name = ""
    idx = 0
    while idx < 20:
        nfs_pods = []
        for k8_pod in list_all_containers(filter_str="pods-nfs"):
            k8_name = k8_pod.metadata.name
            # pods-nfs also matches pods-nfs-mkdir, so we manually pass that case
            if "pods-nfs-mkdirs" in k8_name:
                continue
            nfs_pods.append({'pod_info': k8_pod,
                                'k8_name': k8_name})
        # Checking how many services met the filter (should hopefully be only one)
        match len(nfs_pods):
            case 1:
                logger.info(f"Found one pod matching pods-nfs. Name: {nfs_pods[0]['k8_name']}")
                break
            case 0:
                logger.info(f"Couldn't find pod matching pods-nfs. Trying again.")
                pass
            case _:
                logger.info(f"Got >1 pods matching pods-nfs. Matching pods: {[pod['k8_name'] for pod in nfs_pods]}. Trying again.")
                pass
        # Increment and have a short wait
        idx += 1
        time.sleep(3)
    else:
        logger.error(f"Couldn't find pod matching pods-nfs after 20 tries. Exiting check_nfs_tapis_system.")
        return

    # k8_name could have been changed by now, so we need to set from nfs_pods.
    k8_name = nfs_pods[0]['k8_name']

    # Go through each tenant and initialize folders
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        # Logging for tenant initialization
        logger.info(f"Initializing nfs folders for tenant: {tenant}.")
        nfs_folder_init(tenant)


def nfs_folder_init(tenant):
    try:
        logger.info(f"Creating tenant root folder for {conf.site_id}.{tenant}.")
        # Ensure tenant root folder exists, this will not cause issues, even if storage is already in use.
        res = files_mkdir(path = "/", tenant_id=tenant)
    except Exception as e:
        msg = f"Error creating tenant root folder for {conf.site_id}.{tenant}. e: {e}"
        logger.critical(msg)
        raise BaseTapyException(msg)

    try:
        logger.info(f"Creating tenant volumes folder for {conf.site_id}.{tenant}.")
        # Ensure tenant volumes folder exists, this will not cause issues, even if storage is already in use.
        res = files_mkdir(path = "/volumes", tenant_id=tenant)
    except Exception as e:
        msg = f"Error creating tenant volumes folder for {conf.site_id}.{tenant}. e: {e}"
        logger.critical(msg)
        raise BaseTapyException(msg)

    try:
        logger.info(f"Creating tenant snapshots folder for {conf.site_id}.{tenant}.")
        # Ensure tenant snapshots folder exists, this will not cause issues, even if storage is already in use.
        res = files_mkdir(path = "/snapshots", tenant_id=tenant)
    except Exception as e:
        msg = f"Error creating tenant snapshots folder for {conf.site_id}.{tenant}. e: {e}"
        logger.critical(msg)
        raise BaseTapyException(msg)


def _check_custom_domain_dns(custom_domain: str, pods_ingress: str) -> bool:
    """Return True if custom_domain resolves to the same IP as pods_ingress."""
    try:
        return socket.gethostbyname(custom_domain) == socket.gethostbyname(pods_ingress)
    except socket.gaierror:
        return False


# In-memory throttle/state for TLS cert probing. Keyed by (pod_id, net_name).
# health_central is a single long-running process, so module-level state is safe here
# and keeps throttle bookkeeping out of the DB (we only write the DB on real transitions).
_cert_probe_last = {}        # (pod_id, net_name) -> monotonic ts of last probe
_cert_provision_started = {} # (pod_id, net_name) -> monotonic ts cert was first seen provisioning


def _probe_cert_status(hostname: str, timeout: float) -> str:
    """Probe hostname:443 and classify the outcome — CRITICAL distinction:

    - 'ready'       — a browser-style *verifying* TLS handshake succeeded → the real
                      Let's Encrypt cert is live and trusted.
    - 'invalid'     — we connected, but the TLS/cert is not trusted (name mismatch /
                      Traefik still serving its default cert) → genuinely still provisioning.
    - 'unreachable' — we couldn't even connect (DNS / timeout / no route). This is NOT a
                      cert problem; it usually means the health pod has no path to the
                      *public* URL. We must NOT treat this as "provisioning" — doing so
                      mislabels healthy pods and makes the UI divert users to a wait page
                      for a cert that's actually fine.

    The old code collapsed 'invalid' and 'unreachable' into one False, which is what caused
    "all pods say cert not ready" in environments where the probe can't reach the endpoint.
    """
    ctx = ssl.create_default_context()  # check_hostname=True, verify_mode=CERT_REQUIRED
    try:
        sock = socket.create_connection((hostname, 443), timeout=timeout)
    except Exception:
        return 'unreachable'
    try:
        with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
            ssock.getpeercert()
        return 'ready'
    except ssl.SSLError:
        # Connected, but cert untrusted/mismatched (e.g. default cert during issuance).
        return 'invalid'
    except Exception:
        # Handshake-time timeout or other ambiguous failure — don't guess "provisioning".
        return 'unreachable'
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _evaluate_http_cert(input_pod, net_name, net_info, now_mono):
    """Throttled TLS-cert readiness check for one http networking entry. VISIBILITY ONLY —
    the result never affects routing (see set_traefik_proxy).

    Persists cert_ready/cert_state/timings into input_pod.networking (same write-back pattern
    as custom_domain_verified) and appends action_logs on state transitions. Honors the cert_*
    config knobs. Returns cert_ready (bool). Idempotent: an already-good cert on a pre-existing
    pod is detected 'ready' on the first reachable probe; an unreachable probe leaves state
    untouched so healthy pods are never mislabeled 'provisioning'.
    """
    url = net_info.get('url')
    if not url or not getattr(conf, 'cert_splash_enabled', True):
        return True

    prev_ready = bool(net_info.get('cert_ready', False))
    prev_state = net_info.get('cert_state', '') or ''

    # Once a cert is confirmed live it stays valid (~90d); stop probing.
    if prev_ready:
        return True

    key = (input_pod.pod_id, net_name)
    interval = getattr(conf, 'cert_probe_interval_seconds', 6)
    last = _cert_probe_last.get(key)
    if last is not None and now_mono - last < interval:
        return prev_ready
    _cert_probe_last[key] = now_mono

    timeout = getattr(conf, 'cert_probe_timeout_seconds', 4)
    status = _probe_cert_status(url, timeout)   # 'ready' | 'invalid' | 'unreachable'

    # Unreachable from the health pod → we cannot determine cert state, so we DON'T guess.
    # Leave cert_state untouched (stays '' / unknown) so nothing logs "provisioning" and the
    # UI never diverts users to the wait page for a pod whose cert is probably fine.
    if status == 'unreachable':
        return False

    ready = (status == 'ready')
    started = _cert_provision_started.setdefault(key, now_mono)
    max_wait = getattr(conf, 'cert_provisioning_max_seconds', 180)

    log = None
    if ready:
        new_state = 'ready'
        if prev_state != 'ready':
            log = f"TLS certificate ready for {url}"
    elif now_mono - started >= max_wait:
        # Connected but cert still untrusted past max wait — record 'failed' (informational).
        new_state = 'failed'
        if prev_state != 'failed':
            log = f"TLS certificate still not verified for {url} after {int(now_mono - started)}s"
    else:
        # Connected, cert not yet trusted → genuinely mid-issuance.
        new_state = 'provisioning'
        if prev_state != 'provisioning':
            log = f"TLS certificate provisioning for {url} (Let's Encrypt)…"

    if new_state != prev_state or ready != prev_ready:
        try:
            raw_net = {k: (v.dict() if hasattr(v, 'dict') else dict(v)) for k, v in input_pod.networking.items()}
            entry = raw_net.setdefault(net_name, {})
            entry['cert_ready'] = ready
            entry['cert_state'] = new_state
            # Wall-clock timestamps for the admin timings view (set once, on first transition).
            now_iso = datetime.utcnow().isoformat()
            if new_state == 'provisioning' and not entry.get('cert_provisioning_started_at'):
                entry['cert_provisioning_started_at'] = now_iso
            if ready and not entry.get('cert_ready_at'):
                entry['cert_ready_at'] = now_iso
            input_pod.networking = raw_net
            input_pod.db_update(log=log, tenant=input_pod.tenant_id, site=input_pod.site_id, user_update=False)
        except Exception as e:
            logger.error(f"Failed to update cert_state for pod {input_pod.pod_id} net '{net_name}': {e}")

    return ready


def _prewarm_cert(pod_id, key_suffix, hostname, now_mono):
    """Fire-and-forget TLS handshake to trigger Traefik's on-demand cert issuance for a domain
    before any user visits it (the first handshake to a certless host is what kicks off ACME).
    Used for BYOD custom domains, whose cert state we don't otherwise track. Throttled per host;
    result is intentionally ignored."""
    if not hostname or not getattr(conf, 'cert_splash_enabled', True):
        return
    key = (pod_id, key_suffix)
    interval = getattr(conf, 'cert_probe_interval_seconds', 6)
    last = _cert_probe_last.get(key)
    if last is not None and now_mono - last < interval:
        return
    _cert_probe_last[key] = now_mono
    _probe_cert_status(hostname, getattr(conf, 'cert_probe_timeout_seconds', 4))


def _is_pod_networking_live(pod, input_pod) -> bool:
    """Return True when traffic should route to the real pod service.
    Uses status_container['ready'] (written by health.py each cycle) so this
    function never makes an extra K8s API call.
    """
    if input_pod.status != AVAILABLE:
        return False
    hc = pod.healthchecks
    # hc may be a Pydantic model (templated pods) or a plain dict (stack members
    # combined from a dict definition) — read both the same way, or this throws and
    # aborts the whole set_traefik_proxy() reconcile, freezing routing for every pod.
    if isinstance(hc, dict):
        networking_requires_ready = hc.get('networking_requires_ready')
        readiness = hc.get('readiness')
    else:
        networking_requires_ready = getattr(hc, 'networking_requires_ready', None)
        readiness = getattr(hc, 'readiness', None)
    if not hc or not networking_requires_ready or not readiness:
        # No readiness gate configured — route live once AVAILABLE
        return True
    # Readiness gate active: trust the ready flag written by health.py
    status_container = input_pod.status_container or {}
    if not isinstance(status_container, dict):
        status_container = getattr(status_container, '__dict__', {}) or {}
    return bool(status_container.get('ready', False))


# Smart defaults: already-compressed formats where re-compression wastes CPU.
# Shared by pod networking entries and node routes.
_COMPRESSION_EXCLUDED_DEFAULTS = [
    'application/gzip', 'application/zip', 'application/zstd', 'application/x-tar',
    'application/x-bzip2', 'application/x-xz', 'application/x-7z-compressed',
    'image/png', 'image/jpeg', 'image/webp', 'image/gif',
    'video/mp4', 'video/webm', 'audio/mpeg', 'audio/ogg']


def set_traefik_proxy():
    all_pods = []
    stmt = select(Pod)
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        all_pods += pg_store[conf.site_id][tenant].run("execute", stmt, scalars=True, all=True)

    # Prune cert-state entries for pods that no longer exist — these module-level dicts are
    # keyed by (pod_id, net_name) and would otherwise grow unbounded as pods are deleted.
    _valid_pod_ids = {p.pod_id for p in all_pods}
    for _cert_dict in (_cert_probe_last, _cert_provision_started):
        for _stale in [k for k in _cert_dict if k[0] not in _valid_pod_ids]:
            del _cert_dict[_stale]

    ### Proxy ports and config changes
    # For proxy config later. proxy_info_x = {pod.k8_name: {routing_port, url}, ...} 
    tcp_proxy_info = {}
    http_proxy_info = {}
    postgres_proxy_info = {}
    for input_pod in all_pods:
        #logger.critical(f"TESTINGERROR-input_pod.tenant_id: {input_pod.tenant_id}, input_pod.site_id: {input_pod.site_id}")
        try: 
            pod = combine_pod_and_template_recursively(input_pod, input_pod.template, tenant=input_pod.tenant_id, site=input_pod.site_id)
        except Exception as e:
            logger.critical(f"Error combining pod and template. Skipping pod {input_pod.pod_id}. e: {e}")
            continue
        # Determine networking_live for this pod and persist if changed
        pod_networking_live = _is_pod_networking_live(pod, input_pod)
        if input_pod.networking_live != pod_networking_live:
            try:
                input_pod.networking_live = pod_networking_live
                input_pod.db_update(tenant=input_pod.tenant_id, site=input_pod.site_id, user_update=False)
            except Exception as e:
                logger.warning(f"Failed to update networking_live for pod {input_pod.pod_id}: {e}")
        splash_mode = not pod_networking_live

        # Each pod can have up to 3 networking objects with custom filled port/protocol/name
        for net_name, net_info in pod.networking.items():
            if not isinstance(net_info, dict):
                net_info = net_info.dict()

            # name should be pods-tacc-tacc-mypod unless specified, if so
            # then something like pods-tacc-tacc-mypod-mynet
            if net_name != "default":
                traefik_service_name = f"{pod.k8_name}-{net_name}"
            else:
                traefik_service_name = pod.k8_name

            # Cert tracking + pre-warm — VISIBILITY ONLY, never routing. We probe/track the
            # cert once the pod is AVAILABLE (action_logs + admin timings) and warm it via a
            # best-effort handshake. We deliberately do NOT hold the splash on cert state:
            # doing so broke working pods and deadlocked issuance — the splash middleware
            # rewrites every path (incl. the ACME HTTP-01 challenge at /.well-known/...), so a
            # cert-gated splash prevents the very cert it's waiting for, and the in-cluster
            # verifying probe can't always reach the public endpoint. Pods route to the real
            # backend as soon as networking-live (splash is driven by readiness only); Traefik
            # issues the cert on the first real hit, and the UI holding route covers users.
            if net_info.get('protocol') == 'http' and input_pod.status == AVAILABLE:
                _now_mono = time.monotonic()
                _evaluate_http_cert(input_pod, net_name, net_info, _now_mono)
                if net_info.get('custom_domain') and net_info.get('custom_domain_verified'):
                    _prewarm_cert(input_pod.pod_id, f"{net_name}:custom", net_info['custom_domain'], _now_mono)

            # Splash mode: route to pods-api splash endpoint instead of real service
            if splash_mode and net_info.get('protocol') == 'http':
                template_info = {
                    "routing_port": 8000,
                    "url": net_info['url'],
                    "k8_service": "pods-api",
                    "splash_mode": True,
                }
            else:
                template_info = {
                    "routing_port": net_info['port'],
                    "url": net_info['url'],
                    "k8_service": pod.k8_name,
                    "splash_mode": False,
                }
            ## cors headers
            cors_info = {
                "cors_allow_origins": net_info.get('cors_allow_origins', []),
                "cors_allow_methods": net_info.get('cors_allow_methods', []),
                "cors_allow_headers": net_info.get('cors_allow_headers', []),
                "cors_allow_credentials": net_info.get('cors_allow_credentials', False),
                "cors_max_age": net_info.get('cors_max_age', 100),
            }
            ## tapis auth
            # The goal is: https://tacc.develop.tapis.io/v3/pods/{{pod_id}}/auth
            pod_id_section, tapis_domain = net_info['url'].split('.pods.') ## Should return `mypod` & `tacc.tapis.io` with proper tenant and schmu
            if '-' in pod_id_section:
                pod_id, network_section = pod_id_section.split('-') # e.g. `mypod-networking2` if there's several networking objects
            else:
                pod_id = pod_id_section
            forward_auth_info = {
                "tapis_auth": net_info.get('tapis_auth', False),
                "auth_url": f"https://{tapis_domain}/v3/pods/{pod_id}/auth",
                "tapis_auth_response_headers": net_info.get('tapis_auth_response_headers', {}),
                "tapis_auth_excluded_paths": net_info.get('tapis_auth_excluded_paths', []),
                "tapis_auth_excluded_path_regex": net_info.get('tapis_auth_excluded_path_regex', []),
            }
            ## access gate (shared password/token) — separate forwardAuth to the pod's /gate endpoint
            gate_info = {
                "access_gate": net_info.get('access_gate', False),
                # full pod_id_section (pod_id or pod_id-<network>) — /gate splits it to
                # evaluate the RIGHT networking entry; bare pod_id would always gate 'default'
                "gate_url": f"https://{tapis_domain}/v3/pods/{pod_id_section}/gate",
            }
            ## ip allow list
            ip_allow_list_info = {
                "ip_allow_list": net_info.get('ip_allow_list', [])
            }
            ## proxy compression (defaults ensure backwards compat with old pods missing these fields)
            compression_info = {
                "proxy_compression": net_info.get('proxy_compression', True),
                "proxy_compression_encodings": net_info.get('proxy_compression_encodings', ['zstd', 'br', 'gzip']),
                "proxy_compression_excluded_content_types": list(set(
                    _COMPRESSION_EXCLUDED_DEFAULTS
                    + net_info.get('proxy_compression_excluded_content_types', [])
                )),
                "proxy_compression_min_response_body_bytes": net_info.get('proxy_compression_min_response_body_bytes', 1024),
            }
            ## custom domain (BYOD) — only relevant for http protocol
            custom_domain = net_info.get('custom_domain', '')
            custom_domain_verified = net_info.get('custom_domain_verified', False)
            if custom_domain and net_info.get('protocol') == 'http':
                pods_ingress = 'pods.' + net_info['url'].split('.pods.', 1)[1]
                currently_verified = _check_custom_domain_dns(custom_domain, pods_ingress)
                if currently_verified != custom_domain_verified:
                    try:
                        raw_net = {k: (v.dict() if hasattr(v, 'dict') else dict(v)) for k, v in input_pod.networking.items()}
                        raw_net.setdefault(net_name, {})['custom_domain_verified'] = currently_verified
                        input_pod.networking = raw_net
                        input_pod.db_update(tenant=input_pod.tenant_id, site=input_pod.site_id, user_update=False)
                        logger.info(f"custom_domain_verified={currently_verified} for pod {pod.pod_id} net '{net_name}'")
                    except Exception as e:
                        logger.error(f"Failed to update custom_domain_verified for pod {pod.pod_id}: {e}")
                    custom_domain_verified = currently_verified
            custom_domain_info = {
                "custom_domain": custom_domain,
                "custom_domain_verified": custom_domain_verified,
            }
            logger.debug(f"pod_id: {pod_id}, tapis_domain: {tapis_domain}, net_info: {net_info}, traefik_forward_auth_info: {forward_auth_info}, cors_info: {cors_info}, ip_allow_list: {ip_allow_list_info}, compression_info: {compression_info}")
            match net_info['protocol']:
                case "tcp":
                    # ip_allow_list
                    template_info.update(ip_allow_list_info)
                    tcp_proxy_info[traefik_service_name] = template_info
                case "http":
                    # tapis auth — skip in splash mode (splash page is publicly accessible)
                    if forward_auth_info['tapis_auth'] and not splash_mode:
                        template_info.update(forward_auth_info)
                    # access gate (shared password/token) — also skipped in splash mode
                    if gate_info['access_gate'] and not splash_mode:
                        template_info.update(gate_info)
                    # cors settings
                    if cors_info['cors_allow_origins']:
                        template_info.update(cors_info)
                    # ip_allow_list
                    template_info.update(ip_allow_list_info)
                    # proxy compression
                    template_info.update(compression_info)
                    # custom domain (BYOD)
                    template_info.update(custom_domain_info)
                    http_proxy_info[traefik_service_name] = template_info
                case "postgres":
                    # ip_allow_list
                    template_info.update(ip_allow_list_info)
                    postgres_proxy_info[traefik_service_name] = template_info
                case "local_only":
                    # when users only need networking to connect to other pods in the same namespace
                    pass

    # Node routes (publish v0) — extra http entries alongside pods. k8_service can be ANY
    # host reachable from central (tailnet IP/name, or a dev gateway like
    # host.minikube.internal); the template renders http://<backend_host>:<port> either way.
    # forwardAuth reuses the shared tapis_auth flow at /pods/routes/{route_id}/auth.
    all_routes = []
    route_stmt = select(Route)
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        try:
            all_routes += pg_store[conf.site_id][tenant].run("execute", route_stmt, scalars=True, all=True)
        except Exception as e:
            logger.error(f"Error fetching node routes for tenant {tenant}: {e}")
    for route in all_routes:
        try:
            if not route.url or not route.backend_host:
                logger.warning(f"Skipping route '{route.route_id}': url or backend_host unset — not rendering it into the proxy config.")
                continue
            tapis_domain = route.url.split('.pods.', 1)[1]
            template_info = {
                "routing_port": route.port,
                "url": route.url,
                "k8_service": route.backend_host,
                "splash_mode": False,
                "ip_allow_list": [],
                "proxy_compression": True,
                "proxy_compression_encodings": ['zstd', 'br', 'gzip'],
                "proxy_compression_excluded_content_types": list(_COMPRESSION_EXCLUDED_DEFAULTS),
                "proxy_compression_min_response_body_bytes": 1024,
                "custom_domain": "",
                "custom_domain_verified": False,
            }
            if route.tapis_auth:
                template_info.update({
                    "tapis_auth": True,
                    "auth_url": f"https://{tapis_domain}/v3/pods/routes/{route.route_id}/auth",
                    "tapis_auth_response_headers": route.tapis_auth_response_headers or {},
                    "tapis_auth_excluded_paths": route.tapis_auth_excluded_paths or [],
                    "tapis_auth_excluded_path_regex": route.tapis_auth_excluded_path_regex or [],
                })
            http_proxy_info[route.traefik_service_name()] = template_info
        except Exception as e:
            logger.error(f"Error rendering route '{getattr(route, 'route_id', '?')}' into proxy config. Skipping. e: {e}")

    # This functions only updates if config is out of date.
    update_traefik_configmap(tcp_proxy_info, http_proxy_info, postgres_proxy_info)


def sweep_decommissioning_nodes(timeout_minutes=None):
    """Hard-delete nodes whose decommission was never confirmed by the agent
    within the timeout (agent offline/stopped — including 'the user already
    stopped the thing', which is exactly the case the timeout exists for; the
    UI also offers an immediate force delete). Explicit tenant iteration — no g
    context in the health loop, per the background-task rules. Timeout is read
    per call (NODES_DECOMMISSION_TIMEOUT_MINUTES, default 60) so tests can turn
    it down without a restart. Returns the number of nodes removed.
    """
    if timeout_minutes is None:
        timeout_minutes = int(os.environ.get("NODES_DECOMMISSION_TIMEOUT_MINUTES", "60"))
    cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
    removed = 0
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        try:
            store = pg_store[conf.site_id][tenant]
            stale = store.run(
                "execute",
                select(Node).where(Node.decommission_ts != None,  # noqa: E711 — SQL IS NOT NULL
                                   Node.decommission_ts < cutoff),
                scalars=True, all=True)
            for node in stale:
                logger.warning(
                    f"decommission timeout: node '{node.node_id}' (tenant {tenant}) requested "
                    f"{node.decommission_ts}, no agent confirmation within {timeout_minutes} min — "
                    f"hard-deleting row + routes + telemetry. If the agent is merely offline it "
                    f"will park itself on its next checkin (404) and log removal instructions.")
                store.run("execute", sa_delete(Route).where(Route.node_id == node.node_id))
                store.run("execute", sa_delete(NodeLog).where(NodeLog.node_id == node.node_id))
                store.run("execute", sa_delete(NodeMetric).where(NodeMetric.node_id == node.node_id))
                store.run("execute", sa_delete(NodeCommand).where(NodeCommand.node_id == node.node_id))
                store.run("execute", sa_delete(Node).where(Node.node_id == node.node_id))
                removed += 1
        except Exception as e:
            logger.error(f"decommission sweep failed for tenant {tenant}: {e}")
    return removed


def main():
    """
    Main function for health checks.
    """
    # Try and run check_db_pods. Will try for 60 seconds until health is declared "broken".
    logger.info("Top of health central. Checking if db's are initialized.")
    idx = 0
    while idx < 12:
        try:
            #Volume.db_get_all(tenant="admin", site="tacc")
            check_nfs_tapis_system()

            check_nfs_files()
            logger.info("Successfully ran through check_nfs_files().")
            break
        except Exception as e:
            logger.info(f"Can't run check_nfs_files() yet idx: {idx}. e: {e.args}")
            # Increment and have a short wait
            idx += 1
            time.sleep(5)
    # Reached end of idx limit
    else:
        logger.critical("Health could not run check_nfs_files(). Shutting down!")
        return

    # Main health loop — tick counter drives low-frequency tasks
    _tick = 0
    _SIZE_CHECK_INTERVAL = 200  # every ~10 min (200 ticks × 3 s)
    while True:
        logger.info(f"\n\n\nRunning pods health checks. Now: {time.time()}")
        try:
            set_traefik_proxy()
        except Exception as e:
            logger.error(f"Error setting traefik proxy. e: {e}", exc_info=True)

        try:
            check_nfs_files()
        except Exception as e:
            logger.error(f"Error running check_nfs_files. e: {e}", exc_info=True)

        if _tick % _SIZE_CHECK_INTERVAL == 0:
            try:
                check_volume_sizes()
            except Exception as e:
                logger.error(f"Error running check_volume_sizes. e: {e}", exc_info=True)

        # Decommission timeout sweep every ~3 min (60 ticks × 3 s) — cheap
        # (one indexed-null select per tenant) and the timeout is coarse anyway.
        if _tick % 60 == 0:
            try:
                sweep_decommissioning_nodes()
            except Exception as e:
                logger.error(f"Error running sweep_decommissioning_nodes. e: {e}", exc_info=True)

        _tick += 1
        time.sleep(3)


if __name__ == '__main__':
    main()
