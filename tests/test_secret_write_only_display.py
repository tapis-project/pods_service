"""
Tests for the write-only (readable=False) display gate in resolve_secret_map().

The display-resolve path (GET /pods/{id}/derived?resolve_secrets=true) must honour a
secret's `readable` flag exactly like GET /pods/secrets/{id}/value does: a write-only
secret's value is NOT read from SK — it comes back as WRITE_ONLY_DISPLAY_SENTINEL.

Critically, the POD-START injection path (for_display=False, the default) must be
UNAFFECTED — running pods still receive write-only secret values.

Run in-container:  make test-test_secret_write_only_display.py
"""
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.append('/home/tapis/service')

import secret_utils
from secret_utils import resolve_secret_map, WRITE_ONLY_DISPLAY_SENTINEL


def _secret(readable):
    # Stand-in for a stored Secret row. Owner matches the explicit reference below.
    return SimpleNamespace(
        readable=readable,
        added_by="jsmith",
        sk_secret_name="sk-name",
        secret_id="jsmith:mysecret",
    )


def _run(readable, for_display):
    """Resolve one explicit user-secret reference with SK + DB mocked; returns
    (resolved, errors, readSecret_mock)."""
    secret_map = {"DB_PASSWORD": "${secret:jsmith:mysecret}"}
    tmock = MagicMock()
    tmock.sk.readSecret.return_value = SimpleNamespace(
        secretMap={"secret_value": "REALVALUE"}
    )
    with patch.object(secret_utils.Secret, "db_get_with_pk", return_value=_secret(readable)), \
         patch.object(secret_utils, "log_secret_event"), \
         patch.object(secret_utils, "t", tmock):
        resolved, errors = resolve_secret_map(
            secret_map=secret_map,
            site_id="tacc",
            tenant_id="dev",
            actor="jsmith",
            pod_id="p1",
            pod=None,  # skips random/networking/stack steps → straight to SK branch
            for_display=for_display,
        )
    return resolved, errors, tmock.sk.readSecret


class TestWriteOnlyDisplayGate:
    def test_display_hides_write_only_secret(self):
        # readable=False + display → sentinel, and SK is NEVER read.
        resolved, errors, read_mock = _run(readable=False, for_display=True)
        assert resolved["DB_PASSWORD"] == WRITE_ONLY_DISPLAY_SENTINEL
        read_mock.assert_not_called()

    def test_display_reveals_readable_secret(self):
        # readable=True + display → real value read from SK.
        resolved, errors, read_mock = _run(readable=True, for_display=True)
        assert resolved["DB_PASSWORD"] == "REALVALUE"
        read_mock.assert_called_once()

    def test_injection_path_reads_write_only_secret(self):
        # for_display=False (default; the pod-START injection path) must STILL read
        # write-only secrets — otherwise running pods lose their credentials.
        resolved, errors, read_mock = _run(readable=False, for_display=False)
        assert resolved["DB_PASSWORD"] == "REALVALUE"
        read_mock.assert_called_once()

    def test_default_is_injection_behavior(self):
        # Omitting for_display entirely == injection behavior (safe default).
        secret_map = {"DB_PASSWORD": "${secret:jsmith:mysecret}"}
        tmock = MagicMock()
        tmock.sk.readSecret.return_value = SimpleNamespace(
            secretMap={"secret_value": "REALVALUE"}
        )
        with patch.object(secret_utils.Secret, "db_get_with_pk", return_value=_secret(False)), \
             patch.object(secret_utils, "log_secret_event"), \
             patch.object(secret_utils, "t", tmock):
            resolved, _ = resolve_secret_map(
                secret_map=secret_map, site_id="tacc", tenant_id="dev",
                actor="jsmith", pod_id="p1", pod=None,  # no for_display kwarg
            )
        assert resolved["DB_PASSWORD"] == "REALVALUE"
