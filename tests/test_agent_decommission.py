"""
Unit tests for the agent decommission machinery — plan detection per edge type,
state wipe + marker semantics, and the inert-marker check. No network, no
docker; environment detection is stubbed. Skips in-container like the other
agent test files.
"""

import json
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


def with_exists(mapping):
    """Patch os.path.exists so /.dockerenv and the docker socket answer per
    `mapping`; everything else falls through to the real filesystem."""
    real = os.path.exists

    def fake(p):
        if p in mapping:
            return mapping[p]
        return real(p)

    class _P:
        def __enter__(self):
            agent.os.path.exists = fake

        def __exit__(self, *a):
            agent.os.path.exists = real

    return _P()


def test_decommission_plan_per_edge_type():
    with with_exists({"/.dockerenv": False}):
        outcome, desc = agent.decommission_plan()
        assert outcome == "bare-exit" and "removal complete" in desc
    with with_exists({"/.dockerenv": True, agent.DOCKER_SOCK: True}):
        outcome, desc = agent.decommission_plan()
        assert outcome == "container-self-remove" and "removal complete" in desc
    with with_exists({"/.dockerenv": True, agent.DOCKER_SOCK: False}):
        outcome, desc = agent.decommission_plan()
        assert outcome == "container-inert" and "docker rm -f" in desc


def test_wipe_local_state_removes_everything_but_marker():
    with TmpState() as d:
        agent.save_state({"node_token": "pna_secret", "node_id": "x"})
        with open(os.path.join(d, "pods_agent_updated.py"), "w") as f:
            f.write("AGENT_VERSION = '9'\n")
        removed = agent.wipe_local_state()
        assert "state.json" in removed and "pods_agent_updated.py" in removed
        assert agent.load_state() is None                       # token gone
        assert agent.check_decommissioned() is True             # marker present
        marker = open(agent.decommission_marker_path()).read()
        assert "decommissioned at" in marker and "fresh join" in marker
        # only the marker remains
        assert os.listdir(d) == ["DECOMMISSIONED"]


def test_wipe_without_marker_leaves_no_trace():
    with TmpState() as d:
        agent.save_state({"node_token": "pna_secret"})
        agent.wipe_local_state(write_marker=False)
        assert agent.check_decommissioned() is False
        assert os.listdir(d) == []


def test_marker_gates_check_decommissioned():
    with TmpState():
        assert agent.check_decommissioned() is False
        agent.wipe_local_state()
        assert agent.check_decommissioned() is True
        os.remove(agent.decommission_marker_path())             # the documented reuse path
        assert agent.check_decommissioned() is False


def test_do_decommission_confirms_before_wiping_and_exits():
    with TmpState() as d:
        agent.save_state({"node_token": "pna_secret", "node_id": "x"})
        posted = []
        old_post = agent.post_command_result
        agent.post_command_result = lambda st, h, cid, status, result: posted.append(
            (cid, status, result))
        try:
            with with_exists({"/.dockerenv": False}):
                try:
                    agent.do_decommission_command({"node_id": "x"}, {}, "nc_123")
                    assert False, "expected SystemExit"
                except SystemExit as e:
                    assert e.code == 0
        finally:
            agent.post_command_result = old_post
        assert len(posted) == 1
        cid, status, result = posted[0]
        assert cid == "nc_123" and status == "done"
        assert result["outcome"] == "bare-exit" and "removal complete" in result["detail"]
        assert agent.load_state() is None                       # wiped AFTER the ack
        assert agent.check_decommissioned() is True
