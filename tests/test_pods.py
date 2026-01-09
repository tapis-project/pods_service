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
test_pod_ephemeral_1 = "testspodsephemeral1"
test_pod_ephemeral_2 = "testspodsephemeral2"
test_pod_ephemeral_3 = "testspodsephemeral3"
test_pod_ephemeral_4 = "testspodsephemeral4"
test_pod_eph_unlimited = "testspodsephunlimited"
test_pod_eph_mixed = "testspodsephmixed"
test_pod_eph_mixed2 = "testspodsephmixed2"
test_pod_eph_skip = "testspodsephskip"


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
    pods = [test_pod_1, test_pod_error_1, test_pod_ephemeral_1, test_pod_ephemeral_2, test_pod_ephemeral_3, test_pod_ephemeral_4,
            test_pod_eph_unlimited, test_pod_eph_mixed, test_pod_eph_mixed2, test_pod_eph_skip]
    volumes = []
    for pod_id in pods:
        rsp = client.delete(f'/pods/{pod_id}', headers=headers)
    for vol_id in volumes:
        rsp = client.delete(f'/pods/volumes/{vol_id}', headers=headers)

##### Testing Pods
def test_list_pods(headers):
    rsp = client.get("/pods", headers=headers)
    result = basic_response_checks(rsp)
    assert result is not None

def test_create_pod(headers):
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
    }
    # Create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    # Check the pod object
    assert result['status'] == "REQUESTED"
    assert result['pod_id'] == test_pod_1
    assert result['image'] == "notchristiangarcia/testserver:fastapi"

def test_check_list_pods(headers):
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
    assert result['image'] == "notchristiangarcia/testserver:fastapi"

def test_get_pod(headers):
    rsp = client.get(f"/pods/{test_pod_1}", headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    #assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_1
    assert result['image'] == "notchristiangarcia/testserver:fastapi"

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
        "image": "notchristiangarcia/testserver:fastapi",
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
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "cafè"
    }
    # Attempt to create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    data = response_format(rsp)
    # Test error response.
    assert rsp.status_code == 400
    assert any('description field may only contain ASCII characters' in msg for msg in data['message'])


##### Ephemeral Storage Tests

