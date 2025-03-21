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
test_template_1 = "testtemplatecatchall" # We're just adding a lot of tags to one template, easier that way.
test_template_2 = "testtemplatetesting"

test_template_tag_0 = "latest" # latest is used to test default behaviour
test_template_tag_1 = "fastapi" # the rest will have a template for the specified service
test_template_tag_2 = "postgres"
test_template_tag_3 = "recursive"
test_template_tag_4 = "neo4j"
test_template_tag_5 = "fastapi.withperiod"

test_pod_1 = "testtemplatefastapi"
test_pod_2 = "testtemplatepostgres"
test_pod_3 = "testtemplaterecursive"
test_pod_4 = "testtemplateneo4j"
test_pod_5 = "testtemplateneo4jafterperiod"

##### Teardown
@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    """Delete all Pod service objects created during testing.

    This fixture is automatically invoked by pytest at the end of the test.
    """
    # yield so the fixture waits until the end of the tests in this file to continue
    yield None

    # Delete all objects after the tests are done.
    pods = [test_pod_1, test_pod_2, test_pod_3, test_pod_4, test_pod_5]
    templates = [test_template_1]
    for pod_id in pods:
        rsp = client.delete(f'/pods/{pod_id}', headers=headers)
    for template_id in templates:
        rsp = client.delete(f'/pods/templates/{template_id}', headers=headers)


### Testing Templates
def test_list_templates(headers):
    rsp = client.get("/pods/templates", headers=headers)
    result = basic_response_checks(rsp)
    print(result)
    assert result == []

def test_create_template(headers):
    # Definition
    template_def = {
        "template_id": test_template_1,
        "description": "Test template to store all tags for testing purposes",
        "metatags": ["test", "neo4j", "fastapi", "postgres", "recursive"],
    }
    # Create template
    rsp = client.post("/pods/templates", data=json.dumps(template_def), headers=headers)
    result = basic_response_checks(rsp)
    # Check the template
    assert result['template_id'] == test_template_1
    # Wait for template to be available
    time.sleep(2)


def test_check_get_images(headers):
    rsp = client.get("/pods/templates", headers=headers)
    result = basic_response_checks(rsp)
    print(result)
    found_template = False
    for template in result:
        if template["template_id"] == test_template_1:
            found_template = True
            break
    assert found_template


def test_get_template(headers):
    rsp = client.get(f"/pods/templates/{test_template_1}", headers=headers)
    result = basic_response_checks(rsp)
    assert result['template_id'] == test_template_1


def test_get_permissions(headers):
    rsp = client.get(f"/pods/templates/{test_template_1}/permissions", headers=headers)
    result = basic_response_checks(rsp)
    assert result['permissions']


def test_set_permissions(headers):
    # Definition
    perm_def = {
        "user": "testuser",
        "level": "READ"
    }
    # Create user permission on template
    rsp = client.post(f"/pods/templates/{test_template_1}/permissions", data=json.dumps(perm_def), headers=headers)
    result = basic_response_checks(rsp)
    assert "testuser:READ" in result['permissions']


def test_delete_set_permissions(headers):
    user = "testuser"
    # Delete user permission from template
    rsp = client.delete(f"/pods/templates/{test_template_1}/permissions/{user}", headers=headers)
    result = basic_response_checks(rsp)
    assert "Template permission deleted successfully." in rsp.json()['message']


