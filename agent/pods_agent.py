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
  PODS_AGENT_SHARE_HOSTNAME   "false" withholds the machine hostname from status and
                              bench reports (identity rests on the operator-chosen
                              node_id alone)

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
import fnmatch
import gzip
import hashlib
import http.client
import json
import os
import platform
import re
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

# Startup milestones — ALWAYS-ON (near-free) so every cold start self-documents:
# central renders a startup waterfall from these without any special bench mode.
MILESTONES = {"proc_start": round(STARTED_AT.timestamp(), 3)}


def milestone(name):
    """Record a startup milestone once (first occurrence wins)."""
    MILESTONES.setdefault(name, round(time.time(), 3))


# Settings — env (the box pins it) > central (adopted each heartbeat) > default.
# Central settings arrive in checkin responses (config-as-data) and persist in
# state so restarts keep the last-adopted values while offline.
CENTRAL_SETTINGS = {}

_SETTING_ENVS = {
    "share_hostname": "PODS_AGENT_SHARE_HOSTNAME",
    "metrics": "PODS_AGENT_METRICS",
    "check_docker": "PODS_AGENT_CHECK_DOCKER",
    "check_k8s": "PODS_AGENT_CHECK_K8S",
    "ship_logs": "PODS_AGENT_SHIP_LOGS",
    "metrics_interval": "PODS_AGENT_METRICS_INTERVAL",
    "logs_tail": "PODS_AGENT_LOGS_TAIL",
    "containers": "PODS_AGENT_CONTAINERS",
    "containers_exclude": "PODS_AGENT_CONTAINERS_EXCLUDE",
    "container_label_optin": "PODS_AGENT_CONTAINER_LABEL_OPTIN",
    "log_encoding": "PODS_AGENT_LOG_ENCODING",
    "watch_paths": "PODS_AGENT_WATCH_PATHS",
}
_SETTING_DEFAULTS = {
    "share_hostname": True, "metrics": True, "check_docker": True, "check_k8s": True,
    "ship_logs": True, "metrics_interval": 60, "logs_tail": 200,
    "containers": [], "containers_exclude": [], "container_label_optin": False,
    "log_encoding": "auto",
    "watch_paths": [],
}


def setting(key):
    """Effective value for one setting: env > central > default."""
    env = os.environ.get(_SETTING_ENVS[key])
    default = _SETTING_DEFAULTS[key]
    if env not in (None, ""):
        if key == "watch_paths":
            return parse_watch_env(env)
        if isinstance(default, bool):
            if key == "container_label_optin":
                return env.lower() == "true"
            return env.lower() != "false"
        if isinstance(default, int):
            try:
                return int(env)
            except ValueError:
                return default
        if isinstance(default, list):
            return [part.strip() for part in env.split(",") if part.strip()]
        return env
    if key in CENTRAL_SETTINGS:
        return CENTRAL_SETTINGS[key]
    return default


def env_pinned_settings():
    return sorted(k for k, e in _SETTING_ENVS.items() if os.environ.get(e) not in (None, ""))


def applied_settings():
    return {k: setting(k) for k in _SETTING_ENVS}


def container_selected(c):
    """Log-shipping container filter: allow globs, deny globs, label opt-in."""
    name = (c.get("names") or [c.get("id") or ""])[0]
    allow = setting("containers")
    if allow and not any(fnmatch.fnmatch(name, pat) for pat in allow):
        return False
    if any(fnmatch.fnmatch(name, pat) for pat in setting("containers_exclude")):
        return False
    if setting("container_label_optin") and (c.get("labels") or {}).get("pods.agent.logs") != "true":
        return False
    return True


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
    """docker/k8s probe gates — env > central settings > default-on."""
    return setting("check_docker" if name == "docker" else "check_k8s")

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


def share_hostname():
    """False keeps the machine's hostname out of everything the agent ships
    (status, bench reports); identity then rests on the operator-chosen
    node_id alone. env > central setting > default-true."""
    return setting("share_hostname")


