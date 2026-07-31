#!/usr/bin/env python3
"""pods-agent — edge node agent for the Tapis Pods service (Phase 2 v1).

Zero-dependency (Python stdlib only) so it runs on any box with Python 3.9+.
Speaks the HTTP-only node contract (see pods_service ROADMAP_EDGE_REMOTE):

  join      one-time claim-token exchange -> node-scoped bearer token (saved to state)
  run       checkin loop: capabilities + status + hash-gated inventory; command poll
  status    print local state (token redacted)

Usage:
  pods-agent join --url https://<tenant-base>/v3/pods --node <node_id> --token pnc_...
  pods-agent run

Environment:
  PODS_AGENT_STATE            state dir (default /var/lib/pods-agent, else ~/.pods-agent)
  PODS_JOIN_URL, PODS_NODE_ID, PODS_JOIN_TOKEN, PODS_TENANT
                              auto-join on `run` when no state file exists (container UX:
                              docker run -e PODS_JOIN_URL=... -e PODS_NODE_ID=... -e PODS_JOIN_TOKEN=...)
                              PODS_TENANT (or --tenant) rides along as X-Pods-Tenant so the
                              server can locate the node row even when the URL host can't
                              identify the tenant (bare IPs, localhost dev proxies)
  PODS_HOST_HEADER            dev-only: override the HTTP Host header (lets an agent reach a
                              dev deployment by IP while the server still resolves the tenant
                              from a proper host name)
  PODS_AGENT_ADOPT_ENDPOINTS  "false" to pin the join-time URL and ignore endpoint updates
                              from checkin responses (default: adopt — config-as-data)
  PODS_AGENT_INSECURE         "true" to skip TLS verification (dev only)
  DOCKER_SOCK                 docker socket path (default /var/run/docker.sock)

Telemetry (Phase 3 — pure API traffic, no host commands, so no confirmation needed):
  PODS_AGENT_METRICS          "false" disables metrics sampling (default: on)
  PODS_AGENT_METRICS_INTERVAL sample cadence in seconds (default 60); samples buffer
                              across offline stretches (newest ~1500 kept) and flush
                              with the next successful checkin
  PODS_AGENT_SHIP_LOGS        "false" disables log shipping (default: on). Ships the
                              agent's own lines + running docker containers' stdout/err
                              (per-container since-cursors persisted in state — restart
                              never re-ships what central already has)
  PODS_AGENT_LOGS_TAIL        first-contact tail per container (default 200 lines)

Host-command confirmation (the agent NEVER runs host commands like `tailscale up` silently):
  PODS_AGENT_HOST_CMDS        "ask" (default) | "always" | "never"
                              ask: interactive terminal -> (Y/n) prompt per command;
                                   no TTY (containers, systemd) -> treated as "never"
                              always: pre-approve for unattended installs
                              never: hard opt-out, commands are logged and skipped
Capability probes (each is env-gated so deployers can scope what the agent touches;
defaults stay easy — everything on, every skip logged with the env that controls it):
  PODS_AGENT_CHECK_DOCKER     "false" disables the docker socket probe + inventory
  PODS_AGENT_CHECK_K8S        "false" disables kubernetes detection

Offline resilience: central being unreachable is a NORMAL state for an edge — the loop
backs off (capped) and keeps running; it never exits on network errors. A 403 means the
agent token was revoked (admin ran /regenerate) — the agent parks and waits for a re-join.
"""
import argparse
import gzip
import hashlib
import http.client
import json
import os
import platform
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

AGENT_VERSION = "0.3.0"
TOKEN_HEADER = "X-Pods-Node-Token"
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
K8S_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
STARTED_AT = datetime.now(timezone.utc)

_stop = {"flag": False}


# The agent's own lines double as a shippable log source ("agent" in central's
# node log viewer). Ring-buffered; cleared as batches are acked by the server.
SELF_LOG_BUF = []          # [(epoch_seconds, line)]
SELF_LOG_BUF_MAX = 1000


def log(msg):
    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat(timespec='seconds')}] {msg}", flush=True)
    SELF_LOG_BUF.append((now.timestamp(), msg))
    del SELF_LOG_BUF[:-SELF_LOG_BUF_MAX]


# State ------------------------------------------------------------------------

def state_dir():
    d = os.environ.get("PODS_AGENT_STATE")
    if d:
        return d
    default = "/var/lib/pods-agent"
    if os.path.isdir(default) and os.access(default, os.W_OK):
        return default
    try:
        os.makedirs(default, exist_ok=True)
        if os.access(default, os.W_OK):
            return default
    except OSError:
        pass
    return os.path.expanduser("~/.pods-agent")


