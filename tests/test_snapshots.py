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
test_pod_1 = "testssnapshotsneo4j"
test_pod_multi_mount = "testsnapsmultimount"
test_volume_1 = "testsnapshotsvolume"
test_snapshot_1 = "testsnapshotssnapshot"
test_snapshot_error_1 = "testsnapshotssnapshoterror"


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
    pods = [test_pod_1, test_pod_multi_mount]
    volumes = [test_volume_1]
    snapshots = [test_snapshot_1]
    for pod_id in pods:
        rsp = client.delete(f'/pods/{pod_id}', headers=headers)
    for vol_id in volumes:
        rsp = client.delete(f'/pods/volumes/{vol_id}', headers=headers)
    for snap_id in snapshots:
        rsp = client.delete(f'/pods/snapshots/{snap_id}', headers=headers)


### Testing Snapshots
def test_list_snapshots(headers):
    rsp = client.get("/pods/snapshots", headers=headers)
    result = basic_response_checks(rsp)
    assert result is not None


### Must first create volume for snapshot to be created from
def test_create_volume(headers):
    # Definition
    vol_def = {
        "volume_id": test_volume_1,
        "description": "Test volume for snapshot creation"
    }
    # Create volume
    rsp = client.post("/pods/volumes", data=json.dumps(vol_def), headers=headers)
    result = basic_response_checks(rsp)
    # Check the volume
    assert result['volume_id'] == test_volume_1
    # Wait for volume to be available
    time.sleep(2)


def test_check_list_volumes(headers):
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



def test_create_snapshot(headers):
    # Definition
    vol_def = {
        "snapshot_id": test_snapshot_1,
        "description": "Test snapshot",
        "source_volume_path": "/",
        "source_volume_id": test_volume_1,
        "destination_path": "/"
    }
    # Create snapshot
    rsp = client.post("/pods/snapshots", data=json.dumps(vol_def), headers=headers)
    result = basic_response_checks(rsp)
    # Check the snapshot
    assert result['snapshot_id'] == test_snapshot_1
    # Wait for snapshot to be available
    time.sleep(2)


def test_check_list_snapshots(headers):
    rsp = client.get("/pods/snapshots", headers=headers)
    result = basic_response_checks(rsp)
    found_pod = False
    for pod in result:
        if pod["snapshot_id"] == test_snapshot_1:
            found_pod = True
            break
    assert found_pod


