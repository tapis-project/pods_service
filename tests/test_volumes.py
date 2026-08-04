import os
import sys
import json
import time
import pytest
from tests.test_utils import headers, response_format, basic_response_checks, delete_pods, multipart_headers, t

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
test_pod_multi_mount = "testvolumesmultimount"
test_pod_pvc = "testvolumespvc"
test_pod_multi_pvc = "testvolumesmultipvc"
test_pod_two_pvc = "testvolumestwopvc"
test_pod_mixed = "testvolumesmixed"
test_volume_1 = "testvolumesvolume"
test_volume_2 = "testvolumesvolume2"
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
    pods = [test_pod_1, test_pod_multi_mount, test_pod_pvc, test_pod_multi_pvc, test_pod_two_pvc, test_pod_mixed]
    volumes = [test_volume_1, test_volume_2]
    for pod_id in pods:
        rsp = client.delete(f'/pods/{pod_id}', headers=headers)
    for vol_id in volumes:
        rsp = client.delete(f'/pods/volumes/{vol_id}', headers=headers)


### Testing Volumes
def test_list_volumes(headers):
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
    rsp = client.post(f"/pods/volumes/{test_volume_1}/upload/config.json", files={"file": open('config.json', 'rb')}, headers=multipart_headers(headers))
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
    time.sleep(2)
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
                "type": "tapisvolume",
                "source_id": test_volume_1
            }
        }
    }
    # Attempt to create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    assert result['pod_id'] == test_pod_1
    assert "/var/lib/neo4j/import" in result['volume_mounts']


def test_pod_with_volume_startup(headers):
    # Wait for pod to be available
    i = 0
    while i < 30:
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
    assert any(vm.get('source_id') == test_volume_1 for vm in result['volume_mounts'].values())