def state_file():
    return os.path.join(state_dir(), "state.json")


def load_state():
    try:
        with open(state_file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state):
    os.makedirs(state_dir(), exist_ok=True)
    path = state_file()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # state holds the agent bearer token
    except OSError:
        pass


# HTTP -------------------------------------------------------------------------

def http_json(method, url, body=None, headers=None, timeout=20):
    """JSON request; returns the parsed body. Raises urllib.error.HTTPError/URLError."""
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"Content-Type": "application/json", "User-Agent": f"pods-agent/{AGENT_VERSION}"}
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    ctx = None
    if os.environ.get("PODS_AGENT_INSECURE", "").lower() == "true":
        ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode() or "{}")


def http_error_detail(e):
    """Extract the service's error message from an HTTPError body, best effort."""
    try:
        body = json.loads(e.read().decode())
        return body.get("message") or body.get("detail") or str(body)[:200]
    except Exception:
        return ""


# Docker (Engine API over the unix socket — no docker CLI or SDK needed) --------

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


def docker_get(path, parse_json=True):
    """GET against the docker Engine API; None on any failure (no docker = fine)."""
    if not os.path.exists(DOCKER_SOCK):
        return None
    conn = None
    try:
        conn = _UnixHTTPConnection(DOCKER_SOCK)
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            return None
        return json.loads(raw.decode()) if parse_json else raw.decode()
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


# Kubernetes (in-cluster ServiceAccount over the API — no kubectl or SDK needed) -------
# v1 scope: in-cluster only. A bare-host agent with kubectl still DETECTS runtime.k8s,
# but inventory would mean running host commands every checkin — that fights the
# host-command confirmation model, so it stays detection-only for now.

def k8s_in_cluster():
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and os.path.exists(os.path.join(K8S_SA_DIR, "token"))


def k8s_namespace(state_ns=None):
    """Namespace to inventory: central's advisory namespace (join config) wins, then the
    ServiceAccount's own mounted namespace, then 'default'."""
    if state_ns:
        return state_ns
    try:
        with open(os.path.join(K8S_SA_DIR, "namespace")) as f:
            return f.read().strip() or "default"
    except OSError:
        return "default"


