import os
import sys
import json
import time
import pytest
from tests.test_utils import headers, response_format, basic_response_checks, delete_pods, t

# Allows us to import pods's modules.
sys.path.append('/home/tapis/service')
from api import api

# Set up client for testing
from fastapi.testclient import TestClient

# base_url: The base URL to use for requests, must be valid Tapis URL.
# raise_server_exceptions: If True, the client will raise exceptions from the server rather the normal client errors.
client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)


# Set up test variables
test_pod_1 = "testsvolumesneo4j"
test_volume_1 = "testvolumesvolume"
test_snapshot_1 = "testvolumessnapshot"
test_snapshot_error_1 = "testvolumessnapshoterror"


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
    volumes = [test_volume_1]
    for pod_id in pods:
        rsp = client.delete(f'/pods/{pod_id}', headers=headers)
    for vol_id in volumes:
        rsp = client.delete(f'/pods/volumes/{vol_id}', headers=headers)


### Testing Volumes
def test_get_volumes(headers):
    rsp = client.get("/pods/volumes", headers=headers)
    result = basic_response_checks(rsp)
    assert result is not None

def test_create_volume(headers):
    # Definition
    vol_def = {
        "volume_id": test_volume_1,
        "description": "Test volume"
    }
    # Create volume
    rsp = client.post("/pods/volumes", data=json.dumps(vol_def), headers=headers)
    result = basic_response_checks(rsp)
    # Check the volume
    assert result['volume_id'] == test_volume_1
    # Wait for volume to be available
    time.sleep(2)


def test_check_get_volumes(headers):
    rsp = client.get("/pods/volumes", headers=headers)
    result = basic_response_checks(rsp)
    found_pod = False
    for pod in result:
        if pod["volume_id"] == test_volume_1:
            found_pod = True
            break
    assert found_pod


def test_volume_startup(headers):
    i = 0
    while i < 20:
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


def test_get_volume(headers):
    rsp = client.get(f"/pods/volumes/{test_volume_1}", headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    #assert result['status'] == "AVAILABLE"
    assert result['volume_id'] == test_volume_1


def test_get_permissions(headers):
    rsp = client.get(f"/pods/volumes/{test_volume_1}/permissions", headers=headers)
    result = basic_response_checks(rsp)
    assert result['permissions']

def test_set_permissions(headers):
    # Definition
    perm_def = {
        "user": "testuser",
        "level": "READ"
    }
    # Create user permission on pod
    rsp = client.post(f"/pods/volumes/{test_volume_1}/permissions", data=json.dumps(perm_def), headers=headers)
    result = basic_response_checks(rsp)
    assert "testuser:READ" in result['permissions']

def test_delete_set_permissions(headers):
    user = "testuser"
    # Delete user permission from pod
    rsp = client.delete(f"/pods/volumes/{test_volume_1}/permissions/{user}", headers=headers)
    result = basic_response_checks(rsp)
    assert "Volume permission deleted successfully" in rsp.json()['message']

def test_list_volume_files(headers):
    rsp = client.get(f"/pods/volumes/{test_volume_1}/list", headers=headers)
    result = basic_response_checks(rsp)
    assert isinstance(result, list)

def test_upload_to_volume(headers):
    # Upload file to volume
    # with open('config.json', 'rb') as data_blob:
    #     rsp = t.pods.upload_to_volume(volume_id = test_volume_1,
    #                                   path = '/config.json',
    #                                   file = data_blob,
    #                                   _x_tapis_tenant='dev',
    #                                   _x_tapis_user='_pods_testuser_admin')
    rsp = client.post(f"/pods/volumes/{test_volume_1}/upload/config.json", files={"file": open('config.json', 'rb')}, headers=headers)
    result = basic_response_checks(rsp)

def test_update_volume(headers):
    # Definition
    vol_def = {
        "description": "Test volume updated"
    }
    # Update volume
    rsp = client.put(f"/pods/volumes/{test_volume_1}", data=json.dumps(vol_def), headers=headers)
    result = basic_response_checks(rsp)
    # Check the volume
    assert result['volume_id'] == test_volume_1
    assert result['description'] == "Test volume updated"

def test_update_volume_no_change(headers):
    # Definition
    vol_def = {
        "description": "Test volume updated"
    }
    # Update volume
    rsp = client.put(f"/pods/volumes/{test_volume_1}", data=json.dumps(vol_def), headers=headers)
    result = basic_response_checks(rsp)
    assert rsp.json()['message'] == "Incoming data made no changes to volume. Is incoming data equal to current data?"


### Pod with Volume Mounted!
def test_create_pod_with_volume(headers):
    # Definition
    pod_def = {
        "pod_id": test_pod_1,
        "pod_template": "template/neo4j",
        "description": "Test neo4j pod with mounted volume",
        "volume_mounts": {
            test_volume_1: {
                "type": "tapisvolume",
                "mount_path": "/var/lib/neo4j/import"
            }
        }
    }
    # Attempt to create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    assert result['pod_id'] == test_pod_1
    assert test_volume_1 in result['volume_mounts']


def test_pod_with_volume_startup(headers):
    # Wait for pod to be available
    i = 0
    while i < 10:
        rsp = client.get(f"/pods/{test_pod_1}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        # pod never became available
        assert False

    # Check the pod object
    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_1
    assert test_volume_1 in result['volume_mounts']


##### Error testing
def test_description_length_400(headers):
    # Definition
    vol_def = {
        "volume_id": test_snapshot_error_1,
        "description": "Test" * 200
    }
    # Attempt to create pod
    rsp = client.post("/pods/volumes", data=json.dumps(vol_def), headers=headers)
    data = response_format(rsp)
    # Test error response.
    assert rsp.status_code == 400
    assert any('description field must be less than 255 characters.' in msg for msg in data['message'])


def test_description_is_ascii_400(headers):
    # Definition
    vol_def = {
        "volume_id": test_snapshot_error_1,
        "description": "cafè"
    }
    # Attempt to create pod
    rsp = client.post("/pods/volumes", data=json.dumps(vol_def), headers=headers)
    data = response_format(rsp)
    # Test error response.
    assert rsp.status_code == 400
    assert any('description field may only contain ASCII characters' in msg for msg in data['message'])