### Pod with Multiple Mounts from Same Volume (directory + individual files)
def test_create_pod_with_multiple_volume_mounts(headers):
    """Test creating a pod with multiple mounts from the same volume:
    - One directory mount
    - Two individual file mounts using sub_path
    This verifies that a single volume can be mounted multiple times.
    """
    pod_def = {
        "pod_id": test_pod_multi_mount,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with multiple mounts from same volume",
        "networking": {
            "default": {
                "port": 5000,
                "protocol": "http"
            }
        },
        "volume_mounts": {
            "/data": {
                "type": "tapisvolume",
                "source_id": test_volume_1
            },
            "/etc/app/config.json": {
                "type": "tapisvolume",
                "source_id": test_volume_1,
                "sub_path": "config.json"
            },
            "/var/log/app.log": {
                "type": "tapisvolume",
                "source_id": test_volume_1,
                "sub_path": "logs/app.log"
            }
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object has all three mounts
    assert result['pod_id'] == test_pod_multi_mount
    assert "/data" in result['volume_mounts']
    assert "/etc/app/config.json" in result['volume_mounts']
    assert "/var/log/app.log" in result['volume_mounts']
    
    # Verify all mounts reference the same volume
    for mount_path, mount_config in result['volume_mounts'].items():
        assert mount_config['source_id'] == test_volume_1
        assert mount_config['type'] == 'tapisvolume'


def test_pod_with_multiple_volume_mounts_startup(headers):
    """Wait for multi-mount pod to become available."""
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


def test_delete_pod_with_multiple_volume_mounts(headers):
    """Delete the multi-mount pod to clean up."""
    rsp = client.delete(f'/pods/{test_pod_multi_mount}', headers=headers)
    # Just verify no server error
    assert rsp.status_code < 500


### Pod with PVC Mount
def test_create_pod_with_pvc_mount(headers):
    """Test creating a pod with a PVC volume mount.
    PVC mounts use the shared NFS PVC (pods-data-pvc) with a sub_path.
    """
    pod_def = {
        "pod_id": test_pod_pvc,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with PVC mount",
        "networking": {
            "default": {
                "port": 5000,
                "protocol": "http"
            }
        },
        "volume_mounts": {
            "/pvc-data": {
                "type": "pvc",
                "source_id": test_volume_1
            }
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    assert result['pod_id'] == test_pod_pvc
    assert "/pvc-data" in result['volume_mounts']
    assert result['volume_mounts']['/pvc-data']['type'] == 'pvc'
    assert result['volume_mounts']['/pvc-data']['source_id'] == test_volume_1


def test_pod_with_pvc_mount_startup(headers):
    """Wait for PVC pod to become available."""
    i = 0
    while i < 30:
        rsp = client.get(f"/pods/{test_pod_pvc}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        # pod never became available
        assert False

    # Check the pod is running with PVC mount
    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_pvc
    assert result['volume_mounts']['/pvc-data']['type'] == 'pvc'


def test_delete_pod_with_pvc_mount(headers):
    """Delete the PVC pod to clean up."""
    rsp = client.delete(f'/pods/{test_pod_pvc}', headers=headers)
    # Just verify no server error
    assert rsp.status_code < 500


##### Multi-PVC mount testing (multiple mounts from SAME source_id = ONE PVC)
def test_create_pod_with_multiple_pvc_mounts_for_one_pvc(headers):
    """
    Test creating a pod with multiple mounts from a single PVC (same source_id):
    - One directory mount at /pvc-data
    - Two file mounts via sub_path at /etc/app/config.json and /var/log/app.log
    
    All mounts use the same source_id, so only ONE PVC should be created.
    The PVC is mounted multiple times with different sub_paths.
    """
    # Create a pod with multiple mounts from the same PVC
    pod_def = {
        "pod_id": test_pod_multi_pvc,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with multiple mounts from same PVC (one PVC created)",
        "volume_mounts": {
            "/pvc-data": {
                "type": "pvc",
                "source_id": test_volume_1
            },
            "/etc/app/config.json": {
                "type": "pvc",
                "source_id": test_volume_1,
                "sub_path": "config/app.json"
            },
            "/var/log/app.log": {
                "type": "pvc",
                "source_id": test_volume_1,
                "sub_path": "logs/app.log"
            }
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Verify all three mounts were created
    assert '/pvc-data' in result['volume_mounts']
    assert '/etc/app/config.json' in result['volume_mounts']
    assert '/var/log/app.log' in result['volume_mounts']

    # Verify mount types
    assert result['volume_mounts']['/pvc-data']['type'] == 'pvc'
    assert result['volume_mounts']['/etc/app/config.json']['type'] == 'pvc'
    assert result['volume_mounts']['/var/log/app.log']['type'] == 'pvc'

    # Verify all mounts reference the same source
    assert result['volume_mounts']['/pvc-data']['source_id'] == test_volume_1
    assert result['volume_mounts']['/etc/app/config.json']['source_id'] == test_volume_1
    assert result['volume_mounts']['/var/log/app.log']['source_id'] == test_volume_1

    # Verify sub_paths
    assert result['volume_mounts']['/etc/app/config.json']['sub_path'] == 'config/app.json'
    assert result['volume_mounts']['/var/log/app.log']['sub_path'] == 'logs/app.log'


def test_pod_with_multiple_pvc_mounts_startup(headers):
    """Wait for multi-PVC pod to become available."""
    i = 0
    while i < 30:
        rsp = client.get(f"/pods/{test_pod_multi_pvc}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        # pod never became available
        assert False

    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_multi_pvc
    # Verify mounts persisted correctly
    assert len(result['volume_mounts']) == 3
    assert result['volume_mounts']['/pvc-data']['type'] == 'pvc'
    assert result['volume_mounts']['/etc/app/config.json']['type'] == 'pvc'
    assert result['volume_mounts']['/var/log/app.log']['type'] == 'pvc'


def test_delete_pod_with_multiple_pvc_mounts(headers):
    """Delete the multi-PVC pod to clean up."""
    rsp = client.delete(f'/pods/{test_pod_multi_pvc}', headers=headers)
    # Just verify no server error
    assert rsp.status_code < 500


def test_create_pod_with_two_pvc_sources(headers):
    """
    Test creating a pod with 2 seperate PVC sources and mounting multiple mounts from each
    
    This should create exactly TWO PVCs, one per unique source_id.
    """
    pvc_source1 = "source1"
    pvc_source2 = "source2"
    pod_def = {
        "pod_id": test_pod_two_pvc,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with two different PVC sources (two PVCs created)",
        "volume_mounts": {
            # create pvc 1
            "/vol1-data": {
                "type": "pvc",
                "source_id": pvc_source1
            },
            "/vol1-config": {
                "type": "pvc",
                "source_id": pvc_source1,
                "sub_path": "config"
            },
            # Creates pvc 2
            "/vol2-data": {
                "type": "pvc",
                "source_id": pvc_source2
            },
            "/vol2-logs": {
                "type": "pvc",
                "source_id": pvc_source2,
                "sub_path": "logs"
            }
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # ensure 4 mounts
    assert '/vol1-data' in result['volume_mounts']
    assert '/vol1-config' in result['volume_mounts']
    assert '/vol2-data' in result['volume_mounts']
    assert '/vol2-logs' in result['volume_mounts']

    # ensure both pvc type
    for mount_path in result['volume_mounts']:
        assert result['volume_mounts'][mount_path]['type'] == 'pvc'

    assert result['volume_mounts']['/vol1-data']['source_id'] == pvc_source1
    assert result['volume_mounts']['/vol1-config']['source_id'] == pvc_source1
    assert result['volume_mounts']['/vol2-data']['source_id'] == pvc_source2
    assert result['volume_mounts']['/vol2-logs']['source_id'] == pvc_source2


def test_pod_with_two_pvc_sources_startup(headers):
    """Wait for two-PVC pod to become available."""
    i = 0
    while i < 30:
        rsp = client.get(f"/pods/{test_pod_two_pvc}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        # pod never became available
        assert False

    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_two_pvc
    assert len(result['volume_mounts']) == 4


def test_delete_pod_with_two_pvc_sources(headers):
    """Delete the two-PVC pod to clean up"""
    rsp = client.delete(f'/pods/{test_pod_two_pvc}', headers=headers)
    assert rsp.status_code < 500


##### Mixed mount testing (PVC + tapisvolume combined)

def test_create_pod_with_mixed_mounts(headers):
    """
    Test creating a pod with both PVC and tapisvolume mounts:
    - Two PVC mounts (dir + file)
    - Two tapisvolume mounts (dir + file)
    Tests that different mount types can coexist on the same pod.
    """
    pod_def = {
        "pod_id": test_pod_mixed,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with mixed PVC and tapisvolume mounts",
        "volume_mounts": {
            # PVC mounts
            "/pvc-data": {
                "type": "pvc",
                "source_id": test_volume_1
            },
            "/pvc-config/app.conf": {
                "type": "pvc",
                "source_id": test_volume_1,
                "sub_path": "config/app.conf"
            },
            # tapisvolume mounts
            "/tapis-data": {
                "type": "tapisvolume",
                "source_id": test_volume_1
            },
            "/tapis-logs/app.log": {
                "type": "tapisvolume",
                "source_id": test_volume_1,
                "sub_path": "logs/app.log"
            }
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Verify all four mounts were created
    assert '/pvc-data' in result['volume_mounts']
    assert '/pvc-config/app.conf' in result['volume_mounts']
    assert '/tapis-data' in result['volume_mounts']
    assert '/tapis-logs/app.log' in result['volume_mounts']

    # Verify PVC mount types
    assert result['volume_mounts']['/pvc-data']['type'] == 'pvc'
    assert result['volume_mounts']['/pvc-config/app.conf']['type'] == 'pvc'

    # Verify tapisvolume mount types
    assert result['volume_mounts']['/tapis-data']['type'] == 'tapisvolume'
    assert result['volume_mounts']['/tapis-logs/app.log']['type'] == 'tapisvolume'

    # Verify all mounts reference the same source
    for mount_path in result['volume_mounts']:
        assert result['volume_mounts'][mount_path]['source_id'] == test_volume_1

    # Verify sub_paths
    assert result['volume_mounts']['/pvc-config/app.conf']['sub_path'] == 'config/app.conf'
    assert result['volume_mounts']['/tapis-logs/app.log']['sub_path'] == 'logs/app.log'


def test_pod_with_mixed_mounts_startup(headers):
    """Wait for mixed-mount pod to become available."""
    i = 0
    while i < 30:
        rsp = client.get(f"/pods/{test_pod_mixed}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        # pod never became available
        assert False

    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_mixed
    # Verify mounts persisted correctly
    assert len(result['volume_mounts']) == 4
    # Check both types present
    pvc_count = sum(1 for m in result['volume_mounts'].values() if m['type'] == 'pvc')
    tapisvolume_count = sum(1 for m in result['volume_mounts'].values() if m['type'] == 'tapisvolume')
    assert pvc_count == 2
    assert tapisvolume_count == 2


def test_delete_pod_with_mixed_mounts(headers):
    """Delete the mixed-mount pod to clean up."""
    rsp = client.delete(f'/pods/{test_pod_mixed}', headers=headers)
    # Just verify no server error
    assert rsp.status_code < 500


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
