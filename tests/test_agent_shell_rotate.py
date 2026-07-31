"""
Unit tests for the agent's shell executor and no-downtime token rotation —
env-only gating, real command execution/timeout/output caps, and the
persist-then-confirm-with-the-new-token rotation handshake (incl. rollback when
the confirmation fails). No network; post_command_result is stubbed.
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


class TmpState:
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


class Captured:
    """Stub post_command_result, recording (headers, cid, status, result).

    Returns True — the real function returns a BOOLEAN (True on a successful
    post, False on any failure) and callers branch on it. A stub that returned
    None would read as a failed post and send rotate down its rollback path.
    """
    def __enter__(self):
        self.calls = []
        self.old = agent.post_command_result

        def fake(state, headers, cid, status, result):
            self.calls.append((dict(headers), cid, status, result))
            return True
        agent.post_command_result = fake
        return self.calls

    def __exit__(self, *a):
        agent.post_command_result = self.old


def test_allow_shell_is_env_only():
    agent.CENTRAL_SETTINGS.clear()
    os.environ.pop("PODS_AGENT_ALLOW_SHELL", None)
    assert agent.setting("allow_shell") is False
    # central CANNOT enable it — the overlay is ignored entirely for this key
    agent.CENTRAL_SETTINGS["allow_shell"] = True
    assert agent.setting("allow_shell") is False
    agent.CENTRAL_SETTINGS.clear()
    for val, expect in [("true", True), ("TRUE", True), ("1", False), ("yes", False)]:
        os.environ["PODS_AGENT_ALLOW_SHELL"] = val
        assert agent.setting("allow_shell") is expect, val
    os.environ.pop("PODS_AGENT_ALLOW_SHELL", None)


def test_shell_refused_when_disabled():
    os.environ.pop("PODS_AGENT_ALLOW_SHELL", None)
    with Captured() as calls:
        agent.do_shell_command({}, {}, "nc_1", {"command": "echo nope"})
    assert len(calls) == 1
    _, cid, status, result = calls[0]
    assert cid == "nc_1" and status == "error"
    assert "PODS_AGENT_ALLOW_SHELL" in result["error"]


def test_shell_runs_and_reports_exit_and_output():
    os.environ["PODS_AGENT_ALLOW_SHELL"] = "true"
    try:
        with Captured() as calls:
            agent.do_shell_command({}, {}, "nc_2",
                                   {"command": "echo hello; echo boo 1>&2; exit 3"})
        _, _, status, result = calls[0]
        assert status == "done"                        # ran fine; exit code is data
        assert result["exit_code"] == 3
        assert "hello" in result["stdout"] and "boo" in result["stderr"]
        assert result["timed_out"] is False and result["duration_ms"] >= 0
    finally:
        os.environ.pop("PODS_AGENT_ALLOW_SHELL", None)


def test_shell_timeout_is_reported_as_error():
    os.environ["PODS_AGENT_ALLOW_SHELL"] = "true"
    try:
        with Captured() as calls:
            agent.do_shell_command({}, {}, "nc_3", {"command": "sleep 5", "timeout": 1})
        _, _, status, result = calls[0]
        assert status == "error" and result["timed_out"] is True
        assert "timed out" in result["error"]
    finally:
        os.environ.pop("PODS_AGENT_ALLOW_SHELL", None)


def test_shell_output_is_capped():
    os.environ["PODS_AGENT_ALLOW_SHELL"] = "true"
    try:
        with Captured() as calls:
            agent.do_shell_command(
                {}, {}, "nc_4",
                {"command": f"python3 -c \"print('x' * {agent.SHELL_OUTPUT_MAX_CHARS + 5000})\""})
        _, _, _, result = calls[0]
        assert len(result["stdout"]) <= agent.SHELL_OUTPUT_MAX_CHARS + 100
        assert "truncated" in result["stdout"]
    finally:
        os.environ.pop("PODS_AGENT_ALLOW_SHELL", None)


def test_rotate_persists_then_confirms_with_new_token():
    with TmpState():
        state = {"node_token": "pna_old", "tenant": "dev", "node_id": "n1"}
        agent.save_state(state)
        headers = {agent.TOKEN_HEADER: "pna_old"}
        with Captured() as calls:
            agent.do_rotate_command(state, headers, "nc_5", {"node_token": "pna_new"})
        sent_headers, cid, status, _ = calls[0]
        assert cid == "nc_5" and status == "done"
        # the confirmation MUST carry the new token — that is the proof central needs
        assert sent_headers[agent.TOKEN_HEADER] == "pna_new"
        assert agent.load_state()["node_token"] == "pna_new"   # persisted before confirming
        assert headers[agent.TOKEN_HEADER] == "pna_new"        # running loop switched over


def test_rotate_rolls_back_when_confirmation_fails():
    """Rollback must fire on the REAL failure contract.

    post_command_result catches its own exceptions and returns False — it never
    raises. An earlier version of this test stubbed it with a function that
    raised, so it passed against a rollback path that could never execute: any
    real confirmation failure left the agent on a token central never promoted,
    403ing forever until someone re-joined the box by hand. The stub below
    returns False, which is what the real function actually does.
    """
    with TmpState():
        state = {"node_token": "pna_old", "tenant": "dev", "node_id": "n1"}
        agent.save_state(state)
        headers = {agent.TOKEN_HEADER: "pna_old"}
        old_post = agent.post_command_result

        def failed(*a, **kw):
            return False  # exactly what post_command_result returns on failure
        agent.post_command_result = failed
        try:
            agent.do_rotate_command(state, headers, "nc_6", {"node_token": "pna_new"})
        finally:
            agent.post_command_result = old_post
        # never confirmed -> central never promotes -> keep using the OLD token
        assert agent.load_state()["node_token"] == "pna_old"
        assert headers[agent.TOKEN_HEADER] == "pna_old"


def test_post_command_result_returns_false_and_never_raises():
    """Pins the contract the rollback depends on, so it can't silently change."""
    old_http = agent.http_json

    def boom(*a, **kw):
        raise RuntimeError("central unreachable")
    agent.http_json = boom
    try:
        # must NOT propagate — returns False instead
        assert agent.post_command_result(
            {"api_base": "http://x/pods", "node_id": "n1"}, {}, "nc_x", "done", {}) is False
    finally:
        agent.http_json = old_http


def test_rotate_without_token_errors():
    with Captured() as calls:
        agent.do_rotate_command({}, {}, "nc_7", {})
    _, _, status, result = calls[0]
    assert status == "error" and "no node_token" in result["error"]