def k8s_api(method, path, body=None, timeout=10):
    """Call the in-cluster API with the mounted ServiceAccount token. Raises on failure."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    with open(os.path.join(K8S_SA_DIR, "token")) as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=os.path.join(K8S_SA_DIR, "ca.crt"))
    req = urllib.request.Request(
        f"https://{host}:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": f"pods-agent/{AGENT_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode() or "{}")


# Access the agent WANTS, probed with SelfSubjectAccessReview BEFORE any use — the
# ServiceAccount's actual rules are the source of truth, not what the manifest was
# supposed to grant. Capabilities are reported from what's actually allowed, so a
# missing RoleBinding shows up as a visible capability gap instead of 403 noise.
K8S_DESIRED_ACCESS = [
    ("pods", "", "list"),        # inventory
    ("pods", "log", "get"),      # future: log shipping (Phase 3)
]

_k8s_access_cache = {"at": 0.0, "allowed": {}}
K8S_ACCESS_REPROBE_SECONDS = int(os.environ.get("PODS_AGENT_K8S_REPROBE", "3600"))


def k8s_probe_access(namespace):
    """{'pods.list': bool, 'pods/log.get': bool} for the mounted ServiceAccount in the
    target namespace. Cached; re-probed every K8S_ACCESS_REPROBE_SECONDS so an RBAC fix
    is picked up without an agent restart."""
    now = time.monotonic()
    if _k8s_access_cache["allowed"] and now - _k8s_access_cache["at"] < K8S_ACCESS_REPROBE_SECONDS:
        return _k8s_access_cache["allowed"]
    allowed = {}
    for resource, subresource, verb in K8S_DESIRED_ACCESS:
        attrs = {"namespace": namespace, "verb": verb, "resource": resource}
        if subresource:
            attrs["subresource"] = subresource
        key = f"{resource}{'/' + subresource if subresource else ''}.{verb}"
        try:
            resp = k8s_api(
                "POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                body={"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                      "spec": {"resourceAttributes": attrs}})
            allowed[key] = bool((resp.get("status") or {}).get("allowed"))
        except Exception as e:
            allowed[key] = False
            log(f"k8s access probe for {key} errored: {e}")
    _k8s_access_cache.update(at=now, allowed=allowed)
    summary = ", ".join(f"{k}={'yes' if v else 'NO'}" for k, v in allowed.items())
    log(f"k8s ServiceAccount access (namespace '{namespace}'): {summary}")
    if not allowed.get("pods.list"):
        log("  -> pods.list denied: k8s inventory disabled. Grant the ServiceAccount a Role with "
            "list/get/watch on pods (the kubectl-apply join manifest includes one) and it will be "
            "picked up within an hour, or restart the agent to re-probe now.")
    return allowed


def k8s_inventory_pods(namespace):
    """Namespace pod inventory — fields chosen to be small and hash-stable."""
    resp = k8s_api("GET", f"/api/v1/namespaces/{namespace}/pods?limit=500")
    pods = []
    for item in resp.get("items", []):
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        st = item.get("status") or {}
        cstatuses = st.get("containerStatuses") or []
        pods.append({
            "name": meta.get("name"),
            "phase": st.get("phase"),
            "images": sorted({c.get("image") for c in spec.get("containers", []) if c.get("image")}),
            "ready": f"{sum(1 for c in cstatuses if c.get('ready'))}/{len(cstatuses)}",
            "restarts": sum(c.get("restartCount", 0) for c in cstatuses),
            "node": spec.get("nodeName"),
        })
    return sorted(pods, key=lambda p: p.get("name") or "")


# Confirmation for host commands ------------------------------------------------------

def host_cmds_allowed(reason):
    """Never run host commands silently. Policy via PODS_AGENT_HOST_CMDS
    (ask | always | never); "ask" prompts (Y/n) on a TTY and refuses without one."""
    mode = os.environ.get("PODS_AGENT_HOST_CMDS", "ask").lower()
    if mode in ("always", "true", "yes"):
        return True
    if mode in ("never", "false", "no"):
        log(f"host command SKIPPED by policy (PODS_AGENT_HOST_CMDS=never): {reason}")
        return False
    if sys.stdin.isatty():
        try:
            answer = input(f"pods-agent wants to run a host command:\n  {reason}\nAllow? [Y/n] ")
            return answer.strip().lower() in ("", "y", "yes")
        except EOFError:
            return False
    log(f"host command SKIPPED — no terminal to ask on: {reason}\n"
        f"  (set PODS_AGENT_HOST_CMDS=always to pre-approve for unattended installs)")
    return False


def tailscale_state():
    """None when no tailscale binary; else the daemon's BackendState
    ('Running', 'Stopped', 'NeedsLogin', 'NoDaemon', 'Unknown')."""
    if not shutil.which("tailscale"):
        return None
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=10)
        if out.returncode != 0:
            return "NoDaemon"
        return (json.loads(out.stdout.decode() or "{}")).get("BackendState") or "Unknown"
    except Exception:
        return "Unknown"


# Detection / sampling -----------------------------------------------------------

def _probe_enabled(name):
    return os.environ.get(f"PODS_AGENT_CHECK_{name}", "true").lower() != "false"


def log_probe_policy():
    parts = []
    for name in ("DOCKER", "K8S"):
        parts.append(f"{name.lower()}={'on' if _probe_enabled(name) else 'OFF'} (PODS_AGENT_CHECK_{name})")
    parts.append(f"host_cmds={os.environ.get('PODS_AGENT_HOST_CMDS', 'ask')} (PODS_AGENT_HOST_CMDS)")
    log("probe policy: " + ", ".join(parts))


def detect_capabilities(state_ns=None):
    caps = []
    if _probe_enabled("DOCKER") and docker_get("/_ping", parse_json=False) == "OK":
        caps.append("runtime.docker")
    if _probe_enabled("K8S") and (os.environ.get("KUBERNETES_SERVICE_HOST") or shutil.which("kubectl")):
        caps.append("runtime.k8s")
        # In-cluster: report what the ServiceAccount can actually DO (probed, not
        # assumed) as granular capabilities — the UI renders panels off these.
        if k8s_in_cluster():
            allowed = k8s_probe_access(k8s_namespace(state_ns))
            caps.extend(f"k8s.{key}" for key, ok in sorted(allowed.items()) if ok)
    if not any(c.startswith("runtime.") for c in caps):
        caps.append("runtime.none")
    return caps


def _meminfo():
    """(total_kb, available_kb) from /proc/meminfo, or None off-Linux."""
    try:
        fields = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0].rstrip(":") in ("MemTotal", "MemAvailable"):
                    fields[parts[0].rstrip(":")] = int(parts[1])
        if "MemTotal" in fields:
            return fields["MemTotal"], fields.get("MemAvailable", 0)
    except OSError:
        pass
    return None


def sample_status(caps, inv=None):
    status = {
        "os": platform.system().lower(),
        "arch": platform.machine(),
        "hostname": socket.gethostname(),
        "python": platform.python_version(),
        "agent_started_at": STARTED_AT.isoformat(timespec="seconds"),
        "agent_uptime_seconds": int((datetime.now(timezone.utc) - STARTED_AT).total_seconds()),
    }
    # Metrics-lite: cheap host + workload numbers every checkin (sub-KB). The same
    # reads, timestamped, also feed the Phase 3 history pipeline via metrics_samples
    # (see metrics_sample) — this block stays the instant "now" view on the node row.
    try:
        load1, load5, load15 = os.getloadavg()
        status["load_avg"] = [round(load1, 2), round(load5, 2), round(load15, 2)]
    except OSError:
        pass
    mem = _meminfo()
    if mem:
        total_kb, avail_kb = mem
        status["mem_total_mb"] = total_kb // 1024
        status["mem_available_mb"] = avail_kb // 1024
    try:
        du = shutil.disk_usage("/")
        status["disk_root_used_pct"] = round(du.used / du.total * 100, 1)
    except OSError:
        pass
    if "runtime.docker" in caps:
        version = docker_get("/version")
        if version:
            status["docker_version"] = version.get("Version")
        containers = (inv or {}).get("docker_containers")
        if containers is not None:
            status["docker_containers_running"] = sum(1 for c in containers if c.get("state") == "running")
            status["docker_containers_total"] = len(containers)
    k8s_pods = (inv or {}).get("k8s_pods")
    if k8s_pods is not None:
        phases = {}
        for p in k8s_pods:
            phases[p.get("phase") or "Unknown"] = phases.get(p.get("phase") or "Unknown", 0) + 1
        status["k8s_pod_phases"] = phases
    return status


def collect_inventory(caps, state_ns=None):
    inv = {}
    if "runtime.docker" in caps:
        containers = docker_get("/containers/json?all=true") or []
        inv["docker_containers"] = [
            {
                "id": c.get("Id", "")[:12],
                "image": c.get("Image"),
                "names": [n.lstrip("/") for n in c.get("Names", [])],
                "state": c.get("State"),
                "status": c.get("Status"),
                "ports": sorted({p.get("PrivatePort") for p in c.get("Ports", []) if p.get("PrivatePort")}),
            }
            for c in containers
        ]
    # k8s inventory: in-cluster only, and ONLY when the access probe said pods.list is
    # actually allowed — never fish for 403s.
    if "k8s.pods.list" in caps:
        ns = k8s_namespace(state_ns)
        try:
            inv["k8s_namespace"] = ns
            inv["k8s_pods"] = k8s_inventory_pods(ns)
        except Exception as e:
            log(f"k8s pod inventory failed (namespace '{ns}'): {e}")
            inv.pop("k8s_namespace", None)
    return inv


def inventory_hash(inv):
    return hashlib.sha256(json.dumps(inv, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# Telemetry — Phase 3 (metrics samples + log shipping) ---------------------------
# All pure API traffic over connections the agent already uses (central HTTPS +
# the docker socket) — no host commands, so none of this needs confirmation.

METRICS_BUF_MAX = 1500     # ~25 h @ 60 s; matches central's per-checkin batch cap


def metrics_sample(caps, inv):
    """One timestamped gauge sample from the same cheap reads as sample_status.
    Fields match central's nodemetric columns; absent gauges stay None."""
    sample = {"ts": round(datetime.now(timezone.utc).timestamp(), 3)}
    try:
        sample["load1"] = round(os.getloadavg()[0], 3)
    except OSError:
        pass
    cpus = os.cpu_count()
    if cpus:
        sample["cpu_count"] = cpus
    mem = _meminfo()
    if mem:
        total_kb, avail_kb = mem
        sample["mem_total_bytes"] = total_kb * 1024
        sample["mem_used_bytes"] = max(0, (total_kb - avail_kb) * 1024)
    try:
        du = shutil.disk_usage("/")
        sample["root_disk_pct"] = round(du.used / du.total * 100, 2)
    except OSError:
        pass
    containers = (inv or {}).get("docker_containers")
    if containers is not None:
        sample["docker_running"] = sum(1 for c in containers if c.get("state") == "running")
        sample["docker_total"] = len(containers)
    k8s_pods = (inv or {}).get("k8s_pods")
    if k8s_pods is not None:
        sample["k8s_running"] = sum(1 for p in k8s_pods if p.get("phase") == "Running")
        sample["k8s_total"] = len(k8s_pods)
    return sample