def test_update_template(headers):
    # Definition - both description and tags are updated
    template_def = {
        "description": "Test template to store all tags for testing purposes - updated",
        "metatags": ["test", "neo4j", "fastapi", "postgres", "recursive", "updated"],
    }
    # Update template
    rsp = client.put(f"/pods/templates/{test_template_1}", data=json.dumps(template_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['description'] == "Test template to store all tags for testing purposes - updated"
   
@pytest.mark.skip("Skipping archive - currently always archived when archive arg is set")
def test_archive_template(headers):
    # Archive template
    rsp = client.get(f"/pods/templates/{test_template_1}/archive", headers=headers)
    result = basic_response_checks(rsp)
    assert "Template archived successfully" in rsp.json()['message']

## test that creates pod, pod metadata should say that the template is already archived
## and should show the message why it was archived
@pytest.mark.skip("Skipping archive - currently always archived when archive arg is set")
def test_pod_created_with_archived_template(headers):
    pod_def = {
        "pod_id": test_pod_1,
        "template": test_template_1,
    }
    # Attempt to create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    assert result['pod_id'] == test_pod_1
    assert test_template_1 in result['template']
    assert "This image has been archived for being bad. Please stop using it - test." in result['template']


### Create a lot of template tags on the original template
def test_add_template_helloworld(headers):
    tag_def = {
        "pod_definition": {
            "image": "tiangolo/uvicorn-gunicorn-fastapi",
            "command": ["echo", "Hello, World!"],
            "resources": {
                "cpu_request": 500 # setting this so recursive can overwrite it later.
            }
        },
        "commit_message": "pod should echo Hello, World! to stdout"
    }
    # Add tag to template
    rsp = client.post(f"/pods/templates/{test_template_1}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_0 in result['tag_timestamp'] # tag should be latest


def test_list_template_tags(headers):
    rsp = client.get(f"/pods/templates/{test_template_1}/tags", headers=headers)
    result = basic_response_checks(rsp)
    assert len(result) == 1
    for tag in result:
        assert tag['tag'] == test_template_tag_0
        assert test_template_tag_0 in tag['tag_timestamp']


def test_add_template_fastapi(headers):
    # this is the second template tag on the same template
    tag_def = {
        "pod_definition": {
            "image": "tiangolo/uvicorn-gunicorn-fastapi"
        },
        "tag": test_template_tag_1,
        "commit_message": "fastapi test server which returns a fastapi startup message"
    }
    # Add tag to template
    rsp = client.post(f"/pods/templates/{test_template_1}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_1 in result['tag_timestamp']


def test_list_template_tags_2(headers):
    # should now have two tags
    rsp = client.get(f"/pods/templates/{test_template_1}/tags", headers=headers)
    result = basic_response_checks(rsp)
    assert len(result) == 2
    for tag in result:
        assert tag['tag'] in [test_template_tag_0, test_template_tag_1]


def test_add_template_postgres(headers):
    # this is the second template tag on the same template
    tag_def = {
        "pod_definition": {
            "description": "Postgres template",
            "image": "postgres:14",
            "command": [
                "docker-entrypoint.sh"
            ],
            "arguments": [
                "-c", "ssl=on",
                "-c", "ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem",
                "-c", "ssl_key_file=/etc/ssl/private/ssl-cert-snakeoil.key"
            ],
            "environment_variables": {
                "POSTGRES_USER": "<<TAPIS_user_username>>",
                "POSTGRES_PASSWORD": "<<TAPIS_user_password>>",
            },
            "networking": {
                "default": {
                    "port": 5432,
                    "protocol": "postgres"
                }
            }
        },
        "tag": test_template_tag_2,
        "commit_message": "postgres main template!"
    }
    # Add tag to template
    rsp = client.post(f"/pods/templates/{test_template_1}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_2 in result['tag_timestamp']


def test_add_template_recursive(headers):
    # we overwrite fastapi template validate
    tag_def = {
        "pod_definition": {
            "command": ["echo", "Hello! Recursive was here!"],
            "template": f"{test_template_1}:{test_template_tag_1}",
            "resources": {
                "cpu_request": 400 # :latest sets to 500, we overwrite
            }
        },
        "tag": test_template_tag_3,
        "commit_message": "fastapi test server which returns a fastapi startup message"
    }
    # Add tag to template
    rsp = client.post(f"/pods/templates/{test_template_1}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_3 in result['tag_timestamp']


def test_add_template_neo4j(headers):
    # this is the second template tag on the same template
    tag_def = {
        "pod_definition": {
            "image": "notchristiangarcia/neo4j:4.4",
            "command": [
                "/bin/bash",
                "-c",
                "mkdir /certificates && openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout /certificates/snakeoil.key -out /certificates/snakeoil.crt -subj \"/CN=neo4j\" && chmod -R 777 /certificates && export NEO4J_dbms_default__advertised__address=$(hostname -f) && exec /docker-entrypoint.sh \"neo4j\""
            ],
            "networking": {
                "default": {
                    "port": 7687,
                    "protocol": "tcp"
                },
                # this is only optionally available, maybe create a new template just for it?
                "browser": {
                    "port": 7474,
                    "protocol": "http"
                }
            },
            "environment_variables": {
                #"NEO4JLABS_PLUGINS": '["apoc", "n10s"]', # not needed with custom notchristiangarcia/neo4j image
                "NEO4J_dbms_ssl_policy_bolt_enabled": "true",
                "NEO4J_dbms_ssl_policy_bolt_base__directory": "/certificates", # Can't mount anything to /var/lib/neo4j. Neo4j attempts chown, read-only. So change dir.
                "NEO4J_dbms_ssl_policy_bolt_private__key": "snakeoil.key",
                "NEO4J_dbms_ssl_policy_bolt_public__certificate": "snakeoil.crt",
                "NEO4J_dbms_ssl_policy_bolt_client__auth": "NONE",
                "NEO4J_dbms_security_auth__enabled": "true",
                "NEO4J_dbms_mode": "SINGLE",
                "NEO4J_apoc_import_file_enabled": "true",
                "NEO4J_apoc_export_file_enabled": "true",
                # Create users here with env and apoc. Different format than Neo4J. Kinda borked, might change. github.com/neo4j-contrib/neo4j-apoc-procedures/issues/2120
                # Pods admin user
                "apoc.initializer.system.1": f"CREATE USER <<TAPIS_admin_username>> IF NOT EXISTS SET PLAINTEXT PASSWORD '<<TAPIS_admin_password>>' SET PASSWORD CHANGE NOT REQUIRED",
                # Users user
                "apoc.initializer.system.2": f"CREATE USER <<TAPIS_user_username>> IF NOT EXISTS SET PLAINTEXT PASSWORD '<<TAPIS_user_password>>' SET PASSWORD CHANGE NOT REQUIRED"
            },
        },
        "tag": test_template_tag_4,
        "commit_message": "neo4j main template!"
    }
    # Add tag to template
    rsp = client.post(f"/pods/templates/{test_template_1}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_4 in result['tag_timestamp']

def test_list_template_tags_later(headers):
    rsp = client.get("/pods/templates/testtemplatecatchall/tags", headers=headers)
    result = basic_response_checks(rsp)
    assert len(result) == 5


def test_add_template_fastapi_withperiods(headers):
    # this is the second template tag on the same template
    tag_def = {
        "pod_definition": {
            "image": "tiangolo/uvicorn-gunicorn-fastapi"
        },
        "tag": test_template_tag_5,
        "commit_message": "fastapi test server which returns a fastapi startup message"
    }
    # Add tag to template
    rsp = client.post(f"/pods/templates/{test_template_1}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_5 in result['tag_timestamp']


def test_list_template_tags_with_period(headers):
    rsp = client.get(f"/pods/templates/{test_template_1}/tags", headers=headers)
    result = basic_response_checks(rsp)
    assert len(result) == 6
    for tag in result:
        assert tag['tag'] in [test_template_tag_0, test_template_tag_1, test_template_tag_2, test_template_tag_3, test_template_tag_4, test_template_tag_5]

###
### Create pods with templates
###
def test_create_pod_from_fastapi_template(headers):
    pod_def = {
        "pod_id": test_pod_1,
        "template": f"{test_template_1}:{test_template_tag_1}",
    }
    # Attempt to create pod
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)

    # Check the pod object
    assert result['pod_id'] == test_pod_1
    assert test_template_1 in result['template']
    # info that template should have written
    # info that pod should have overwritten from template

def test_create_pod_from_postgres_template(headers):
    pod_def = {
        "pod_id": test_pod_2,
        "template": f"{test_template_1}:{test_template_tag_2}",
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_2
    assert test_template_1 in result['template']

def test_create_pod_from_recursive_template(headers):
    pod_def = {
        "pod_id": test_pod_3,
        "template": f"{test_template_1}:{test_template_tag_3}",
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_3
    assert test_template_1 in result['template']
    
def test_create_pod_from_neo4j_template(headers):
    pod_def = {
        "pod_id": test_pod_4,
        "template": f"{test_template_1}:{test_template_tag_4}",
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_4
    assert test_template_1 in result['template']

def test_create_pod_from_neo4j_afterperiod_template(headers):
    pod_def = {
        "pod_id": test_pod_5,
        "template": f"{test_template_1}:{test_template_tag_5}",
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_5
    assert test_template_1 in result['template']
###
### Check status of pods starting with templates
###
def test_startup_pod_from_fastapi_template(headers):
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
        res = client.get(f"/pods/{test_pod_1}/logs", headers=headers)
        assert False # pod never became available
    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_1
    assert test_template_1 in result['template']


def test_startup_pod_from_postgres_template_startup(headers):
    i = 0
    while i < 10:
        rsp = client.get(f"/pods/{test_pod_2}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        assert False
    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_2
    assert test_template_1 in result['template']


def test_startup_pod_from_recursive_template_startup(headers):
    #### THIS ISN'T A LONG RUNNING Image
    # It immediately echo's and goes to COMPLETE. Check for that instead.
    i = 0
    while i < 10:
        rsp = client.get(f"/pods/{test_pod_3}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "COMPLETE":
            break
        time.sleep(2)
        i += 1
    else:
        assert False
    assert result['status'] == "COMPLETE"
    assert result['pod_id'] == test_pod_3
    assert test_template_1 in result['template']


def test_startup_pod_from_neo4j_template_startup(headers):
    i = 0
    while i < 10:
        rsp = client.get(f"/pods/{test_pod_4}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        assert False
    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_4
    assert test_template_1 in result['template']


def test_startup_pod_from_neo4j_afterperiod_template_startup(headers):
    i = 0
    while i < 10:
        rsp = client.get(f"/pods/{test_pod_5}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] == "AVAILABLE":
            break
        time.sleep(2)
        i += 1
    else:
        assert False
    assert result['status'] == "AVAILABLE"
    assert result['pod_id'] == test_pod_5
    assert test_template_1 in result['template']


##### Error testing
## Need to test with template with volume
## Need to check template deletion after we ensure tags deleted are not in use
def test_description_length_400(headers):
    # Definition
    template_def = {
        "template_id": test_template_1,
        "description": "Test" * 200,
        "metatags": ["test", "neo4j-template"]
    }
    # Attempt to create pod
    rsp = client.post("/pods/templates", data=json.dumps(template_def), headers=headers)
    data = response_format(rsp)
    # Test error response.
    assert rsp.status_code == 400
    assert any('description field must be less than 255 characters.' in msg for msg in data['message'])


def test_description_is_ascii_400(headers):
    # Definition
    template_def = {
        "template_id": test_template_1,
        "description": "cafè",
        "metatags": ["test", "neo4j-template"]
    }
    # Attempt to create pod
    rsp = client.post("/pods/templates", data=json.dumps(template_def), headers=headers)
    data = response_format(rsp)
    # Test error response.
    assert rsp.status_code == 400
    assert any('description field may only contain ASCII characters' in msg for msg in data['message'])

def test_stop_for_debug():
    if True:
        time.sleep(150)
    
def test_delete_template(headers):
    # Delete template
    rsp = client.delete(f"/pods/templates/{test_template_1}", headers=headers)
    result = basic_response_checks(rsp)
    assert "Template and associated Template Tags successfully deleted." in rsp.json()['message']

