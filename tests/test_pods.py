import os
import sys
import json
import time
import pytest
from tests.test_utils import headers, response_format, basic_response_checks, delete_pods

# Allows us to import pods's modules.
sys.path.append('/home/tapis/service')
from api import api

# Set up client for testing
from fastapi.testclient import TestClient

# base_url: The base URL to use for requests, must be valid Tapis URL.
# raise_server_exceptions: If True, the client will raise exceptions from the server rather the normal client errors.
client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)


# Set up test variables
test_pod_1 = "testspodsneo4j"
test_pod_error_1 = "testspodsneo4jerror"


##### Teardown
@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    """Delete all Pod service objects created during testing.

    This fixture is automatically invoked by pytest at the end of the test.
    To note I 
    """
    # yield so the fixture waits until the end of the test to continue
    yield None

    # Delete all objects after the tests are done.
    pods = [test_pod_1]
    volumes = []
    for pod_id in pods:
        rsp = client.delete(f'/pods/{pod_id}', headers=headers)
    for vol_id in volumes:
        rsp = client.delete(f'/pods/volumes/{vol_id}', headers=headers)

##### Testing Pods
def test_get_pods(headers):
    rsp = client.get("/pods", headers=headers)
    result = basic_response_checks(rsp)
    assert result is not None

def test_create_pod(headers):
    # Definition
    pod_def = {
        "pod_id": test_pod_1,
        "pod_template": "template/neo4j",
        "description": "Test Neo4j pod"
    }
    # Create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    # Check the pod object
    assert result['status'] == "REQUESTED"
    assert result['pod_id'] == test_pod_1
    assert result['pod_template'] == "template/neo4j"

def test_check_get_pods(headers):
    rsp = client.get("/pods", headers=headers)
    result = basic_response_checks(rsp)
    found_pod = False
    for pod in result:
        if pod["pod_id"] == test_pod_1:
            found_pod = True
            break
    assert found_pod

def test_pod_startup(headers):
    i = 0
    while i < 20:
        rsp = client.get(f"/pods/{test_pod_1}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        # Pod never became available
        assert False
    # Check the pod object
    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_1
    assert result['pod_template'] == "template/neo4j"

def test_get_pod(headers):
    rsp = client.get(f"/pods/{test_pod_1}", headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    #assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_1
    assert result['pod_template'] == "template/neo4j"

def test_get_pod_logs(headers):
    rsp = client.get(f"/pods/{test_pod_1}/logs",
                     headers=headers)
    result = basic_response_checks(rsp)

    assert result['logs'] or result['logs'] == ''

def test_get_pod_credentials(headers):
    rsp = client.get(f"/pods/{test_pod_1}/credentials", headers=headers)
    result = basic_response_checks(rsp)
    assert result['user_username']
    assert result['user_password']

def test_get_permissions(headers):
    rsp = client.get(f"/pods/{test_pod_1}/permissions", headers=headers)
    result = basic_response_checks(rsp)
    assert result['permissions']

def test_set_permissions(headers):
    # Definition
    perm_def = {
        "user": "testuser",
        "level": "READ"
    }
    # Attempt to create pod
    rsp = client.post(f"/pods/{test_pod_1}/permissions", data=json.dumps(perm_def), headers=headers)
    result = basic_response_checks(rsp)
    assert "testuser:READ" in result['permissions']

def test_delete_set_permissions(headers):
    user = "testuser"
    # Delete user permission from pod
    rsp = client.delete(f"/pods/{test_pod_1}/permissions/{user}", headers=headers)
    result = basic_response_checks(rsp)
    assert "Pod permission deleted successfully" in rsp.json()['message']

def test_update_pod(headers):
    # Definition
    pod_def = {
        "description": "Test Neo4j pod updated"
    }
    # Attempt to create pod
    rsp = client.put(f"/pods/{test_pod_1}", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['description'] == "Test Neo4j pod updated"

def test_update_pod_no_change(headers):
    # Definition
    pod_def = {
        "description": "Test Neo4j pod updated"
    }
    # Attempt to create pod
    rsp = client.put(f"/pods/{test_pod_1}", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert rsp.json()['message'] == "Incoming data made no changes to pod. Is incoming data equal to current data?"

### TODO stop, start, restart pod, update pod

##### Error testing
def test_description_length_400(headers):
    # Definition
    pod_def = {
        "pod_id": test_pod_error_1,
        "pod_template": "template/neo4j",
        "description": "Test" * 200
    }
    # Attempt to create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    data = response_format(rsp)
    # Test error response.
    assert rsp.status_code == 400
    assert any('description field must be less than 255 characters.' in msg for msg in data['message'])


def test_description_is_ascii_400(headers):
    # Definition
    pod_def = {
        "pod_id": test_pod_error_1,
        "pod_template": "template/neo4j",
        "description": "cafè"
    }
    # Attempt to create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    data = response_format(rsp)
    # Test error response.
    assert rsp.status_code == 400
    assert any('description field may only contain ASCII characters' in msg for msg in data['message'])