def docker_get_bytes(path):
    """GET against the docker Engine API returning raw bytes (log streams are
    NOT json and NOT necessarily utf-8-clean); None on any failure."""
    if not os.path.exists(DOCKER_SOCK):
        return None
    conn = None
    try:
        conn = _UnixHTTPConnection(DOCKER_SOCK, timeout=15)
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read()
        return raw if resp.status == 200 else None
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


def demux_docker_logs(raw):
    """Docker's log endpoint returns multiplexed 8-byte-header frames for non-TTY
    containers and a raw byte stream for TTY ones. Try frames first; on any
    malformed header fall back to treating the whole payload as raw text."""
    out = []
    i = 0
    n = len(raw)
    while i + 8 <= n:
        stream_type = raw[i]
        if stream_type not in (0, 1, 2) or raw[i + 1:i + 4] != b"\x00\x00\x00":
            return raw.decode("utf-8", errors="replace")
        length = int.from_bytes(raw[i + 4:i + 8], "big")
        if i + 8 + length > n:
            return raw.decode("utf-8", errors="replace")
        out.append(raw[i + 8:i + 8 + length])
        i += 8 + length
    if i != n:
        return raw.decode("utf-8", errors="replace")
    return b"".join(out).decode("utf-8", errors="replace")


def _parse_rfc3339_nano(ts):
    """Docker's timestamps=1 prefix ('2026-07-30T12:00:00.123456789Z') -> epoch
    float. fromisoformat can't take 9 fractional digits, so parse manually."""
    try:
        base, _, frac = ts.partition(".")
        frac = frac.rstrip("Z")
        base = base.rstrip("Z")
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        ns = int((frac + "000000000")[:9]) if frac else 0
        return dt.timestamp() + ns / 1e9
    except (ValueError, AttributeError):
        return None