def sample_status(caps, inv=None):
    status = {
        "os": platform.system().lower(),
        "arch": platform.machine(),
        "hostname": socket.gethostname() if share_hostname() else "(withheld)",
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
    status["startup_milestones"] = dict(MILESTONES)
    status["applied_settings"] = applied_settings()
    status["env_pinned"] = env_pinned_settings()
    watches = watch_status_blob()
    if watches:
        status["watches"] = watches
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
                "labels": {k: v for k, v in sorted((c.get("Labels") or {}).items()) if k.startswith("pods.agent.")},
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
    extras = {**watch_extras(), **agent_self_metrics()}
    if extras:
        sample["extras"] = extras
    return sample


# Storage watch --------------------------------------------------------------
# Per-path disk watching, configured via the settings channel (structured
# objects, sanitized server-side) or PODS_AGENT_WATCH_PATHS (JSON blob, or the
# hand-typeable compact form "path[:90%|:200G],path"). Deliberately slow and
# I/O-respectful: filesystem-level statvfs is free and refreshes every loop
# pass; the per-directory du walk runs at most ONE path per pass, on its own
# interval (default 900 s), with a hard time budget — a huge tree yields a
# partial (flagged) size rather than a long scan. No threshold = graph-only.

WATCH_DU_BUDGET_MS = 2000
WATCH_INTERVAL_DEFAULT_S = 900
WATCH_CLEAR_FRACTION = 0.95   # hysteresis: warn at threshold, clear below 95% of it
_WATCH_THRESH_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(%|[KMGT]i?B?|B)$", re.IGNORECASE)
_WATCH_MULT = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}

# path -> {"total","fs_used","used","warn_state","last_walk_mono","partial",
#          "scan_ms","scanned_at","du","threshold"}
WATCH_STATE = {}


def parse_watch_threshold(raw):
    """'90%' -> ('pct', 90.0); '200G' -> ('bytes', n); None if invalid."""
    m = _WATCH_THRESH_RE.match(raw.strip()) if isinstance(raw, str) else None
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2).upper()
    if unit == "%":
        return ("pct", num) if 0 < num <= 100 else None
    if unit == "B":
        return ("bytes", num)
    return ("bytes", num * _WATCH_MULT[unit[0]])


def parse_watch_env(raw):
    """PODS_AGENT_WATCH_PATHS: a JSON list of objects, or compact
    "path[:threshold],path" — the suffix only parses as a threshold when it
    matches the strict size/pct pattern, so colons in paths stay paths."""
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [e for e in parsed if isinstance(e, dict) and
                    isinstance(e.get("path"), str) and e["path"].startswith("/")]
        except ValueError:
            log(f"PODS_AGENT_WATCH_PATHS: invalid JSON — ignoring ({raw[:60]}...)")
            return []
    entries = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        path, sep, suffix = item.rpartition(":")
        if sep and parse_watch_threshold(suffix):
            entries.append({"path": path, "warn": suffix})
        else:
            entries.append({"path": item})
    return [e for e in entries if e["path"].startswith("/")]


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def du_walk(path, budget_ms):
    """Apparent-size directory walk (st_size, no symlink follow, permission
    errors skipped) with a hard wall-clock budget. Returns
    (bytes, truncated, duration_ms)."""
    deadline = time.monotonic() + budget_ms / 1000.0
    total, truncated = 0, False
    stack = [path]
    t0 = time.monotonic()
    while stack:
        if time.monotonic() > deadline:
            truncated = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, truncated, round((time.monotonic() - t0) * 1000, 1)


