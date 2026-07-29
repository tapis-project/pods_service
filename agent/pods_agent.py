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

AGENT_VERSION = "0.2.0"
TOKEN_HEADER = "X-Pods-Node-Token"
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
K8S_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
STARTED_AT = datetime.now(timezone.utc)

_stop = {"flag": False}


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


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
    # Metrics-lite: cheap host + workload numbers every checkin (sub-KB — the full
    # metrics pipeline with history tables is Phase 3).
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

    log_probe_policy()
    log(f"checkin loop starting for node '{node_id}' against {state['api_base']} (interval {interval}s)")
    while not _stop["flag"]:
        caps = detect_capabilities(state.get("namespace"))
        inv = collect_inventory(caps, state.get("namespace"))
        h = inventory_hash(inv)
        body = {
            "agent_version": AGENT_VERSION,
            "capabilities": caps,
            "status": sample_status(caps, inv),
            "inventory_hash": h,
        }
        if send_full or h != last_acked_hash:
            body["inventory"] = inv

        try:
            resp = http_json("POST", f"{state['api_base']}/nodes/{node_id}/checkin", body=body, headers=headers)
            result = resp.get("result", {})
            last_acked_hash = h
            send_full = bool(result.get("resync"))
            interval = int(result.get("poll_after_seconds") or interval)
            backoff = interval

            # Config-as-data: adopt central's currently-published endpoints.
            endpoints = result.get("endpoints") or {}
            new_base = endpoints.get("api_base")
            if adopt_endpoints and new_base and new_base != state["api_base"]:
                log(f"central republished api_base: {state['api_base']} -> {new_base} (adopting)")
                state["api_base"] = new_base
                save_state(state)

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
