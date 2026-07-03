"""Test fixtures for the Pods MCP server.

Offline tests build the server from the committed spec dump
(../docs/openapi_v3-pods.yml) with a dummy client — no network. Live tests
(test_live_integration.py) activate only when TAPIS_BASE_URL + TAPIS_TOKEN point
at a real tenant.
"""
import asyncio
import os
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
MCP_DIR = HERE.parent
SPEC = MCP_DIR.parent / "docs" / "openapi_v3-pods.yml"

sys.path.insert(0, str(MCP_DIR))

# Defaults so pods_mcp imports offline. setdefault => a real live run (env set by
# the caller) is untouched; PODS_SPEC stays the committed dump either way since the
# public gateway does not serve /openapi.json.
os.environ.setdefault("TAPIS_BASE_URL", "http://localhost:0")
os.environ.setdefault("PODS_SPEC", str(SPEC))


@pytest.fixture(scope="session")
def full_mcp():
    if not SPEC.exists():
        pytest.skip(f"committed spec not found at {SPEC}")
    import pods_mcp  # import-time build reads the spec file offline
    return pods_mcp.mcp


@pytest.fixture(scope="session")
def tool_names(full_mcp):
    return sorted(t.name for t in asyncio.run(full_mcp.list_tools()))


@pytest.fixture(scope="session")
def loop():
    """One event loop for the whole session. The MCP's module-level AsyncClient
    binds to the first loop that drives it, so live tests must share one loop
    (else httpx teardown hits a closed loop)."""
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


@pytest.fixture
def run(loop):
    return lambda coro: loop.run_until_complete(coro)
