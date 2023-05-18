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
test_pod_1 = "testbaseneo4j"
error_pod_1 = "testbaseneo4jerror"
test_volume_1 = "testbasevolume"
test_snapshot_1 = "testbasesnapshot"


##### Teardown
@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    """Delete all Pod service objects created during testing.

    This fixture is automatically invoked by pytest at the end of the test.
    To note I think this is the best way to do garbage collection.
    Normally users are advised to create an object function pytest fixture with a "yield" in it.
    This yield would wait for the tests to complete and then run deletion code. Keeping it to 
    one function greatly simplifies the code in terms of variables and knowing where things are deleted.
    """
    # yield so the fixture waits until the end of the test to continue
    yield None

    # Delete all objects after the tests are done.
    pods = [test_pod_1]
    volumes = [test_volume_1]
    for pod_id in pods:
        rsp = client.delete(f'/pods/{pod_id}', headers=headers)
    for vol_id in volumes:
        rsp = client.delete(f'/pods/volumes/{vol_id}', headers=headers)


### Testing Pods
def test_get_pods(headers):
    rsp = client.get("/pods", headers=headers)
    result = basic_response_checks(rsp)
    assert result is not None

def test_create_neo4j_pod(headers):
    # Definition
    pod_def = {"pod_id": test_pod_1,
               "pod_template": "template/neo4j",
               "description": "Test Neo4j pod"}
    # Create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod
    assert result['status'] == "REQUESTED"
    assert result['pod_id'] == test_pod_1
    assert result['pod_template'] == "template/neo4j"

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


### Testing Volumes
def test_get_volumes(headers):
    rsp = client.get("/pods/volumes", headers=headers)
    result = basic_response_checks(rsp)
    assert result != None

def test_create_volume(headers):
    # Definition
    vol_def = {"volume_id": test_volume_1,
               "description": "Test volume"}
    # Create volume
    rsp = client.post("/pods/volumes", data=json.dumps(vol_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the volume
    assert result['volume_id'] == test_volume_1


def test_volume_startup(headers):
    i = 0
    while i < 10:
        rsp = client.get(f"/pods/volumes/{test_volume_1}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        # volume never became available
        assert False

    # Check the pod object
    assert result['status'] == "AVAILABLE"
    assert result['volume_id'] == test_volume_1