def scan_watches(state):
    """One loop-pass update of WATCH_STATE from setting('watch_paths').

    Every configured path gets fresh filesystem-level numbers (statvfs — free).
    Directory walks are rationed: only paths with du enabled (default) walk, at
    most one per pass, the most-overdue first. Warn evaluation is hysteretic
    and per-path warn states persist across restarts so a value sitting in the
    hysteresis band cannot flap the ledger on every agent restart.
    """
    entries = setting("watch_paths") or []
    configured = set()
    persisted = state.get("watch_states") or {}
    walk_candidates = []
    for entry in entries:
        path = (entry.get("path") or "").rstrip("/") or "/"
        if not path.startswith("/"):
            continue
        configured.add(path)
        w = WATCH_STATE.setdefault(path, {"warn_state": persisted.get(path, "ok"),
                                          "last_walk_mono": 0.0})
        w["du"] = entry.get("du", True)
        w["threshold"] = entry.get("warn")
        try:
            fs = shutil.disk_usage(path)
            w["total"], w["fs_used"] = fs.total, fs.used
            w.pop("error", None)
        except OSError as e:
            # Report, don't vanish — a containerized agent only sees mounted
            # paths, and a typo'd path should be visible in the UI, not silent.
            w["error"] = f"{type(e).__name__}: {e}"[:120]
            w.pop("total", None)
            w.pop("fs_used", None)
            continue
        if not w["du"]:
            w["used"] = w["fs_used"]
            w["scanned_at"] = round(time.time(), 1)
            w.pop("partial", None)
        else:
            interval = entry.get("interval_s") or WATCH_INTERVAL_DEFAULT_S
            overdue = time.monotonic() - w["last_walk_mono"] - interval
            if overdue >= 0:
                walk_candidates.append((overdue, path))
    for path in [p for p in list(WATCH_STATE) if p not in configured]:
        del WATCH_STATE[path]

    if walk_candidates:
        _, path = max(walk_candidates)
        w = WATCH_STATE[path]
        used, truncated, ms = du_walk(path, WATCH_DU_BUDGET_MS)
        w.update({"used": used, "partial": truncated, "scan_ms": ms,
                  "scanned_at": round(time.time(), 1),
                  "last_walk_mono": time.monotonic()})
        if truncated:
            log(f"watch scan {path}: PARTIAL {_human_bytes(used)} in {ms}ms "
                f"(budget {WATCH_DU_BUDGET_MS}ms hit — size is a floor, not a total)")
        milestone("first_watch_scan")

    # Warn evaluation on whatever is freshest; persist states only on change.
    changed = False
    for path, w in WATCH_STATE.items():
        thresh = parse_watch_threshold(w.get("threshold") or "")
        used, total = w.get("used"), w.get("total")
        if not thresh or used is None or not total:
            w["warn_state"] = "ok" if not thresh else w.get("warn_state", "ok")
            continue
        kind, limit = thresh
        value = (used / total * 100.0) if kind == "pct" else float(used)
        prev = w.get("warn_state", "ok")
        if value >= limit:
            w["warn_state"] = "warn"
        elif value < limit * WATCH_CLEAR_FRACTION:
            w["warn_state"] = "ok"
        # else: inside the hysteresis band — hold the previous state
        if w["warn_state"] != prev:
            log(f"watch {path}: {prev} -> {w['warn_state']} "
                f"({_human_bytes(used)} used, threshold {w.get('threshold')})")
            changed = True
    if changed or set(persisted) != configured:
        state["watch_states"] = {p: w.get("warn_state", "ok") for p, w in WATCH_STATE.items()}
        save_state(state)


def watch_extras():
    """extras gauges for the current metrics sample: disk:<path>:used/:total.
    Values are the last completed scan carried at sample cadence — bounded-stale
    by each path's interval (scanned_at in the status blob keeps that honest)."""
    extras = {}
    for path, w in WATCH_STATE.items():
        if w.get("used") is not None and w.get("total"):
            extras[f"disk:{path}:used"] = w["used"]
            extras[f"disk:{path}:total"] = w["total"]
    return extras


def watch_status_blob():
    """Per-path watch detail for checkin status — central ledgers state EDGES
    (ok->warn, warn->ok) from this, and the UI renders the live table."""
    out = {}
    for path, w in WATCH_STATE.items():
        used, total = w.get("used"), w.get("total")
        entry = {"state": w.get("warn_state", "ok"), "du": w.get("du", True)}
        if w.get("threshold"):
            entry["threshold"] = w["threshold"]
        if used is not None and total:
            entry.update({
                "used": used, "total": total,
                "used_h": _human_bytes(used),
                "pct": round(used / total * 100.0, 1),
            })
        for k in ("partial", "scan_ms", "scanned_at", "error"):
            if w.get(k) is not None:
                entry[k] = w[k]
        out[path] = entry
    return out


# Agent self-metrics -----------------------------------------------------------
# The watcher's own footprint, from /proc (Linux; keys are simply absent
# elsewhere) — proof at a glance that the agent stays tiny. Rides the same
# extras rail as storage watch: agent:rss_bytes (data-scaled), agent:cpu_pct
# (static cap = 100% of one core), agent:fds with the soft ulimit as its
# natural static cap. Gated by the same 'metrics' setting as everything else.

_AGENT_CPU = {"last_total": None, "last_mono": None}