def test_create_pod_with_ephemeral_storage(headers):
    """Test creating a pod with ephemeral storage request and limit set."""
    pod_def = {
        "pod_id": test_pod_ephemeral_1,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with ephemeral storage",
        "resources": {
            "ephemeral_storage_request": 1024,
            "ephemeral_storage_limit": 2048
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_ephemeral_1
    assert result['resources']['ephemeral_storage_request'] == 1024
    assert result['resources']['ephemeral_storage_limit'] == 2048


def test_get_pod_with_ephemeral_storage(headers):
    """Test that ephemeral storage values are returned when getting a pod."""
    rsp = client.get(f"/pods/{test_pod_ephemeral_1}", headers=headers)
    result = basic_response_checks(rsp)
    assert result['resources']['ephemeral_storage_request'] == 1024
    assert result['resources']['ephemeral_storage_limit'] == 2048


def test_update_pod_ephemeral_storage(headers):
    """Test updating a pod's ephemeral storage values."""
    pod_def = {
        "resources": {
            "ephemeral_storage_request": 2048,
            "ephemeral_storage_limit": 4096
        }
    }
    rsp = client.put(f"/pods/{test_pod_ephemeral_1}", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['resources']['ephemeral_storage_request'] == 2048
    assert result['resources']['ephemeral_storage_limit'] == 4096


def test_create_pod_with_only_ephemeral_request(headers):
    """Test creating a pod with only ephemeral_storage_request set."""
    pod_def = {
        "pod_id": test_pod_ephemeral_2,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with only ephemeral request",
        "resources": {
            "ephemeral_storage_request": 512
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_ephemeral_2
    assert result['resources']['ephemeral_storage_request'] == 512
    # ephemeral_storage_limit should have default value (-1 = unlimited)
    assert result['resources']['ephemeral_storage_limit'] == -1


def test_create_pod_with_only_ephemeral_limit(headers):
    """Test creating a pod with only ephemeral_storage_limit set."""
    pod_def = {
        "pod_id": test_pod_ephemeral_3,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with only ephemeral limit",
        "resources": {
            "ephemeral_storage_limit": 8192
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_ephemeral_3
    assert result['resources']['ephemeral_storage_limit'] == 8192
    # ephemeral_storage_request should have default value (-1 = unlimited)
    assert result['resources']['ephemeral_storage_request'] == -1


def test_ephemeral_storage_above_maximum_error(headers):
    """Test that ephemeral storage above maximum (18432 Mi) returns error."""
    pod_def = {
        "pod_id": test_pod_error_1,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with too much ephemeral storage",
        "resources": {
            "ephemeral_storage_request": 20000  # Above max of 18432
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    data = response_format(rsp)
    assert rsp.status_code == 400
    assert any('ephemeral_storage_x out of bounds' in msg for msg in data['message'])


def test_create_pod_with_all_resources(headers):
    """Test creating a pod with all resource fields including ephemeral storage."""
    pod_def = {
        "pod_id": test_pod_ephemeral_4,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with all resources",
        "resources": {
            "cpu_request": 500,
            "cpu_limit": 1000,
            "mem_request": 512,
            "mem_limit": 1024,
            "ephemeral_storage_request": 1024,
            "ephemeral_storage_limit": 2048
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_ephemeral_4
    assert result['resources']['cpu_request'] == 500
    assert result['resources']['cpu_limit'] == 1000
    assert result['resources']['mem_request'] == 512
    assert result['resources']['mem_limit'] == 1024
    assert result['resources']['ephemeral_storage_request'] == 1024
    assert result['resources']['ephemeral_storage_limit'] == 2048


def test_create_pod_with_unlimited_ephemeral_storage(headers):
    """Test creating a pod with -1 for unlimited ephemeral storage."""
    test_pod_unlimited = "testspodsephunlimited"
    pod_def = {
        "pod_id": test_pod_unlimited,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with unlimited ephemeral storage",
        "resources": {
            "ephemeral_storage_request": -1,
            "ephemeral_storage_limit": -1
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_unlimited
    assert result['resources']['ephemeral_storage_request'] == -1
    assert result['resources']['ephemeral_storage_limit'] == -1


def test_create_pod_with_unlimited_ephemeral_request_only(headers):
    """Test creating a pod with -1 request but specified limit."""
    test_pod_mixed = "testspodsephmixed"
    pod_def = {
        "pod_id": test_pod_mixed,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with unlimited request but limited limit",
        "resources": {
            "ephemeral_storage_request": -1,
            "ephemeral_storage_limit": 2048
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_mixed
    assert result['resources']['ephemeral_storage_request'] == -1
    assert result['resources']['ephemeral_storage_limit'] == 2048


def test_create_pod_with_unlimited_ephemeral_limit_only(headers):
    """Test creating a pod with specified request but -1 limit."""
    test_pod_mixed2 = "testspodsephmixed2"
    pod_def = {
        "pod_id": test_pod_mixed2,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with specified request but unlimited limit",
        "resources": {
            "ephemeral_storage_request": 1024,
            "ephemeral_storage_limit": -1
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_mixed2
    assert result['resources']['ephemeral_storage_request'] == 1024
    assert result['resources']['ephemeral_storage_limit'] == -1


def test_ephemeral_request_gt_limit_with_unlimited_skip_validation(headers):
    """Test that request > limit validation is skipped when either is -1."""
    test_pod_skip = "testspodsephskip"
    # This would normally fail (request > limit), but -1 skips validation
    pod_def = {
        "pod_id": test_pod_skip,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test validation skip with -1",
        "resources": {
            "ephemeral_storage_request": 5000,
            "ephemeral_storage_limit": -1
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_skip


def test_k8s_verify_all_ephemeral_storage_configs(headers):
    """Verify actual K8s pod specs for all ephemeral storage configurations."""
    from service.kubernetes_utils import k8, NAMESPACE
    
    # Wait for all pods to start
    time.sleep(7)
    
    # Test 1: Both request and limit are -1 (unlimited)
    k8_pod_unlimited = k8.read_namespaced_pod(name="pods-tacc-dev-testspodsephunlimited", namespace=NAMESPACE)
    resources_unlimited = k8_pod_unlimited.spec.containers[0].resources
    if resources_unlimited.limits:
        assert "ephemeral-storage" not in resources_unlimited.limits, "testspodsephunlimited: ephemeral-storage limit should not be set when -1"
    if resources_unlimited.requests:
        assert "ephemeral-storage" not in resources_unlimited.requests, "testspodsephunlimited: ephemeral-storage request should not be set when -1"
    
    # Test 2: Request is -1, limit is 2048Mi (2Gi)
    k8_pod_mixed = k8.read_namespaced_pod(name="pods-tacc-dev-testspodsephmixed", namespace=NAMESPACE)
    resources_mixed = k8_pod_mixed.spec.containers[0].resources
    assert resources_mixed.limits.get("ephemeral-storage") == "2Gi", "testspodsephmixed: ephemeral-storage limit should be 2Gi"
    # K8s auto-fills request=limit when request is not specified (expected behavior)
    if resources_mixed.requests and "ephemeral-storage" in resources_mixed.requests:
        assert resources_mixed.requests.get("ephemeral-storage") == "2Gi", "testspodsephmixed: K8s auto-filled request to match limit"
    
    # Test 3: Request is 1024Mi (1Gi), limit is -1 (unlimited)
    k8_pod_mixed2 = k8.read_namespaced_pod(name="pods-tacc-dev-testspodsephmixed2", namespace=NAMESPACE)
    resources_mixed2 = k8_pod_mixed2.spec.containers[0].resources
    assert resources_mixed2.requests.get("ephemeral-storage") == "1Gi", "testspodsephmixed2: ephemeral-storage request should be 1Gi"
    if resources_mixed2.limits:
        assert "ephemeral-storage" not in resources_mixed2.limits, "testspodsephmixed2: ephemeral-storage limit should not be set when -1"
    
    # Test 4: Request is 5000Mi, limit is -1 (validation skip test)
    k8_pod_skip = k8.read_namespaced_pod(name="pods-tacc-dev-testspodsephskip", namespace=NAMESPACE)
    resources_skip = k8_pod_skip.spec.containers[0].resources
    assert resources_skip.requests.get("ephemeral-storage") == "5000Mi", "testspodsephskip: ephemeral-storage request should be 5000Mi"
    if resources_skip.limits:
        assert "ephemeral-storage" not in resources_skip.limits, "testspodsephskip: ephemeral-storage limit should not be set when -1"
    
    # Test 5: Only limit is set to 8192Mi (8Gi), request defaults to -1
    k8_pod_ephemeral3 = k8.read_namespaced_pod(name="pods-tacc-dev-testspodsephemeral3", namespace=NAMESPACE)
    resources_ephemeral3 = k8_pod_ephemeral3.spec.containers[0].resources
    assert resources_ephemeral3.limits.get("ephemeral-storage") == "8Gi", "testspodsephemeral3: ephemeral-storage limit should be 8Gi"
    # K8s auto-fills request=limit when only limit is specified (expected behavior)
    if resources_ephemeral3.requests and "ephemeral-storage" in resources_ephemeral3.requests:
        assert resources_ephemeral3.requests.get("ephemeral-storage") == "8Gi", "testspodsephemeral3: K8s auto-filled request to match limit"

