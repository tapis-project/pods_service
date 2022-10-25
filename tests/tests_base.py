import os
import sys

# Allows us to import actor's modules.
sys.path.append('/home/tapis/service')

from fastapi.testclient import TestClient
from api import api
import json

from utils import headers, wait_for_rabbit, basic_response_checks, delete_pods


client = TestClient(api, base_url="https://dev.develop.tapis.io")

test_pod_1 = "testsuiteneo4jtest"

# Pre clean up
def test_delete_pods_init(headers):
    delete_pods(client, headers)


def test_get_pods(headers):
    rsp = client.get("/pods",
                     headers=headers)
    result = basic_response_checks(rsp)
    assert result != None

def test_create_pod(headers):
    rsp = client.post("/pods",
                     data=json.dumps({"pod_id": test_pod_1,
                                      "pod_template": "neo4j",
                                      "description": "Test Neo pod"}),
                     headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    assert result['status'] == "REQUESTED"
    assert result['pod_id'] == test_pod_1
    assert result['pod_template'] == "neo4j"

def test_check_get_pods(headers):
    rsp = client.get("/pods",
                     headers=headers)
    result = basic_response_checks(rsp)
    found_pod = False
    for pod in result:
        if pod["pod_id"] == test_pod_1:
            found_pod = True
            break
    assert found_pod

def test_get_pod(headers):
    rsp = client.get(f"/pods/{test_pod_1}",
                     headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    #assert result['status'] == "RUNNING"
    assert result['pod_id'] == test_pod_1
    assert result['pod_template'] == "neo4j"

def test_get_pod_logs(headers):
    rsp = client.get(f"/pods/{test_pod_1}/logs",
                     headers=headers)
    result = basic_response_checks(rsp)

    assert result['logs'] or result['logs'] == ''

def test_get_pod_credentials(headers):
    rsp = client.get(f"/pods/{test_pod_1}/credentials",
                     headers=headers)
    result = basic_response_checks(rsp)

    assert result['user_username']
    assert result['user_password']

def test_get_permissions(headers):
    rsp = client.get(f"/pods/{test_pod_1}/permissions",
                     headers=headers)
    result = basic_response_checks(rsp)

    assert result['permissions']

def test_desc_length(headers):
    rsp = client.post("/pods",
                     data=json.dumps({"pod_id": test_pod_2,
                                      "pod_template": "neo4j",
                                      "description": "Test"*200}),
                     headers=headers)

    data = response_format(rsp)

    # test for 400 error status code due to exceeding description length
    assert rsp.status_code == 400

    # test for right error message
    assert 'description: description field must be less than 255 characters.' in data['message']

def test_desc_char(headers):
    rsp = client.post("/pods",
                     data=json.dumps({"pod_id": test_pod_2,
                                      "pod_template": "neo4j",
                                      "description": "Test~~~"}),
                     headers=headers)

    data = response_format(rsp)
    assert rsp.status_code == 400
    assert 'description: description field must only contain alphanumeric values or the following special characters: !.?@#' in data['message']


# Clean up
def test_delete_pods(headers):
    delete_pods(client, headers)