def test_snapshot_startup(headers):
    i = 0
    while i < 20:
        rsp = client.get(f"/pods/snapshots/{test_snapshot_1}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        # snapshot never became available
        assert False

    # Check the pod object
    assert result['status'] == "AVAILABLE"
    assert result['snapshot_id'] == test_snapshot_1


def test_get_snapshot(headers):
    rsp = client.get(f"/pods/snapshots/{test_snapshot_1}", headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    #assert result['status'] == "AVAILABLE"
    assert result['snapshot_id'] == test_snapshot_1


def test_get_permissions(headers):
    rsp = client.get(f"/pods/snapshots/{test_snapshot_1}/permissions", headers=headers)
    result = basic_response_checks(rsp)
    assert result['permissions']

def test_set_permissions(headers):
    # Definition
    perm_def = {
        "user": "testuser",
        "level": "READ"
    }
    # Create user permission on pod
    rsp = client.post(f"/pods/snapshots/{test_snapshot_1}/permissions", data=json.dumps(perm_def), headers=headers)
    result = basic_response_checks(rsp)
    assert "testuser:READ" in result['permissions']

def test_delete_set_permissions(headers):
    user = "testuser"
    # Delete user permission from pod
    rsp = client.delete(f"/pods/snapshots/{test_snapshot_1}/permissions/{user}", headers=headers)
    result = basic_response_checks(rsp)
    assert "Snapshot permission deleted successfully" in rsp.json()['message']

def test_list_snapshot_files(headers):
    rsp = client.get(f"/pods/snapshots/{test_snapshot_1}/list", headers=headers)
    result = basic_response_checks(rsp)
    assert isinstance(result, list)

def test_update_snapshot(headers):
    # Definition
    vol_def = {
        "description": "Test snapshot updated"
    }
    # Update snapshot
    rsp = client.put(f"/pods/snapshots/{test_snapshot_1}", data=json.dumps(vol_def), headers=headers)
    result = basic_response_checks(rsp)
    # Check the snapshot
    assert result['snapshot_id'] == test_snapshot_1
    assert result['description'] == "Test snapshot updated"

def test_update_snapshot_no_change(headers):
    # Definition
    vol_def = {
        "description": "Test snapshot updated"
    }
    # Update snapshot
    rsp = client.put(f"/pods/snapshots/{test_snapshot_1}", data=json.dumps(vol_def), headers=headers)
    result = basic_response_checks(rsp)
    assert rsp.json()['message'] == "Incoming data made no changes to snapshot. Is incoming data equal to current data?"


### Pod with Volume Mounted!
def test_create_pod_with_snapshot(headers):
    pod_def = {
        "pod_id": test_pod_1,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test fastapi server pod",
        "networking": {
            "default": {
                "port": 5000,
                "protocol": "http"
            }
        },
        "volume_mounts": {
            "/var/lib/neo4j/import": {
                "type": "tapissnapshot",
                "source_id": test_snapshot_1
            }
        }
    }
    # Attempt to create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    assert result['pod_id'] == test_pod_1
    assert "/var/lib/neo4j/import" in result['volume_mounts']


def test_pod_with_snapshot_startup(headers):
    # Wait for pod to be available
    i = 0
    while i < 20:
        rsp = client.get(f"/pods/{test_pod_1}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(40)
        i += 1
    else:
        # pod never became available
        assert False

    # Check the pod object
    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_1
    assert any(vm.get('source_id') == test_snapshot_1 for vm in result['volume_mounts'].values())


### Pod with Multiple Mounts from Same Snapshot (directory + individual files)
def test_create_pod_with_multiple_snapshot_mounts(headers):
    """Test creating a pod with multiple mounts from the same snapshot:
    - One directory mount
    - Two individual file mounts using sub_path
    This verifies that a single snapshot can be mounted multiple times.
    Note: All snapshot mounts are read-only by default.
    """
    pod_def = {
        "pod_id": test_pod_multi_mount,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with multiple mounts from same snapshot",
        "networking": {
            "default": {
                "port": 5000,
                "protocol": "http"
            }
        },
        "volume_mounts": {
            "/data": {
                "type": "tapissnapshot",
                "source_id": test_snapshot_1
            },
            "/etc/app/config.json": {
                "type": "tapissnapshot",
                "source_id": test_snapshot_1,
                "sub_path": "config.json"
            },
            "/var/reference/data.csv": {
                "type": "tapissnapshot",
                "source_id": test_snapshot_1,
                "sub_path": "exports/data.csv"
            }
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object has all three mounts
    assert result['pod_id'] == test_pod_multi_mount
    assert "/data" in result['volume_mounts']
    assert "/etc/app/config.json" in result['volume_mounts']
    assert "/var/reference/data.csv" in result['volume_mounts']
    
    # Verify all mounts reference the same snapshot
    for mount_path, mount_config in result['volume_mounts'].items():
        assert mount_config['source_id'] == test_snapshot_1
        assert mount_config['type'] == 'tapissnapshot'
        # Snapshots should default to read_only=True
        assert mount_config.get('read_only', True) == True


def test_pod_with_multiple_snapshot_mounts_startup(headers):
    """Wait for multi-mount snapshot pod to become available."""
    i = 0
    while i < 30:
        rsp = client.get(f"/pods/{test_pod_multi_mount}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        # pod never became available
        assert False

    # Check the pod has all mounts and is running
    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_multi_mount
    assert len(result['volume_mounts']) == 3


def test_delete_pod_with_multiple_snapshot_mounts(headers):
    """Delete the multi-mount snapshot pod to clean up."""
    rsp = client.delete(f'/pods/{test_pod_multi_mount}', headers=headers)
    # Just verify no server error
    assert rsp.status_code < 500


##### Error testing
def test_description_length_400(headers):
    # Definition
    snap_def = {
        "snapshot_id": test_snapshot_1,
        "description": "Test" * 200,
        "source_volume_path": "/",
        "source_volume_id": test_volume_1,
        "destination_path": "/"
    }


    # Attempt to create pod
    rsp = client.post("/pods/snapshots", data=json.dumps(snap_def), headers=headers)
    data = response_format(rsp)
    # Test error response.
    assert rsp.status_code == 400
    assert any('description field must be less than 255 characters.' in msg for msg in data['message'])


def test_description_is_ascii_400(headers):
    # Definition
    snap_def = {
        "snapshot_id": test_snapshot_1,
        "description": "cafè",
        "source_volume_path": "/",
        "source_volume_id": test_volume_1,
        "destination_path": "/"
    }

    # Attempt to create pod
    rsp = client.post("/pods/snapshots", data=json.dumps(snap_def), headers=headers)
    data = response_format(rsp)
    # Test error response.
    assert rsp.status_code == 400
    assert any('description field may only contain ASCII characters' in msg for msg in data['message'])