def parse_docker_log_lines(text, after_epoch):
    """Timestamped log text -> [(epoch, line)], strictly newer than after_epoch.
    Docker's `since` filter is inclusive-ish at second granularity, so the exact
    per-line cutoff here is what prevents boundary duplicates."""
    out = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        ts_str, _, rest = raw_line.partition(" ")
        epoch = _parse_rfc3339_nano(ts_str)
        if epoch is None:
            # No parseable prefix — keep the line, order it at the cursor edge.
            out.append((after_epoch or 0.0, raw_line))
            continue
        if after_epoch and epoch <= after_epoch:
            continue
        out.append((epoch, rest))
    return out


def _self_container_id():
    """Our own container id prefix when running containerized (hostname default),
    so the agent doesn't ship its container's stdout AND its self-log source."""
    if not os.path.exists("/.dockerenv"):
        return None
    return socket.gethostname()[:12]


def collect_container_logs(inv, cursors, tail, per_container_cap=500, total_cap=2000):
    """New log lines per running container, strictly after each cursor.
    Returns (entries, advanced) where advanced = {name: newest_epoch_included}.
    Cursors only ever advance to the newest line actually COLLECTED (per-container
    and total caps leave the rest on docker for the next pass), and the caller
    commits them only AFTER a successful ship — a failed ship re-collects the
    same lines next pass, so nothing is lost."""
    entries = []
    advanced = {}
    self_id = _self_container_id()
    for c in (inv or {}).get("docker_containers", []):
        room = min(per_container_cap, total_cap - len(entries))
        if room <= 0:
            break
        if c.get("state") != "running":
            continue
        cid = c.get("id")
        name = (c.get("names") or [cid])[0]
        if self_id and cid and cid.startswith(self_id):
            continue
        cursor = cursors.get(name)
        query = f"/containers/{cid}/logs?stdout=1&stderr=1&timestamps=1"
        # First contact tails; afterwards `since` bounds the fetch server-side.
        query += f"&since={cursor:.9f}" if cursor else f"&tail={tail}"
        raw = docker_get_bytes(query)
        if raw is None:
            continue
        lines = parse_docker_log_lines(demux_docker_logs(raw), cursor)[:room]
        if not lines:
            continue
        entries.extend({"source": name, "ts": epoch, "line": line} for epoch, line in lines)
        advanced[name] = max(epoch for epoch, _ in lines)
    return entries, advanced


