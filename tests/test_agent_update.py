"""
Unit tests for agent restart + self-update — argv construction, apply_update
verification chain (sha / utf-8 / compile / atomic install / prev-copy keep),
and the startup handoff loader incl. the crash-loop guard. No network; exec is
stubbed. Skips in-container like the other agent test files.
"""

import hashlib
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

GOOD_SRC = 'AGENT_VERSION = "9.9.9"\nprint("hi")\n'


class TmpState:
    """PODS_AGENT_STATE -> fresh tmpdir for the duration of a test."""
    def __enter__(self):
        self.d = tempfile.mkdtemp()
        self.old = os.environ.get("PODS_AGENT_STATE")
        os.environ["PODS_AGENT_STATE"] = self.d
        return self.d

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop("PODS_AGENT_STATE", None)
        else:
            os.environ["PODS_AGENT_STATE"] = self.old
        shutil.rmtree(self.d, ignore_errors=True)


def test_build_reexec_argv_same_and_new_script():
    old_argv = sys.argv[:]
    sys.argv = ["/opt/pods_agent.py", "run"]
    try:
        same = agent.build_reexec_argv()
        assert same[0] == sys.executable
        assert same[1] == os.path.abspath("/opt/pods_agent.py") and same[2] == "run"
        new = agent.build_reexec_argv("/var/lib/pods-agent/pods_agent_updated.py")
        assert new[1] == "/var/lib/pods-agent/pods_agent_updated.py" and new[2] == "run"
    finally:
        sys.argv = old_argv


def test_apply_update_happy_path_and_prev_copy():
    with TmpState():
        raw = GOOD_SRC.encode()
        sha = hashlib.sha256(raw).hexdigest()
        state = {}
        path, version = agent.apply_update(raw, sha, state)
        assert version == "9.9.9"
        assert open(path).read() == GOOD_SRC
        assert state["agent_update"]["sha256"] == sha
        assert state["agent_update"]["version"] == "9.9.9"
        # a second install keeps the previous copy as fallback
        raw2 = GOOD_SRC.replace("9.9.9", "9.9.10").encode()
        agent.apply_update(raw2, hashlib.sha256(raw2).hexdigest(), state)
        assert open(path + ".prev").read() == GOOD_SRC


def test_apply_update_refusals():
    with TmpState():
        raw = GOOD_SRC.encode()
        sha = hashlib.sha256(raw).hexdigest()
        for bad_raw, bad_sha, expect in [
            (raw, "", "unverified"),                                   # no sha at all
            (raw, "0" * 64, "mismatch"),                               # wrong sha
            (b"\xff\xfe", hashlib.sha256(b"\xff\xfe").hexdigest(), "utf-8"),
            (b"def broken(:", hashlib.sha256(b"def broken(:").hexdigest(), "compile"),
        ]:
            try:
                agent.apply_update(bad_raw, bad_sha, {})
                assert False, f"expected refusal for {expect}"
            except RuntimeError as e:
                assert expect in str(e)
        assert not os.path.exists(agent.updated_copy_path())           # nothing written


def test_loader_hands_off_to_verified_copy():
    with TmpState():
        raw = GOOD_SRC.encode()
        sha = hashlib.sha256(raw).hexdigest()
        state = {}
        path, _ = agent.apply_update(raw, sha, state)
        agent.save_state(state)
        calls = []
        old_reexec = agent._reexec
        agent._reexec = lambda p=None: calls.append(p)
        try:
            agent.maybe_exec_updated_copy()
        finally:
            agent._reexec = old_reexec
        assert calls == [path]
        st = agent.load_state()
        assert st["agent_update_attempts"]["count"] == 1               # attempt recorded pre-exec


def test_loader_rejects_tampered_copy():
    with TmpState():
        raw = GOOD_SRC.encode()
        state = {}
        path, _ = agent.apply_update(raw, hashlib.sha256(raw).hexdigest(), state)
        agent.save_state(state)
        with open(path, "a") as f:
            f.write("# tampered\n")
        calls = []
        old_reexec = agent._reexec
        agent._reexec = lambda p=None: calls.append(p)
        try:
            agent.maybe_exec_updated_copy()
        finally:
            agent._reexec = old_reexec
        assert calls == []                                             # no handoff
        assert "agent_update" not in (agent.load_state() or {})        # record cleared


