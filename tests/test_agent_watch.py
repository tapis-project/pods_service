"""
Unit tests for the agent's storage-watch machinery — env parsing (JSON blob +
compact form), budgeted du walk, hysteresis warn/clear transitions, and the
extras/status blobs. No network, no docker; real tmpdirs for the walk. Runs
under pytest OR the bare mini-runner; skips in-container like the other agent
test files.
"""

import os
import shutil
import sys
import tempfile

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "agent")
if not os.path.exists(os.path.join(AGENT_DIR, "pods_agent.py")):
    try:
        import pytest
        pytest.skip("agent/ not present (in-container run)", allow_module_level=True)
    except ImportError:
        raise SystemExit("agent/ not present")
sys.path.insert(0, AGENT_DIR)

import pods_agent as agent


def _reset():
    agent.WATCH_STATE.clear()
    agent.CENTRAL_SETTINGS.clear()
    os.environ.pop("PODS_AGENT_WATCH_PATHS", None)
    # cpu_pct is a delta vs the previous sample; the whole test process shares one
    # _AGENT_CPU, so a prior test's sample leaks a delta into "first call" tests.
    # Reset it so agent_self_metrics() behaves as a fresh process (test isolation).
    agent._AGENT_CPU.update({"last_total": None, "last_mono": None})


def test_parse_watch_env_compact_and_json():
    _reset()
    assert agent.parse_watch_env("/scratch:90%,/data") == [
        {"path": "/scratch", "warn": "90%"}, {"path": "/data"}]
    # a suffix that is NOT a valid threshold stays part of the path
    assert agent.parse_watch_env("/weird:dir") == [{"path": "/weird:dir"}]
    assert agent.parse_watch_env('[{"path": "/s", "warn": "200G", "du": false}]') == [
        {"path": "/s", "warn": "200G", "du": False}]
    assert agent.parse_watch_env("[not json") == []
    assert agent.parse_watch_env("relative,also-relative") == []


def test_setting_env_overrides_central_watch_paths():
    _reset()
    agent.CENTRAL_SETTINGS["watch_paths"] = [{"path": "/from-central"}]
    assert agent.setting("watch_paths") == [{"path": "/from-central"}]
    os.environ["PODS_AGENT_WATCH_PATHS"] = "/pinned:50%"
    try:
        assert agent.setting("watch_paths") == [{"path": "/pinned", "warn": "50%"}]
    finally:
        _reset()


def test_du_walk_counts_and_budget():
    d = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(d, "sub"))
        with open(os.path.join(d, "a.bin"), "wb") as f:
            f.write(b"x" * 1000)
        with open(os.path.join(d, "sub", "b.bin"), "wb") as f:
            f.write(b"y" * 500)
        total, truncated, ms = agent.du_walk(d, 5000)
        assert total == 1500 and truncated is False
        # zero budget: deadline hit before any directory is scanned
        total0, truncated0, _ = agent.du_walk(d, 0)
        assert truncated0 is True and total0 == 0
    finally:
        shutil.rmtree(d)


def test_scan_watches_warn_and_hysteresis_clear():
    _reset()
    d = tempfile.mkdtemp()
    saved = []
    try:
        with open(os.path.join(d, "big.bin"), "wb") as f:
            f.write(b"z" * 10_000)
        agent.CENTRAL_SETTINGS["watch_paths"] = [{"path": d, "warn": "5K"}]
        old_save = agent.save_state
        agent.save_state = lambda s: saved.append(dict(s))
        try:
            state = {}
            agent.scan_watches(state)                     # 10 KB >= 5 K -> warn
            w = agent.WATCH_STATE[d.rstrip("/")]
            assert w["warn_state"] == "warn" and w["used"] == 10_000
            assert state["watch_states"][d.rstrip("/")] == "warn"

            # shrink into the hysteresis band (between 95% and 100% of 5 K): holds warn
            os.remove(os.path.join(d, "big.bin"))
            with open(os.path.join(d, "mid.bin"), "wb") as f:
                f.write(b"z" * 5000)                       # 5000 < 5120, >= 4864
            w["last_walk_mono"] = 0.0                      # force the walk to be due
            agent.scan_watches(state)
            assert agent.WATCH_STATE[d.rstrip("/")]["warn_state"] == "warn"

            # drop clearly below the clear threshold: ok
            os.remove(os.path.join(d, "mid.bin"))
            agent.WATCH_STATE[d.rstrip("/")]["last_walk_mono"] = 0.0
            agent.scan_watches(state)
            assert agent.WATCH_STATE[d.rstrip("/")]["warn_state"] == "ok"
        finally:
            agent.save_state = old_save
    finally:
        shutil.rmtree(d)
        _reset()


def test_watch_extras_and_status_blob():
    _reset()
    agent.WATCH_STATE["/s"] = {
        "warn_state": "warn", "du": True, "threshold": "90%",
        "used": 900, "total": 1000, "scan_ms": 12.5, "scanned_at": 1785412800.0,
        "partial": False, "last_walk_mono": 0.0,
    }
    extras = agent.watch_extras()
    assert extras == {"disk:/s:used": 900, "disk:/s:total": 1000}
    blob = agent.watch_status_blob()
    assert blob["/s"]["state"] == "warn" and blob["/s"]["pct"] == 90.0
    assert blob["/s"]["used_h"] == "900 B" and blob["/s"]["threshold"] == "90%"
    _reset()


def test_watch_graph_only_never_warns():
    _reset()
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "f.bin"), "wb") as f:
            f.write(b"x" * 100_000)
        agent.CENTRAL_SETTINGS["watch_paths"] = [{"path": d}]   # no warn threshold
        old_save = agent.save_state
        agent.save_state = lambda s: None
        try:
            agent.scan_watches({})
            assert agent.WATCH_STATE[d.rstrip("/")]["warn_state"] == "ok"
        finally:
            agent.save_state = old_save
    finally:
        shutil.rmtree(d)
        _reset()


def test_scan_watches_missing_path_reports_error():
    _reset()
    agent.CENTRAL_SETTINGS["watch_paths"] = [{"path": "/definitely/not/mounted/here"}]
    old_save = agent.save_state
    agent.save_state = lambda s: None
    try:
        agent.scan_watches({})
        blob = agent.watch_status_blob()
        entry = blob["/definitely/not/mounted/here"]
        assert "error" in entry and "used" not in entry
        assert agent.watch_extras() == {}          # no fake gauges for a dead path
    finally:
        agent.save_state = old_save
        _reset()


def test_agent_self_metrics_shape():
    _reset()
    first = agent.agent_self_metrics()
    assert first["agent:rss_bytes"] > 1024 * 1024      # a real process is > 1 MB
    assert first["agent:fds"] > 0
    assert "agent:cpu_pct" not in first                # needs a delta — absent on 1st call
    second = agent.agent_self_metrics()
    assert 0.0 <= second["agent:cpu_pct"] <= 100.0
    if "agent:fds:total" in second:                    # soft ulimit, when finite
        assert second["agent:fds:total"] > second["agent:fds"]
    _reset()
    agent._AGENT_CPU["last_total"] = None
    agent._AGENT_CPU["last_mono"] = None


def test_scan_watches_removed_path_forgotten():
    _reset()
    agent.WATCH_STATE["/gone"] = {"warn_state": "warn", "last_walk_mono": 0.0}
    agent.CENTRAL_SETTINGS["watch_paths"] = []
    old_save = agent.save_state
    agent.save_state = lambda s: None
    try:
        agent.scan_watches({})
        assert "/gone" not in agent.WATCH_STATE
    finally:
        agent.save_state = old_save
        _reset()