def ship_log_batch(state, headers, entries):
    """POST one batch to central's log-ingest endpoint. gzip always (stdlib);
    zstd automatically when this interpreter has it (3.14+) AND central
    advertised it. Raises on failure — the caller decides what to retry."""
    url = state.get("log_ingest") or f"{state['api_base']}/nodes/{state['node_id']}/logs"
    raw = json.dumps({"entries": entries}).encode()
    encoding = "gzip"
    body = gzip.compress(raw)
    if "zstd" in (state.get("log_encodings") or ""):
        try:
            from compression import zstd as _zstd   # stdlib, Python 3.14+
            body = _zstd.compress(raw)
            encoding = "zstd"
        except ImportError:
            pass
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={**headers, "Content-Type": "application/json",
                 "Content-Encoding": encoding,
                 "User-Agent": f"pods-agent/{AGENT_VERSION}"})
    ctx = None
    if os.environ.get("PODS_AGENT_INSECURE", "").lower() == "true":
        ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return json.loads(resp.read().decode() or "{}")


def ship_pending_logs(state, headers, inv, tail, max_batch):
    """Collect + ship one batch (agent self-lines + container logs since cursors).
    Cursors and the self-buffer are committed ONLY on success — except 400/413
    rejections, where the batch is dropped loudly instead of poisoning every
    subsequent pass. Returns the (possibly server-lowered) max batch size."""
    self_snapshot = list(SELF_LOG_BUF)
    budget = max(0, max_batch - len(self_snapshot))
    cursors = dict(state.get("log_cursors") or {})
    entries, advanced = collect_container_logs(inv, cursors, tail, total_cap=budget)
    entries.extend({"source": "agent", "ts": ts, "line": line} for ts, line in self_snapshot)
    if not entries:
        return max_batch

    def commit():
        if advanced:
            cursors.update(advanced)
            state["log_cursors"] = cursors
            save_state(state)
        del SELF_LOG_BUF[:len(self_snapshot)]

    try:
        result = ship_log_batch(state, headers, entries).get("result") or {}
        commit()
        dropped = result.get("dropped") or 0
        if dropped:
            log(f"log ship: central dropped {dropped} of {len(entries)} entr(ies)")
        server_cap = (result.get("retention") or {}).get("max_batch")
        if isinstance(server_cap, int) and 0 < server_cap < max_batch:
            log(f"central caps log batches at {server_cap} — adopting")
            return server_cap
    except urllib.error.HTTPError as e:
        if e.code in (400, 413):
            log(f"log ship rejected ({e.code}): {http_error_detail(e)} — dropping this batch so it can't wedge the loop")
            commit()
        else:
            log(f"log ship failed ({e.code}): {http_error_detail(e)} — retrying next pass")
    except Exception as e:
        log(f"log ship failed ({getattr(e, 'reason', e)}) — retrying next pass")
    return max_batch


# Commands ----------------------------------------------------------------------

def base_headers(tenant=None):
    """Headers common to every agent request: tenant hint + optional dev Host override."""
    h = {}
    if tenant:
        h["X-Pods-Tenant"] = tenant
    host_override = os.environ.get("PODS_HOST_HEADER")
    if host_override:
        h["Host"] = host_override
    return h


def _hint_if_container_localhost(url):
    if ("localhost" in url or "127.0.0.1" in url) and os.path.exists("/.dockerenv"):
        log(
            "hint: inside a container, localhost is the CONTAINER, not the host machine — "
            "re-run with --network host (Linux), or point the URL at an address containers "
            "can reach (e.g. the minikube NodePort)"
        )


