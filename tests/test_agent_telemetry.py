"""
Unit tests for the agent's Phase 3 telemetry: docker log demux/parse/cursoring,
metrics sampling, and ship bookkeeping (commit-on-success, poison-pill on 4xx).

No cluster, no docker, no network — everything is patched by hand (plain
functions, no pytest fixtures) so this runs under pytest OR the bare
mini-runner used when the local pytest shim is absent:
  python3 -c "import importlib.util,...; exec + call test_*"
The agent lives in agent/ (not mounted into the pods-api container), so this
file skips in-container like tests/test_agent_k8s.py.
"""

import gzip
import io
import json
import os
import sys
import urllib.error

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "agent")
if not os.path.exists(os.path.join(AGENT_DIR, "pods_agent.py")):
    try:
        import pytest
        pytest.skip("agent/ not present (in-container run) — agent tests run locally", allow_module_level=True)
    except ImportError:
        raise SystemExit("agent/ not present")
sys.path.insert(0, AGENT_DIR)

import pods_agent as agent


def frame(stream_type, payload: bytes) -> bytes:
    return bytes([stream_type, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


# ── demux / timestamp parse / line parse ─────────────────────────────────────

def test_demux_multiplexed_frames():
    raw = frame(1, b"out line\n") + frame(2, b"err line\n")
    assert agent.demux_docker_logs(raw) == "out line\nerr line\n"


def test_demux_raw_tty_fallback():
    raw = b"2026-07-30T12:00:00.000000000Z plain tty line\n"
    assert agent.demux_docker_logs(raw) == raw.decode()
    # malformed frame header (impossible length) → raw fallback, not an exception;
    # invalid utf-8 bytes come back as U+FFFD replacements
    out = agent.demux_docker_logs(b"\x01\x00\x00\x00\xff\xff\xff\xff")
    assert out.startswith("\x01\x00\x00\x00") and "�" in out


def test_parse_rfc3339_nano():
    assert agent._parse_rfc3339_nano("2026-07-30T12:00:00.500000000Z") == 1785412800.5
    assert agent._parse_rfc3339_nano("2026-07-30T12:00:00Z") == 1785412800.0
    assert agent._parse_rfc3339_nano("garbage") is None
    assert agent._parse_rfc3339_nano("") is None


def test_parse_docker_log_lines_cursor_strictness():
    text = ("2026-07-30T12:00:00.000000000Z at cursor\n"
            "2026-07-30T12:00:00.500000000Z after cursor\n"
            "no-timestamp-prefix-line\n")
    lines = agent.parse_docker_log_lines(text, after_epoch=1785412800.0)
    # at-cursor line filtered (docker `since` is inclusive; we re-filter exactly),
    # unparseable-prefix line kept and ordered at the cursor edge
    assert ("no-timestamp-prefix-line" in [l for _, l in lines])
    assert [l for _, l in lines if "after" in l] == ["after cursor"]
    assert all(l != "at cursor" for _, l in lines)


# ── metrics_sample ───────────────────────────────────────────────────────────

def test_metrics_sample_shape_and_counts():
    inv = {
        "docker_containers": [{"state": "running"}, {"state": "exited"}],
        "k8s_pods": [{"phase": "Running"}, {"phase": "Pending"}],
    }
    s = agent.metrics_sample(["runtime.docker"], inv)
    assert isinstance(s["ts"], float)
    assert s["docker_running"] == 1 and s["docker_total"] == 2
    assert s["k8s_running"] == 1 and s["k8s_total"] == 2
    # host gauges present on Linux runners
    assert "cpu_count" in s
    assert s.get("mem_total_bytes", 1) > 0


def test_metrics_sample_no_runtimes():
    s = agent.metrics_sample([], {})
    assert "docker_running" not in s and "k8s_running" not in s


# ── collect_container_logs ───────────────────────────────────────────────────

def with_patched(obj, name, value):
    """Tiny context manager for manual monkeypatching."""
    class _P:
        def __enter__(self):
            self.old = getattr(obj, name)
            setattr(obj, name, value)
        def __exit__(self, *a):
            setattr(obj, name, self.old)
    return _P()


INV = {"docker_containers": [
    {"id": "aaa111222333", "names": ["web"], "state": "running"},
    {"id": "bbb111222333", "names": ["db"], "state": "exited"},
]}


def test_collect_first_contact_uses_tail():
    seen = []
    def fake_get(path):
        seen.append(path)
        return frame(1, b"2026-07-30T12:00:00.000000000Z hello\n")
    with with_patched(agent, "docker_get_bytes", fake_get):
        entries, advanced = agent.collect_container_logs(INV, {}, tail=99)
    assert "tail=99" in seen[0] and "since=" not in seen[0]
    assert len(seen) == 1                       # exited container skipped
    assert entries == [{"source": "web", "ts": 1785412800.0, "line": "hello"}]
    assert advanced == {"web": 1785412800.0}


def test_collect_uses_since_cursor_and_filters():
    def fake_get(path):
        assert "since=1785412800.000000000" in path
        return frame(1, b"2026-07-30T12:00:00.000000000Z old\n"
                        b"2026-07-30T12:00:01.000000000Z new\n")
    with with_patched(agent, "docker_get_bytes", fake_get):
        entries, advanced = agent.collect_container_logs(INV, {"web": 1785412800.0}, tail=200)
    assert [e["line"] for e in entries] == ["new"]
    assert advanced == {"web": 1785412801.0}


def test_collect_total_cap_stops_collection():
    def fake_get(path):
        return frame(1, b"".join(
            f"2026-07-30T12:00:{i:02d}.000000000Z line{i}\n".encode() for i in range(10)))
    with with_patched(agent, "docker_get_bytes", fake_get):
        entries, advanced = agent.collect_container_logs(INV, {}, tail=200, total_cap=3)
    assert len(entries) == 3
    # cursor advanced only to the newest COLLECTED line — the rest re-fetch next pass
    assert advanced["web"] == entries[-1]["ts"]


def test_collect_docker_failure_is_quiet():
    with with_patched(agent, "docker_get_bytes", lambda path: None):
        entries, advanced = agent.collect_container_logs(INV, {}, tail=200)
    assert entries == [] and advanced == {}


# ── ship_log_batch encoding ──────────────────────────────────────────────────

def test_ship_log_batch_gzips():
    captured = {}
    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None, context=None):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = req.data
        captured["url"] = req.full_url
        return FakeResp(json.dumps({"result": {"accepted": 1}}).encode())
    state = {"api_base": "https://x/v3/pods", "node_id": "n1"}
    with with_patched(urllib.request, "urlopen", fake_urlopen):
        out = agent.ship_log_batch(state, {"X-Pods-Node-Token": "t"}, [{"source": "agent", "ts": 1.0, "line": "x"}])
    assert out["result"]["accepted"] == 1
    assert captured["url"] == "https://x/v3/pods/nodes/n1/logs"
    assert captured["headers"]["content-encoding"] == "gzip"
    decoded = json.loads(gzip.decompress(captured["body"]))
    assert decoded["entries"][0]["line"] == "x"


def test_ship_log_batch_prefers_adopted_ingest_url():
    captured = {}
    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        return FakeResp(b"{}")
    state = {"api_base": "https://x/v3/pods", "node_id": "n1",
             "log_ingest": "https://republished/v3/pods/nodes/n1/logs"}
    with with_patched(urllib.request, "urlopen", fake_urlopen):
        agent.ship_log_batch(state, {}, [{"source": "agent", "ts": 1.0, "line": "x"}])
    assert captured["url"] == "https://republished/v3/pods/nodes/n1/logs"


# ── ship_pending_logs bookkeeping ────────────────────────────────────────────

def _pending_setup():
    agent.SELF_LOG_BUF.clear()
    agent.SELF_LOG_BUF.append((1785412800.0, "agent line"))
    state = {"api_base": "https://x/v3/pods", "node_id": "n1"}
    return state


def test_ship_pending_commits_on_success():
    state = _pending_setup()
    def fake_ship(s, h, entries):
        assert {e["source"] for e in entries} == {"web", "agent"}
        return {"result": {"accepted": len(entries), "retention": {"max_batch": 5000}}}
    def fake_get(path):
        return frame(1, b"2026-07-30T12:00:01.000000000Z weblog\n")
    saved = []
    with with_patched(agent, "ship_log_batch", fake_ship), \
         with_patched(agent, "docker_get_bytes", fake_get), \
         with_patched(agent, "save_state", lambda s: saved.append(dict(s))):
        out = agent.ship_pending_logs(state, {}, INV, tail=200, max_batch=2000)
    assert out == 2000                                  # server cap higher → keep ours
    assert state["log_cursors"] == {"web": 1785412801.0}
    assert saved, "cursors must persist via save_state"
    assert agent.SELF_LOG_BUF == []                     # self lines acked


def test_ship_pending_keeps_state_on_failure():
    state = _pending_setup()
    def fake_ship(s, h, entries):
        raise urllib.error.URLError("down")
    with with_patched(agent, "ship_log_batch", fake_ship), \
         with_patched(agent, "docker_get_bytes", lambda p: None), \
         with_patched(agent, "save_state", lambda s: None):
        agent.ship_pending_logs(state, {}, INV, tail=200, max_batch=2000)
    assert "log_cursors" not in state
    assert len([l for _, l in agent.SELF_LOG_BUF if l == "agent line"]) == 1  # retained for retry


def test_ship_pending_drops_batch_on_400():
    state = _pending_setup()
    def fake_ship(s, h, entries):
        raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"{}"))
    def fake_get(path):
        return frame(1, b"2026-07-30T12:00:01.000000000Z weblog\n")
    with with_patched(agent, "ship_log_batch", fake_ship), \
         with_patched(agent, "docker_get_bytes", fake_get), \
         with_patched(agent, "save_state", lambda s: None):
        agent.ship_pending_logs(state, {}, INV, tail=200, max_batch=2000)
    # poison-pill guard: cursors advance + self lines cleared so the loop can't wedge
    assert state["log_cursors"] == {"web": 1785412801.0}
    assert all(l != "agent line" for _, l in agent.SELF_LOG_BUF)


def test_ship_pending_adopts_lower_server_cap():
    state = _pending_setup()
    def fake_ship(s, h, entries):
        return {"result": {"accepted": len(entries), "retention": {"max_batch": 100}}}
    with with_patched(agent, "ship_log_batch", fake_ship), \
         with_patched(agent, "docker_get_bytes", lambda p: None), \
         with_patched(agent, "save_state", lambda s: None):
        out = agent.ship_pending_logs(state, {}, INV, tail=200, max_batch=2000)
    assert out == 100
