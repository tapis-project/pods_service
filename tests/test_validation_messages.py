"""
Pin the two body-validation 400 messages from service/utils.py error_handler:
- a body that fastapi never parsed (missing/mislabeled Content-Type) points at the header
- parsed JSON whose top level isn't an object says so plainly

Run inside the container:
    pytest tests/test_validation_messages.py --disable-pytest-warnings -v
"""
import json
import sys
from tests.test_utils import headers, response_format

sys.path.append('/home/tapis/service')
from api import api

from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)


def test_body_without_content_type_names_the_header(headers):
    h = {k: v for k, v in headers.items() if k.lower() != 'content-type'}
    rsp = client.post("/pods", data=json.dumps({"pod_id": "neverexists"}), headers=h)
    assert rsp.status_code == 400
    msg = response_format(rsp)["message"]
    assert any("Content-Type: application/json" in m for m in msg)


def test_non_object_json_body_says_expected_object(headers):
    rsp = client.post("/pods", json=[1, 2, 3], headers=headers)
    assert rsp.status_code == 400
    msg = response_format(rsp)["message"]
    assert any("expected a JSON object" in m for m in msg)


def test_object_body_with_bad_fields_keeps_field_messages(headers):
    # The special-casing must not swallow normal per-field validation output.
    rsp = client.post("/pods", json={"bogus_field": 1}, headers=headers)
    assert rsp.status_code == 400
    msg = response_format(rsp)["message"]
    assert any("pod_id" in m and "required" in m.lower() for m in msg)