def join(url, node_id, claim_token, tenant=None):
    url = url.rstrip("/")
    log_probe_policy()
    caps = detect_capabilities()
    log(f"joining node '{node_id}' at {url} (capabilities: {caps}{', tenant: ' + tenant if tenant else ''})")
    try:
        resp = http_json(
            "POST",
            f"{url}/nodes/{node_id}/join",
            body={"claim_token": claim_token, "agent_version": AGENT_VERSION, "capabilities": caps},
            headers=base_headers(tenant),
        )
    except urllib.error.HTTPError as e:
        log(f"join REFUSED ({e.code}): {http_error_detail(e)}")
        return False
    except urllib.error.URLError as e:
        log(f"join failed — central unreachable: {e.reason}")
        _hint_if_container_localhost(url)
        return False

    result = resp.get("result", {})
    state = {
        "node_id": result.get("node_id", node_id),
        "node_token": result["node_token"],
        "tenant": tenant,
        "api_base": result.get("central_base_url") or url,
        "login_server": result.get("login_server"),
        "checkin_interval_seconds": result.get("checkin_interval_seconds", 60),
        "namespace": result.get("namespace"),
        "joined_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_state(state)
    log(f"joined — state saved to {state_file()}")

    preauthkey = result.get("ts_preauthkey")
    if preauthkey:
        _maybe_join_tailnet(preauthkey, state["login_server"])
    return True


def _maybe_join_tailnet(preauthkey, login_server):
    """Join the pods tailnet — only after confirmation, and never when tailscale is already
    in use (a host runs ONE tailscale; we must not yank an existing tailnet)."""
    ts = tailscale_state()
    if ts is None:
        log("central issued a tailnet preauth key but no `tailscale` binary is present — skipping (API-only mode)")
        return
    if ts == "Running":
        log("tailscale is ALREADY RUNNING on this host — a host runs one tailscale, so leaving it untouched "
            "and continuing in API-only mode. To move this node onto the pods tailnet, disconnect the existing "
            "tailnet first (`tailscale down` / `tailscale logout`), then re-run join.")
        return
    if ts == "NoDaemon":
        log("tailscaled is not running — start it first (`systemctl start tailscaled` or `tailscaled &`), "
            "then re-run join. Continuing in API-only mode.")
        return
    if not host_cmds_allowed(f"tailscale up --authkey *** --login-server={login_server}  (join the pods tailnet)"):
        log("tailnet join skipped — continuing in API-only mode")
        return
    try:
        rc = subprocess.run(
            ["tailscale", "up", "--authkey", preauthkey, f"--login-server={login_server}"],
            timeout=60).returncode
        log("tailnet join " + ("succeeded" if rc == 0 else f"FAILED (rc={rc}) — continuing; agent works over plain API too"))
    except Exception as e:
        log(f"tailnet join errored: {e} — continuing; agent works over plain API too")


def run():
    state = load_state()
    if not state:
        env_url, env_node, env_token = (os.environ.get(k) for k in ("PODS_JOIN_URL", "PODS_NODE_ID", "PODS_JOIN_TOKEN"))
        if env_url and env_node and env_token:
            log("no state found — auto-joining from PODS_JOIN_* environment")
            if not join(env_url, env_node, env_token, tenant=os.environ.get("PODS_TENANT")):
                log("auto-join failed; exiting so the failure is visible (fix the claim and rerun)")
                return 1
            state = load_state()
        else:
            log("no state and no PODS_JOIN_* env — run `pods-agent join` first")
            return 1

    adopt_endpoints = os.environ.get("PODS_AGENT_ADOPT_ENDPOINTS", "true").lower() != "false"
    node_id = state["node_id"]
    headers = {TOKEN_HEADER: state["node_token"], **base_headers(state.get("tenant"))}
    interval = int(state.get("checkin_interval_seconds", 60))
    last_acked_hash = None
    send_full = True  # first checkin always carries the full inventory
    backoff = interval

    # Telemetry (Phase 3): metrics buffer across offline stretches; log shipping
    # driven by per-container cursors persisted in state.
    metrics_enabled = os.environ.get("PODS_AGENT_METRICS", "true").lower() != "false"
    metrics_interval = int(os.environ.get("PODS_AGENT_METRICS_INTERVAL", "60"))
    metrics_buf = []
    last_sample_mono = 0.0
    ship_logs_enabled = os.environ.get("PODS_AGENT_SHIP_LOGS", "true").lower() != "false"
    logs_tail = int(os.environ.get("PODS_AGENT_LOGS_TAIL", "200"))
    logs_max_batch = 2000  # lowered automatically if central advertises a smaller cap

    log_probe_policy()
    log(f"checkin loop starting for node '{node_id}' against {state['api_base']} (interval {interval}s)")
    while not _stop["flag"]:
        caps = detect_capabilities(state.get("namespace"))
        inv = collect_inventory(caps, state.get("namespace"))
        h = inventory_hash(inv)

        # Sample on cadence (one per loop pass at most — during offline backoff the
        # cadence stretches with the loop, and the resulting gaps are honest data:
        # central's charts render them as off periods rather than interpolating).
        if metrics_enabled and time.monotonic() - last_sample_mono >= metrics_interval * 0.9:
            metrics_buf.append(metrics_sample(caps, inv))
            del metrics_buf[:-METRICS_BUF_MAX]
            last_sample_mono = time.monotonic()

        body = {
            "agent_version": AGENT_VERSION,
            "capabilities": caps,
            "status": sample_status(caps, inv),
            "inventory_hash": h,
        }
        if send_full or h != last_acked_hash:
            body["inventory"] = inv
        metrics_batch = list(metrics_buf)
        if metrics_batch:
            body["metrics_samples"] = metrics_batch

        try:
            resp = http_json("POST", f"{state['api_base']}/nodes/{node_id}/checkin", body=body, headers=headers)
            result = resp.get("result", {})
            last_acked_hash = h
            send_full = bool(result.get("resync"))
            interval = int(result.get("poll_after_seconds") or interval)
            backoff = interval
            # 200 = stored (dedupe makes resends harmless) — clear what was sent.
            del metrics_buf[:len(metrics_batch)]

            # Config-as-data: adopt central's currently-published endpoints.
            endpoints = result.get("endpoints") or {}
            new_base = endpoints.get("api_base")
            if adopt_endpoints and new_base and new_base != state["api_base"]:
                log(f"central republished api_base: {state['api_base']} -> {new_base} (adopting)")
                state["api_base"] = new_base
                save_state(state)
            for key in ("log_ingest", "log_encodings"):
                if adopt_endpoints and endpoints.get(key) and endpoints.get(key) != state.get(key):
                    state[key] = endpoints[key]
                    save_state(state)

            if ship_logs_enabled:
                logs_max_batch = ship_pending_logs(state, headers, inv, logs_tail, logs_max_batch)

            try:
                cmds = http_json("GET", f"{state['api_base']}/nodes/{node_id}/commands", headers=headers)
                pending = (cmds.get("result") or {}).get("commands") or []
                if pending:
                    # Executor lands with command dispatch; for now visibility beats silence.
                    log(f"received {len(pending)} command(s) — no executor in v1, ignoring: {pending}")
            except (urllib.error.HTTPError, urllib.error.URLError):
                pass  # command poll is best-effort; next loop retries

        except urllib.error.HTTPError as e:
            if e.code == 403:
                log(f"checkin REJECTED (403): {http_error_detail(e)} — token likely revoked via /regenerate; parking (re-join required)")
                backoff = min(max(backoff * 2, 60), 600)
            else:
                log(f"checkin failed ({e.code}): {http_error_detail(e)}")
                backoff = min(max(backoff * 2, 5), 600)
        except Exception as e:
            reason = getattr(e, "reason", e)
            log(f"central unreachable ({reason}) — offline is a normal state, backing off {min(backoff * 2, 600)}s")
            backoff = min(max(backoff * 2, 5), 600)

        # Interruptible sleep
        deadline = time.monotonic() + backoff
        while not _stop["flag"] and time.monotonic() < deadline:
            time.sleep(1)

    log("stop requested — exiting cleanly")
    return 0


def show_status():
    state = load_state()
    if not state:
        print("no state — not joined")
        return 1
    redacted = dict(state)
    token = redacted.get("node_token", "")
    redacted["node_token"] = f"{token[:8]}…(redacted)" if token else None
    print(json.dumps(redacted, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="pods-agent", description="Tapis Pods edge node agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_join = sub.add_parser("join", help="one-time claim-token exchange")
    p_join.add_argument("--url", required=True, help="central pods API base, e.g. https://tacc.develop.tapis.io/v3/pods")
    p_join.add_argument("--node", required=True, help="node_id")
    p_join.add_argument("--token", required=True, help="single-use claim token (pnc_...)")
    p_join.add_argument("--tenant", default=os.environ.get("PODS_TENANT"), help="tenant id (baked into the join command; identifies the node row when the URL host cannot)")

    sub.add_parser("run", help="checkin loop (auto-joins from PODS_JOIN_* env when no state)")
    sub.add_parser("status", help="print local agent state (token redacted)")

    args = parser.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: _stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: _stop.update(flag=True))

    if args.command == "join":
        return 0 if join(args.url, args.node, args.token, tenant=args.tenant) else 1
    if args.command == "run":
        return run()
    return show_status()


if __name__ == "__main__":
    sys.exit(main())