def agent_self_metrics():
    extras = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    extras["agent:rss_bytes"] = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/proc/self/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()  # skip comm (may hold spaces)
        total = (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")
        now = time.monotonic()
        last_t, last_m = _AGENT_CPU["last_total"], _AGENT_CPU["last_mono"]
        _AGENT_CPU["last_total"], _AGENT_CPU["last_mono"] = total, now
        if last_t is not None and now > last_m:
            extras["agent:cpu_pct"] = round(
                min(100.0, max(0.0, (total - last_t) / (now - last_m) * 100.0)), 2)
    except (OSError, ValueError, IndexError):
        pass
    try:
        extras["agent:fds"] = len(os.listdir("/proc/self/fd"))
        import resource
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft and soft != resource.RLIM_INFINITY:
            extras["agent:fds:total"] = soft
    except (OSError, ImportError, ValueError):
        pass
    return extras


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
        if c.get("state") != "running" or not container_selected(c):
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
    pref = setting("log_encoding")
    encoding = "gzip"
    body = gzip.compress(raw)
    if pref == "identity":
        encoding, body = "identity", raw
    elif pref in ("auto", "zstd") and "zstd" in (state.get("log_encodings") or ""):
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
        milestone("first_logs_ack")
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


def record_container_milestones():
    """When containerized, pull the container's own Created/StartedAt from docker
    inspect (socket permitting) so the startup waterfall begins at container
    creation, not process start."""
    if not os.path.exists("/.dockerenv"):
        return
    info = docker_get(f"/containers/{socket.gethostname()}/json")
    if not info:
        return
    created = _parse_rfc3339_nano(info.get("Created", ""))
    started = _parse_rfc3339_nano((info.get("State") or {}).get("StartedAt", ""))
    if created:
        MILESTONES.setdefault("container_created", round(created, 3))
    if started:
        MILESTONES.setdefault("container_started", round(started, 3))


# Bench — dispatcher type=bench (research/onboarding suite) ----------------------
# Every row measures BOTH dimensions wherever they exist: wall time AND bytes.
# Settings arrive pre-sanitized by central (sanitize_bench_settings) — the agent
# still treats them defensively.

_BENCH_WORDS = ("request handled upstream latency queue worker cache miss hit "
                "retry timeout connect flush batch shipped accepted stored").split()


def synth_line(kind, size, i):
    """One deterministic synthetic log line of exactly `size` bytes."""
    if kind == "json":
        base = (f'{{"ts":"2026-07-30T12:00:{i % 60:02d}Z","level":"info","seq":{i},'
                f'"path":"/api/v1/items/{i % 997}","ms":{i % 97},"msg":"')
        body = " ".join(_BENCH_WORDS[(i + j) % len(_BENCH_WORDS)] for j in range(max(1, size // 8)))
        line = (base + body)[:max(len(base) + 2, size) - 2] + '"}'
    elif kind == "entropy":
        import base64
        line = base64.b64encode(os.urandom(size)).decode()[:size]
    else:  # text
        line = " ".join(_BENCH_WORDS[(i + j) % len(_BENCH_WORDS)] for j in range(size // 4))
    return (line[:size]).ljust(size, "x")


def synth_corpus(kind, line_bytes, count):
    return [synth_line(kind, line_bytes, i) for i in range(count)]


def real_corpus(inv, max_lines=500):
    """The node's own recent lines (containers tail + agent self-log) — the most
    honest compression corpus there is."""
    lines = [l for _, l in SELF_LOG_BUF[-100:]]
    for c in (inv or {}).get("docker_containers", [])[:5]:
        if c.get("state") != "running":
            continue
        raw = docker_get_bytes(f"/containers/{c.get('id')}/logs?stdout=1&stderr=1&tail=100")
        if raw:
            lines.extend(l for _, l in parse_docker_log_lines(demux_docker_logs(raw), None))
        if len(lines) >= max_lines:
            break
    return lines[:max_lines]


def bench_encoders(encodings):
    """[(name, compress_fn, decompress_fn)] for the encodings this interpreter
    supports; unsupported zstd picks are skipped (reported by omission)."""
    out = []
    for enc in encodings:
        name, _, level = enc.partition(":")
        if name == "identity":
            out.append((enc, lambda b: b, lambda b: b))
        elif name == "gzip":
            lvl = int(level or 6)
            out.append((enc,
                        lambda b, l=lvl: gzip.compress(b, compresslevel=l),
                        gzip.decompress))
        elif name == "zstd":
            try:
                from compression import zstd as _z   # stdlib 3.14+
                lvl = int(level or 3)
                out.append((enc, lambda b, l=lvl: _z.compress(b, level=l), _z.decompress))
            except ImportError:
                continue
    return out


def _entries_body(lines):
    now = time.time()
    return json.dumps({"entries": [
        {"source": "bench", "ts": now, "line": l} for l in lines]}).encode()


def bench_compression(params, inv):
    """Ratio × speed matrix over every (corpus, encoding) pair — local CPU only."""
    corpora = {}
    for kind in params["corpora"]:
        if kind == "real":
            lines = real_corpus(inv)
            if lines:
                corpora["real"] = lines
        else:
            for size in params["line_bytes"]:
                corpora[f"{kind}@{size}B"] = synth_corpus(kind, size, params["line_counts"][0])
    rows = []
    for cname, lines in corpora.items():
        raw = _entries_body(lines)
        for enc, comp, decomp in bench_encoders(params["encodings"]):
            t0 = time.perf_counter()
            wire = comp(raw)
            compress_ms = (time.perf_counter() - t0) * 1000
            t1 = time.perf_counter()
            decomp(wire)
            decompress_ms = (time.perf_counter() - t1) * 1000
            rows.append({
                "corpus": cname, "lines": len(lines), "encoding": enc,
                "raw_bytes": len(raw), "wire_bytes": len(wire),
                "ratio": round(len(raw) / max(1, len(wire)), 2),
                "compress_ms": round(compress_ms, 2),
                "decompress_ms": round(decompress_ms, 2),
            })
    return rows


def bench_ingest(params, state, headers):
    """The full wire path per encoding × batch volume: wall time, bytes on wire,
    and the server's own decode/insert split from the timings block. dry_run
    (default) times everything but stores nothing."""
    mid_size = params["line_bytes"][len(params["line_bytes"]) // 2]
    dry = "true" if params.get("dry_run", True) else "false"
    url_base = (state.get("log_ingest") or
                f"{state['api_base']}/nodes/{state['node_id']}/logs")
    rows = []
    for count in params["line_counts"]:
        raw = _entries_body(synth_corpus("json", mid_size, count))
        for enc, comp, _ in bench_encoders(params["encodings"]):
            body = comp(raw)
            req_headers = {**headers, "Content-Type": "application/json",
                           "User-Agent": f"pods-agent/{AGENT_VERSION}"}
            if not enc.startswith("identity"):
                req_headers["Content-Encoding"] = enc.partition(":")[0]
            req = urllib.request.Request(f"{url_base}?dry_run={dry}", data=body,
                                         method="POST", headers=req_headers)
            ctx = ssl._create_unverified_context() if os.environ.get(
                "PODS_AGENT_INSECURE", "").lower() == "true" else None
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                    parsed = json.loads(resp.read().decode() or "{}")
            except Exception as e:
                rows.append({"encoding": enc, "lines": count, "error": str(e)[:200]})
                continue
            wall_ms = (time.perf_counter() - t0) * 1000
            server = (parsed.get("result") or {}).get("timings") or {}
            rows.append({
                "encoding": enc, "lines": count,
                "raw_bytes": len(raw), "wire_bytes": len(body),
                "wall_ms": round(wall_ms, 2),
                "server_decode_ms": server.get("decode_ms"),
                "server_insert_ms": server.get("insert_ms"),
                "dry_run": params.get("dry_run", True),
            })
    return rows


def _pctl(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(len(s) * p / 100))], 2)


def bench_latency(params, state, headers):
    """Connection-leg waterfall to central (DNS/TCP/TLS/TTFB/total over a raw
    socket — any HTTP status counts, only the legs matter) + wall-clock
    percentiles for the authed checkin (auth + DB cost included)."""
    from urllib.parse import urlparse
    u = urlparse(state["api_base"])
    host = u.hostname or "localhost"
    port = u.port or (443 if u.scheme == "https" else 80)
    n = params["probe_count"]

    legs = {"dns_ms": [], "tcp_ms": [], "tls_ms": [], "ttfb_ms": [], "total_ms": []}
    for _ in range(n):
        try:
            t0 = time.perf_counter()
            addr = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0][4]
            t1 = time.perf_counter()
            s = socket.create_connection(addr[:2], timeout=10)
            t2 = time.perf_counter()
            t3 = t2
            if u.scheme == "https":
                sctx = ssl._create_unverified_context() if os.environ.get(
                    "PODS_AGENT_INSECURE", "").lower() == "true" else ssl.create_default_context()
                s = sctx.wrap_socket(s, server_hostname=host)
                t3 = time.perf_counter()
            s.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            s.recv(1)
            t4 = time.perf_counter()
            s.close()
            legs["dns_ms"].append((t1 - t0) * 1000)
            legs["tcp_ms"].append((t2 - t1) * 1000)
            legs["tls_ms"].append((t3 - t2) * 1000)
            legs["ttfb_ms"].append((t4 - t3) * 1000)
            legs["total_ms"].append((t4 - t0) * 1000)
        except Exception:
            continue

    checkin_ms = []
    for _ in range(min(n, 10)):
        t0 = time.perf_counter()
        try:
            http_json("POST", f"{state['api_base']}/nodes/{state['node_id']}/checkin",
                      body={}, headers=headers)
            checkin_ms.append((time.perf_counter() - t0) * 1000)
        except Exception:
            continue

    def agg(vals):
        return {"p50": _pctl(vals, 50), "p95": _pctl(vals, 95),
                "min": _pctl(vals, 0), "max": _pctl(vals, 100), "n": len(vals)}

    return {
        "target": f"{host}:{port}",
        "legs": {k: agg(v) for k, v in legs.items()},
        "checkin_wall": agg(checkin_ms),
    }


def bench_docker(inv, caps):
    """Docker socket cost — time AND bytes for what the agent actually does:
    the inventory scan and per-container log fetches."""
    if "runtime.docker" not in caps:
        return {"available": False}
    t0 = time.perf_counter()
    raw = docker_get_bytes("/containers/json?all=true")
    scan_ms = (time.perf_counter() - t0) * 1000
    containers = (inv or {}).get("docker_containers", [])
    fetches = []
    for c in [c for c in containers if c.get("state") == "running"][:5]:
        t1 = time.perf_counter()
        body = docker_get_bytes(f"/containers/{c.get('id')}/logs?stdout=1&stderr=1&tail=100")
        fetches.append({
            "container": (c.get("names") or ["?"])[0],
            "ms": round((time.perf_counter() - t1) * 1000, 2),
            "bytes": len(body) if body else 0,
        })
    return {
        "available": True,
        "container_count": len(containers),
        "inventory_scan_ms": round(scan_ms, 2),
        "inventory_scan_bytes": len(raw) if raw else 0,
        "log_fetches": fetches,
    }


def bench_payload(caps, inv):
    """Checkin payload anatomy — what each piece costs on the wire, json + gzip.
    Proves the hash-gating discipline: steady-state vs full-inventory."""
    status = sample_status(caps, inv)
    h = inventory_hash(inv)
    heartbeat = {"agent_version": AGENT_VERSION, "capabilities": caps,
                 "status": status, "inventory_hash": h}
    full = {**heartbeat, "inventory": inv}
    sample = metrics_sample(caps, inv)
    rows = {}
    for name, obj in (("heartbeat", heartbeat), ("heartbeat_plus_inventory", full),
                      ("one_metrics_sample", sample)):
        raw = json.dumps(obj).encode()
        rows[name] = {"json_bytes": len(raw), "gzip_bytes": len(gzip.compress(raw))}
    return rows


def bench_clock(state, headers):
    """Agent clock vs the server's Date header — edge log timestamps ride edge
    clocks, so skew is an operational number worth knowing."""
    import email.utils
    req = urllib.request.Request(
        f"{state['api_base']}/nodes/{state['node_id']}/commands",
        headers={**headers, "User-Agent": f"pods-agent/{AGENT_VERSION}"})
    ctx = ssl._create_unverified_context() if os.environ.get(
        "PODS_AGENT_INSECURE", "").lower() == "true" else None
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            date_hdr = resp.headers.get("Date")
        t1 = time.time()
    except Exception as e:
        return {"error": str(e)[:200]}
    if not date_hdr:
        return {"error": "no Date header"}
    server = email.utils.parsedate_to_datetime(date_hdr).timestamp()
    midpoint = (t0 + t1) / 2
    return {"skew_ms": round((midpoint - server) * 1000, 1),
            "rtt_ms": round((t1 - t0) * 1000, 1),
            "note": "positive = agent clock ahead of server (±500ms noise: Date has 1s resolution)"}


def run_bench_suite(params, state, headers, caps, inv):
    """Assemble the warm-suite report. Each section is independently fault-
    isolated — one failure records an error, never aborts the run."""
    t0 = time.perf_counter()
    zstd_ok = True
    try:
        from compression import zstd as _z  # noqa: F401
    except ImportError:
        zstd_ok = False
    report = {"meta": {
        "agent_version": AGENT_VERSION,
        "hostname": socket.gethostname() if share_hostname() else "(withheld)",
        "started": round(time.time(), 3),
        "settings": params,
        "zstd_supported": zstd_ok,
    }}
    sections = (
        ("compression", lambda: bench_compression(params, inv)),
        ("ingest", lambda: bench_ingest(params, state, headers)),
        ("latency", lambda: bench_latency(params, state, headers)),
        ("docker", lambda: bench_docker(inv, caps)),
        ("payload", lambda: bench_payload(caps, inv)),
        ("clock", lambda: bench_clock(state, headers)),
    )
    for name, fn in sections:
        try:
            report[name] = fn()
        except Exception as e:
            report[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
    report["meta"]["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return report


def post_command_result(state, headers, command_id, status, result):
    try:
        http_json("POST",
                  f"{state['api_base']}/nodes/{state['node_id']}/commands/{command_id}/result",
                  body={"status": status, "result": result}, headers=headers, timeout=30)
        return True
    except Exception as e:
        log(f"command result post failed for {command_id}: {getattr(e, 'reason', e)}")
        return False


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
    milestone("caps_detected")
    milestone("join_start")
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
    milestone("join_ok")
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
        if rc == 0:
            milestone("tailnet_join_ok")
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
    CENTRAL_SETTINGS.update(state.get("central_settings") or {})
    metrics_buf = []
    last_sample_mono = 0.0
    logs_max_batch = 2000  # lowered automatically if central advertises a smaller cap

    log_probe_policy()
    log(f"checkin loop starting for node '{node_id}' against {state['api_base']} (interval {interval}s)")
    record_container_milestones()
    while not _stop["flag"]:
        caps = detect_capabilities(state.get("namespace"))
        milestone("caps_detected")
        inv = collect_inventory(caps, state.get("namespace"))
        h = inventory_hash(inv)

        # Storage watch: statvfs refresh every pass (free), at most one budgeted
        # du walk per pass on its own interval — see scan_watches.
        try:
            scan_watches(state)
        except Exception as e:
            log(f"watch scan error (non-fatal): {e}")

        # Sample on cadence (one per loop pass at most — during offline backoff the
        # cadence stretches with the loop, and the resulting gaps are honest data:
        # central's charts render them as off periods rather than interpolating).
        if setting("metrics") and time.monotonic() - last_sample_mono >= setting("metrics_interval") * 0.9:
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
            milestone("first_checkin_ok")
            # 200 = stored (dedupe makes resends harmless) — clear what was sent.
            if metrics_batch:
                milestone("first_metrics_flush")
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

            # Settings channel: adopt central's per-node agent settings (env pins win
            # inside setting(); the settings themselves are whitelisted server-side).
            new_settings = result.get("settings")
            if new_settings is not None and new_settings != CENTRAL_SETTINGS:
                log(f"central settings adopted: {json.dumps(new_settings, sort_keys=True)}")
                CENTRAL_SETTINGS.clear()
                CENTRAL_SETTINGS.update(new_settings)
                state["central_settings"] = dict(new_settings)
                save_state(state)

            if setting("ship_logs"):
                logs_max_batch = ship_pending_logs(state, headers, inv, setting("logs_tail"), logs_max_batch)

            try:
                cmds = http_json("GET", f"{state['api_base']}/nodes/{node_id}/commands", headers=headers)
                pending = (cmds.get("result") or {}).get("commands") or []
                for cmd in pending:
                    cid, ctype = cmd.get("command_id"), cmd.get("type")
                    if ctype == "bench":
                        log(f"bench command {cid} — running warm suite (blocks this loop pass, ~10-20s)")
                        try:
                            report = run_bench_suite(cmd.get("params") or {}, state, headers, caps, inv)
                            post_command_result(state, headers, cid, "done", report)
                            log(f"bench {cid} complete in {report['meta']['duration_ms']}ms")
                        except Exception as e:
                            post_command_result(state, headers, cid, "error",
                                                {"error": f"{type(e).__name__}: {e}"[:300]})
                            log(f"bench {cid} FAILED: {e}")
                    else:
                        log(f"unsupported command type '{ctype}' ({cid}) — reporting error")
                        post_command_result(state, headers, cid, "error",
                                            {"error": f"unsupported command type '{ctype}'"})
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
