"""
Unit tests for agent address sharing — default-off gate, env/central precedence,
and the shape of the addresses block in status. No network assumptions beyond a
possibly-routeless test box (lan_ip is best-effort); tailscale is absent in CI,
so tailnet keys are exercised only via the negative path. Skips in-container
like the other agent test files.
"""

import os
import sys

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
    agent.CENTRAL_SETTINGS.clear()
    os.environ.pop("PODS_AGENT_SHARE_ADDRESSES", None)


def test_share_addresses_defaults_off():
    _reset()
    assert agent.share_addresses() is False
    status = agent.sample_status(["runtime.none"])
    assert "addresses" not in status


def test_share_addresses_env_enables():
    _reset()
    os.environ["PODS_AGENT_SHARE_ADDRESSES"] = "true"
    try:
        assert agent.share_addresses() is True
        status = agent.sample_status(["runtime.none"])
        # present when sharing — possibly empty on a routeless box, always a dict
        assert isinstance(status.get("addresses"), dict)
    finally:
        _reset()


def test_share_addresses_central_setting_and_env_pin():
    _reset()
    agent.CENTRAL_SETTINGS["share_addresses"] = True
    assert agent.share_addresses() is True
    # env pin on the box wins over central, both directions
    os.environ["PODS_AGENT_SHARE_ADDRESSES"] = "false"
    try:
        assert agent.share_addresses() is False
    finally:
        _reset()


def test_node_addresses_shape():
    _reset()
    addrs = agent.node_addresses()
    assert isinstance(addrs, dict)
    allowed = {"tailnet_ip4", "tailnet_ip6", "tailnet_name", "lan_ip"}
    assert set(addrs).issubset(allowed)
    for v in addrs.values():
        assert isinstance(v, str) and v


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
