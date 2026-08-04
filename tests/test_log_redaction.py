"""
Unit tests for service/log_redaction.py (R1 — keep JWTs out of logs).

Run inside the container:
    pytest tests/test_log_redaction.py --disable-pytest-warnings -v

Pure unit tests — no live service, database, or Kubernetes required.
"""
import logging
import sys

sys.path.append('/home/tapis/service')

from log_redaction import scrub_headers, scrub_cookies, JWTRedactionFilter


def test_scrub_headers_masks_credential_headers():
    out = scrub_headers({
        "X-Tapis-Token": "eyJhbGciOiJSUzI1NiJ9.payload.sig",
        "Authorization": "Bearer whatever",
        "Cookie": "X-Tapis-Token=secret",
        "Accept": "application/json",
    })
    assert out["X-Tapis-Token"] == "***"
    assert out["Authorization"] == "***"
    assert out["Cookie"] == "***"
    assert out["Accept"] == "application/json"


def test_scrub_headers_masks_jwt_shaped_values_in_any_header():
    out = scrub_headers({"X-Custom-Header": "eyJ" + "a" * 24})
    assert out["X-Custom-Header"] == "***"


def test_scrub_cookies_masks_every_value_keeps_names():
    out = scrub_cookies({"X-Tapis-Token": "secret", "gate_session": "s3cr3t"})
    assert set(out) == {"X-Tapis-Token", "gate_session"}
    assert set(out.values()) == {"***"}


def test_filter_masks_jwt_shaped_string_in_message():
    jwt = "eyJ" + "A" * 30 + "." + "B" * 24 + "." + "C" * 24
    rec = logging.LogRecord("n", logging.INFO, "p", 1, f"token={jwt} tail", (), None)
    assert JWTRedactionFilter().filter(rec) is True
    assert jwt not in rec.getMessage()
    assert "eyJ***REDACTED***" in rec.getMessage()
    assert rec.getMessage().endswith(" tail")


def test_filter_never_drops_records_and_leaves_normal_messages():
    rec = logging.LogRecord("n", logging.INFO, "p", 1, "hello %s", ("world",), None)
    assert JWTRedactionFilter().filter(rec) is True
    assert rec.getMessage() == "hello world"