def test_loader_crash_loop_guard_abandons():
    with TmpState():
        import time as _time
        raw = GOOD_SRC.encode()
        state = {}
        agent.apply_update(raw, hashlib.sha256(raw).hexdigest(), state)
        state["agent_update_attempts"] = {"count": 3, "first_ts": _time.time()}
        agent.save_state(state)
        calls = []
        old_reexec = agent._reexec
        agent._reexec = lambda p=None: calls.append(p)
        try:
            agent.maybe_exec_updated_copy()
        finally:
            agent._reexec = old_reexec
        assert calls == []                                             # abandoned, no exec
        st = agent.load_state()
        assert "agent_update" not in st and st["agent_update_attempts"] == {}


def test_loader_noop_without_record():
    with TmpState():
        calls = []
        old_reexec = agent._reexec
        agent._reexec = lambda p=None: calls.append(p)
        try:
            agent.maybe_exec_updated_copy()
        finally:
            agent._reexec = old_reexec
        assert calls == []


def test_allow_self_update_env_is_strict_opt_in():
    for val, expect in [("true", True), ("TRUE", True), ("1", False),
                        ("yes", False), ("false", False)]:
        os.environ["PODS_AGENT_ALLOW_SELF_UPDATE"] = val
        try:
            assert agent.setting("allow_self_update") is expect, (val, expect)
        finally:
            os.environ.pop("PODS_AGENT_ALLOW_SELF_UPDATE", None)
    agent.CENTRAL_SETTINGS["allow_self_update"] = True
    try:
        assert agent.setting("allow_self_update") is True              # central-settable
    finally:
        agent.CENTRAL_SETTINGS.clear()


def test_update_posture_pending_not_spooky_on_first_beat():
    saved = dict(agent.MILESTONES)
    try:
        agent.MILESTONES.pop("first_checkin_ok", None)
        p = agent.update_posture({})                    # fresh state, no checkin yet
        assert p.get("pending") is True and "blocker" not in p
        agent.MILESTONES["first_checkin_ok"] = 1.0
        p = agent.update_posture({})                    # now the absence is a fact
        assert "blocker" in p and "pending" not in p
        p = agent.update_posture({"agent_source_version": "9.9.9"})
        assert p["available"] is True and "blocker" not in p and "pending" not in p
        p = agent.update_posture({"agent_source_version": agent.AGENT_VERSION})
        assert p["available"] is False
    finally:
        agent.MILESTONES.clear()
        agent.MILESTONES.update(saved)


def test_agent_source_url_derives_when_not_adopted():
    st = {"api_base": "http://pods-api:8000/pods", "node_id": "mickey"}
    assert agent.agent_source_url(st) == "http://pods-api:8000/pods/nodes/mickey/agent-source"
    st["agent_source"] = "https://tacc.tapis.io/v3/pods/nodes/mickey/agent-source"
    assert agent.agent_source_url(st) == st["agent_source"]


def test_adopt_central_settings_shared_helper():
    with TmpState():
        agent.CENTRAL_SETTINGS.clear()
        state = {}
        agent.adopt_central_settings(state, {"logs_tail": 321})
        assert agent.CENTRAL_SETTINGS == {"logs_tail": 321}
        assert state["central_settings"] == {"logs_tail": 321}
        assert (agent.load_state() or {}).get("central_settings") == {"logs_tail": 321}
        # unchanged overlay = no-op (no state churn)
        agent.adopt_central_settings(state, {"logs_tail": 321})
        # None = nothing advertised = no-op
        agent.adopt_central_settings(state, None)
        assert agent.CENTRAL_SETTINGS == {"logs_tail": 321}
        agent.CENTRAL_SETTINGS.clear()
