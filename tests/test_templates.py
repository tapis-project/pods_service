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

test_template_ephemeral = "testtemplateephemeral"
test_template_tag_ephemeral = "ephemeral"
test_template_tag_ephemeral_recursive = "ephemeral-recursive"
test_pod_ephemeral_template = "testpodephemeraltemplate"
test_pod_ephemeral_override = "testpodephemeraloverride"
test_pod_ephemeral_recursive = "testpodephemeralrecursive"
test_template_overrides = "testtemplateoverrides"
test_template_tag_overrides = "overridesbase"
test_pod_overrides_vm = "testpodoverridesvm"
test_pod_overrides_sm = "testpodoverridessm"
test_pod_overrides_both = "testpodoverridesboth"

# Secret_map Placeholder Tests variables
test_template_secrets = "testtemplatesecrets"
test_template_tag_secrets = "withsecrets"
test_template_tag_secrets_required = "requiredsecrets"
test_template_tag_secrets_invalid = "invalidsecrets"
test_pod_secrets_template = "testpodsecretstmpl"
test_pod_secrets_override = "testpodsecretsovrde"

# Template Tag Delete Tests variables
test_template_tag_delete_by_tag = "deletetag"
test_template_tag_delete_by_timestamp = "deletetimestamp"
test_template_tag_delete_with_force = "deleteforce"


##### Teardown
@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    """Delete all Pod service objects created during testing.

    This fixture is automatically invoked by pytest at the end of the test.
    """
    # yield so the fixture waits until the end of the tests in this file to continue
    yield None

    # Delete all objects after the tests are done.
    pods = [
        test_pod_1, test_pod_2, test_pod_3, test_pod_4, test_pod_5,
        test_pod_ephemeral_template, test_pod_ephemeral_override, test_pod_ephemeral_recursive,
        test_pod_overrides_vm, test_pod_overrides_sm, test_pod_overrides_both,
        test_pod_secrets_template, test_pod_secrets_override,
    ]
    templates = [
        test_template_1, test_template_2,
        test_template_ephemeral,
        test_template_overrides,
        test_template_secrets,
    ]
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
            "image": "notchristiangarcia/testserver:fastapi",
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
            "image": "notchristiangarcia/testserver:fastapi"
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
                "POSTGRES_USER": "<<tapissecret_user_username>>",
                "POSTGRES_PASSWORD": "<<tapissecret_user_password>>",
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
                "apoc.initializer.system.1": f"CREATE USER <<tapissecret_admin_username>> IF NOT EXISTS SET PLAINTEXT PASSWORD '<<tapissecret_admin_password>>' SET PASSWORD CHANGE NOT REQUIRED",
                # Users user
                "apoc.initializer.system.2": f"CREATE USER <<tapissecret_user_username>> IF NOT EXISTS SET PLAINTEXT PASSWORD '<<tapissecret_user_password>>' SET PASSWORD CHANGE NOT REQUIRED"
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
            "image": "notchristiangarcia/testserver:fastapi"
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
### Template Tag Delete Tests
###
def test_add_tags_for_delete_tests(headers):
    """Add template tags to test deletion."""
    # Add tag for delete by tag name
    tag_def = {
        "pod_definition": {"image": "notchristiangarcia/testserver:fastapi"},
        "tag": test_template_tag_delete_by_tag,
        "commit_message": "Tag for delete by tag name test"
    }
    rsp = client.post(f"/pods/templates/{test_template_1}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_delete_by_tag in result['tag_timestamp']
    
    # Add tag for delete by timestamp
    tag_def["tag"] = test_template_tag_delete_by_timestamp
    rsp = client.post(f"/pods/templates/{test_template_1}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    global delete_by_timestamp_full
    delete_by_timestamp_full = result['tag_timestamp']
    
    # Add tag for force delete
    tag_def["tag"] = test_template_tag_delete_with_force
    rsp = client.post(f"/pods/templates/{test_template_1}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    global delete_force_full_timestamp
    delete_force_full_timestamp = result['tag_timestamp']


def test_delete_template_tag_by_tag_name(headers):
    """Test deleting a template tag by tag name only."""
    rsp = client.delete(f"/pods/templates/{test_template_1}/tags/{test_template_tag_delete_by_tag}", headers=headers)
    result = basic_response_checks(rsp)
    
    # Confirm tag no longer exists
    rsp = client.get(f"/pods/templates/{test_template_1}/tags", headers=headers)
    result = basic_response_checks(rsp)
    tags = [tag['tag'] for tag in result]
    assert test_template_tag_delete_by_tag not in tags


def test_delete_template_tag_by_full_timestamp(headers):
    """Test deleting a template tag by tag@timestamp."""
    rsp = client.delete(f"/pods/templates/{test_template_1}/tags/{delete_by_timestamp_full}", headers=headers)
    result = basic_response_checks(rsp)
    
    # Confirm tag no longer exists
    rsp = client.get(f"/pods/templates/{test_template_1}/tags", headers=headers)
    result = basic_response_checks(rsp)
    tags = [tag['tag'] for tag in result]
    assert test_template_tag_delete_by_timestamp not in tags


def test_delete_template_tag_with_force(headers):
    """Test deleting a tag with force=true."""
    rsp = client.delete(f"/pods/templates/{test_template_1}/tags/{delete_force_full_timestamp}?force=true", headers=headers)
    result = basic_response_checks(rsp)
    
    # Confirm tag no longer exists
    rsp = client.get(f"/pods/templates/{test_template_1}/tags", headers=headers)
    result = basic_response_checks(rsp)
    tags = [tag['tag'] for tag in result]
    assert test_template_tag_delete_with_force not in tags


def test_delete_nonexistent_template_tag_fails(headers):
    """Test that deleting a non-existent tag returns appropriate error."""
    rsp = client.delete(f"/pods/templates/{test_template_1}/tags/nonexistenttag", headers=headers)
    assert rsp.status_code in [400, 404], f"Expected 400 or 404, got {rsp.status_code}"


###
### Template Dependencies Tests (include_dependencies parameter)
###
def test_list_template_tags_with_include_dependencies(headers):
    """Test that list_template_tags endpoint returns dependents when include_dependencies=true."""
    rsp = client.get(f"/pods/templates/{test_template_1}/tags?include_dependencies=true", headers=headers)
    result = basic_response_checks(rsp)
    
    # Should have template tags
    assert len(result) >= 1
    
    # Each tag should have a dependents field when include_dependencies=true
    for tag in result:
        assert 'dependents' in tag, f"Tag {tag.get('tag')} missing 'dependents' field"
        dependents = tag['dependents']
        # Check structure of dependents object
        assert 'dependant_pods' in dependents
        assert 'dependant_pod_count' in dependents
        assert 'dependant_tags' in dependents
        assert 'dependant_tags_count' in dependents
        # Values should be appropriate types
        assert isinstance(dependents['dependant_pods'], list)
        assert isinstance(dependents['dependant_pod_count'], int)
        assert isinstance(dependents['dependant_tags'], list)
        assert isinstance(dependents['dependant_tags_count'], int)


def test_list_template_tags_without_include_dependencies(headers):
    """Test that list_template_tags endpoint does NOT return dependents when include_dependencies is not set."""
    rsp = client.get(f"/pods/templates/{test_template_1}/tags", headers=headers)
    result = basic_response_checks(rsp)
    
    # Should have template tags
    assert len(result) >= 1
    
    # Tags should NOT have dependents field when include_dependencies is not set
    for tag in result:
        assert 'dependents' not in tag, f"Tag {tag.get('tag')} should not have 'dependents' field when include_dependencies is false"


def test_list_template_tags_include_dependencies_false(headers):
    """Test that list_template_tags endpoint does NOT return dependents when include_dependencies=false."""
    rsp = client.get(f"/pods/templates/{test_template_1}/tags?include_dependencies=false", headers=headers)
    result = basic_response_checks(rsp)
    
    # Should have template tags
    assert len(result) >= 1
    
    # Tags should NOT have dependents field
    for tag in result:
        assert 'dependents' not in tag, f"Tag {tag.get('tag')} should not have 'dependents' field when include_dependencies=false"


def test_list_template_tags_with_full_and_include_dependencies(headers):
    """Test that list_template_tags endpoint works with both full=true and include_dependencies=true."""
    rsp = client.get(f"/pods/templates/{test_template_1}/tags?full=true&include_dependencies=true", headers=headers)
    result = basic_response_checks(rsp)
    
    assert len(result) >= 1
    
    for tag in result:
        # Should have pod_definition when full=true
        assert 'pod_definition' in tag, f"Tag {tag.get('tag')} missing 'pod_definition' field"
        # Should have dependents when include_dependencies=true
        assert 'dependents' in tag, f"Tag {tag.get('tag')} missing 'dependents' field"


def test_get_template_tag_with_include_dependencies(headers):
    """Test that get_template_tag endpoint (specific tag) returns dependents when include_dependencies=true."""
    rsp = client.get(f"/pods/templates/{test_template_1}/tags/{test_template_tag_1}?include_dependencies=true", headers=headers)
    result = basic_response_checks(rsp)
    
    # Should return at least one tag
    assert len(result) >= 1
    
    # Each returned tag should have dependents
    for tag in result:
        assert 'dependents' in tag, f"Tag {tag.get('tag')} missing 'dependents' field"
        dependents = tag['dependents']
        assert 'dependant_pods' in dependents
        assert 'dependant_pod_count' in dependents
        assert 'dependant_tags' in dependents
        assert 'dependant_tags_count' in dependents


def test_tag_id_injection_is_not_evaluated(headers):
    """Security regression: tag_id (a raw URL path param) flows into db_get_where.
    It used to be spliced into an eval() string — arbitrary Python RCE. Now it's a
    parameterized column comparison, so an injection payload is simply a tag name
    that matches nothing (200 with empty result), never executed, never a 500."""
    payloads = [
        "'+str(__import__('os').getpid())+'",
        "x' or '1'='1",
        "'; import os; os.system('id'); '",
        "latest') | (TemplateTag.tag == 'latest",
    ]
    for p in payloads:
        rsp = client.get(f"/pods/templates/{test_template_1}/tags/{p}", headers=headers)
        # Not a 500 (would mean the string reached an interpreter/query error) and
        # not a match — the payload is treated as an opaque tag name.
        assert rsp.status_code in (200, 404), f"payload {p!r} gave {rsp.status_code}"
        if rsp.status_code == 200:
            assert response_format(rsp)["result"] == [], f"payload {p!r} matched a tag"


def test_get_template_with_include_dependencies(headers):
    """Test that get_template endpoint returns tag_dependents when include_dependencies=true."""
    rsp = client.get(f"/pods/templates/{test_template_1}?include_dependencies=true", headers=headers)
    result = basic_response_checks(rsp)
    
    # Should have template info
    assert result['template_id'] == test_template_1
    
    # Should have tag_dependents field when include_dependencies=true
    assert 'tag_dependents' in result, "Template missing 'tag_dependents' field"
    tag_dependents = result['tag_dependents']
    
    # tag_dependents should be a list
    assert isinstance(tag_dependents, list)
    
    # If there are dependents, check their structure
    for dep in tag_dependents:
        assert 'tag_timestamp' in dep
        assert 'dependant_pods' in dep
        assert 'dependant_pod_count' in dep
        assert 'dependant_tags' in dep
        assert 'dependant_tags_count' in dep


def test_get_template_without_include_dependencies(headers):
    """Test that get_template endpoint does NOT return tag_dependents when include_dependencies is not set."""
    rsp = client.get(f"/pods/templates/{test_template_1}", headers=headers)
    result = basic_response_checks(rsp)
    
    # Should have template info
    assert result['template_id'] == test_template_1
    
    # Should NOT have tag_dependents field
    assert 'tag_dependents' not in result, "Template should not have 'tag_dependents' field when include_dependencies is false"


def test_list_templates_with_include_dependencies(headers):
    """Test that list_templates endpoint returns tag_dependents when include_dependencies=true."""
    rsp = client.get("/pods/templates?include_dependencies=true", headers=headers)
    result = basic_response_checks(rsp)
    
    # Should have templates
    assert len(result) >= 1
    
    # Find our test template and check it has tag_dependents
    test_template_found = False
    for template in result:
        if template.get('template_id') == test_template_1:
            test_template_found = True
            assert 'tag_dependents' in template, f"Template {test_template_1} missing 'tag_dependents' field"
            assert isinstance(template['tag_dependents'], list)
            break
    
    assert test_template_found, f"Test template {test_template_1} not found in list"


def test_list_templates_without_include_dependencies(headers):
    """Test that list_templates endpoint does NOT return tag_dependents when include_dependencies is not set."""
    rsp = client.get("/pods/templates", headers=headers)
    result = basic_response_checks(rsp)
    
    # Should have templates
    assert len(result) >= 1
    
    # Templates should NOT have tag_dependents field
    for template in result:
        assert 'tag_dependents' not in template, f"Template {template.get('template_id')} should not have 'tag_dependents' field"


def test_list_templates_and_tags_with_include_dependencies(headers):
    """Test that list_templates_and_tags endpoint returns dependents when include_dependencies=true."""
    rsp = client.get("/pods/templates/tags?include_dependencies=true", headers=headers)
    result = basic_response_checks(rsp)
    
    # Result is a dict with template_id as keys
    assert isinstance(result, dict)
    assert len(result) >= 1
    
    # Find our test template
    assert test_template_1 in result, f"Test template {test_template_1} not found"
    
    template_data = result[test_template_1]
    assert 'tags' in template_data
    
    # Each tag should have dependents
    for tag in template_data['tags']:
        assert 'dependents' in tag, f"Tag {tag.get('tag')} missing 'dependents' field"
        dependents = tag['dependents']
        assert 'dependant_pods' in dependents
        assert 'dependant_pod_count' in dependents
        assert 'dependant_tags' in dependents
        assert 'dependant_tags_count' in dependents


def test_list_templates_and_tags_without_include_dependencies(headers):
    """Test that list_templates_and_tags endpoint does NOT return dependents when include_dependencies is not set."""
    rsp = client.get("/pods/templates/tags", headers=headers)
    result = basic_response_checks(rsp)
    
    # Result is a dict with template_id as keys
    assert isinstance(result, dict)
    assert len(result) >= 1
    
    # Find our test template
    assert test_template_1 in result, f"Test template {test_template_1} not found"
    
    template_data = result[test_template_1]
    assert 'tags' in template_data
    
    # Tags should NOT have dependents field
    for tag in template_data['tags']:
        assert 'dependents' not in tag, f"Tag {tag.get('tag')} should not have 'dependents' field"


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


##### Ephemeral Storage Template Tests
def test_create_template_with_ephemeral_storage(headers):
    """Test creating a template with ephemeral storage in pod_definition."""
    template_def = {
        "template_id": test_template_ephemeral,
        "description": "Template with ephemeral storage for testing",
        "metatags": ["test", "ephemeral-storage"]
    }
    rsp = client.post("/pods/templates", data=json.dumps(template_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['template_id'] == test_template_ephemeral


def test_add_template_tag_with_ephemeral_storage(headers):
    """Test adding a template tag with ephemeral storage resources."""
    tag_def = {
        "pod_definition": {
            "image": "notchristiangarcia/testserver:fastapi",
            "description": "Template tag with ephemeral storage",
            "resources": {
                "ephemeral_storage_request": 2048,
                "ephemeral_storage_limit": 4096
            }
        },
        "tag": test_template_tag_ephemeral,
        "commit_message": "Template tag with ephemeral storage resources"
    }
    rsp = client.post(f"/pods/templates/{test_template_ephemeral}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_ephemeral in result['tag_timestamp']


def test_get_template_tag_with_ephemeral_storage(headers):
    """Test that ephemeral storage values are returned when getting a template tag."""
    rsp = client.get(f"/pods/templates/{test_template_ephemeral}/tags/{test_template_tag_ephemeral}", headers=headers)
    result = basic_response_checks(rsp)
    # API returns a list of matching tags
    assert result[0]['pod_definition']['resources']['ephemeral_storage_request'] == 2048
    assert result[0]['pod_definition']['resources']['ephemeral_storage_limit'] == 4096


def test_create_pod_from_ephemeral_template(headers):
    """Test creating a pod from a template with ephemeral storage."""
    pod_def = {
        "pod_id": test_pod_ephemeral_template,
        "template": f"{test_template_ephemeral}:{test_template_tag_ephemeral}"
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_ephemeral_template


def test_pod_from_ephemeral_template_inherits_resources(headers):
    """Test that pod created from template inherits ephemeral storage values."""
    rsp = client.get(f"/pods/{test_pod_ephemeral_template}/derived", headers=headers)
    result = basic_response_checks(rsp)
    # Pod should inherit ephemeral storage from template
    assert result['resources']['ephemeral_storage_request'] == 2048  # From template
    assert result['resources']['ephemeral_storage_limit'] == 4096    # From template


def test_pod_override_template_ephemeral_storage(headers):
    """Test that pod can override template ephemeral storage values."""
    pod_def = {
        "pod_id": test_pod_ephemeral_override,
        "template": f"{test_template_ephemeral}:{test_template_tag_ephemeral}",
        "resources": {
            "ephemeral_storage_request": 1024,
            "ephemeral_storage_limit": 8192
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_ephemeral_override
    # Pod should use overridden values, not template values
    assert result['resources']['ephemeral_storage_request'] == 1024
    assert result['resources']['ephemeral_storage_limit'] == 8192


def test_add_template_tag_with_all_resources(headers):
    """Test adding a template tag with all resource fields including ephemeral storage."""
    tag_def = {
        "pod_definition": {
            "image": "notchristiangarcia/testserver:fastapi",
            "description": "Template tag with all resources",
            "resources": {
                "cpu_request": 500,
                "cpu_limit": 1000,
                "mem_request": 512,
                "mem_limit": 1024,
                "ephemeral_storage_request": 1024,
                "ephemeral_storage_limit": 2048
            }
        },
        "tag": "all-resources",
        "commit_message": "Template tag with all resource fields"
    }
    rsp = client.post(f"/pods/templates/{test_template_ephemeral}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert "all-resources" in result['tag_timestamp']


def test_template_ephemeral_storage_validation_error(headers):
    """Test that ephemeral storage above maximum returns error in template tag."""
    tag_def = {
        "pod_definition": {
            "image": "notchristiangarcia/testserver:fastapi",
            "resources": {
                "ephemeral_storage_request": 20000  # Above max of 18432
            }
        },
        "tag": "invalid-ephemeral",
        "commit_message": "This should fail"
    }
    rsp = client.post(f"/pods/templates/{test_template_ephemeral}/tags", data=json.dumps(tag_def), headers=headers)
    data = response_format(rsp)
    assert rsp.status_code == 400
    assert any('ephemeral_storage_x out of bounds' in msg for msg in data['message'])


def test_add_recursive_template_with_ephemeral_storage(headers):
    """Test adding a recursive template that modifies ephemeral storage from parent."""
    tag_def = {
        "pod_definition": {
            "template": f"{test_template_ephemeral}:{test_template_tag_ephemeral}",
            "description": "Recursive template with different ephemeral storage",
            "resources": {
                "ephemeral_storage_request": 3072,  # Different from parent's 2048
                "ephemeral_storage_limit": 6144     # Different from parent's 4096
            }
        },
        "tag": test_template_tag_ephemeral_recursive,
        "commit_message": "Recursive template overriding ephemeral storage"
    }
    rsp = client.post(f"/pods/templates/{test_template_ephemeral}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_ephemeral_recursive in result['tag_timestamp']


def test_create_pod_from_recursive_ephemeral_template(headers):
    """Test creating a pod from recursive template with ephemeral storage."""
    pod_def = {
        "pod_id": test_pod_ephemeral_recursive,
        "template": f"{test_template_ephemeral}:{test_template_tag_ephemeral_recursive}"
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_ephemeral_recursive

    # Get derived pod to check resources
    rsp = client.get(f"/pods/{test_pod_ephemeral_recursive}/derived", headers=headers)
    result = basic_response_checks(rsp)
    # Pod should inherit ephemeral storage from recursive template (overrides parent template)
    assert result['resources']['ephemeral_storage_request'] == 3072  # From recursive template
    assert result['resources']['ephemeral_storage_limit'] == 6144    # From recursive template


##### Secret_map Placeholder Tests (25Q4 Feature)
# Tests for template tags with secret_map placeholders and pod override behavior

def test_create_template_for_secrets(headers):
    """Create a template to hold secret_map placeholder tags."""
    template_def = {
        "template_id": test_template_secrets,
        "description": "Template for testing secret_map placeholders",
        "metatags": ["test", "secrets", "placeholders"],
    }
    rsp = client.post("/pods/templates", data=json.dumps(template_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['template_id'] == test_template_secrets
    time.sleep(2)


def test_add_template_tag_with_default_placeholder(headers):
    """Template tag with ${pods:default:value:?description} placeholder should succeed."""
    tag_def = {
        "pod_definition": {
            "image": "postgres:15",
            "secret_map": {
                "DB_HOST": "${pods:default:localhost:?Database hostname}",
                "DB_PORT": "${pods:default:5432:?Database port number}"
            },
            "environment_variables": {
                "POSTGRES_HOST": "${pods:secrets:DB_HOST}",
                "POSTGRES_PORT": "${pods:secrets:DB_PORT}"
            }
        },
        "tag": test_template_tag_secrets,
        "commit_message": "Template with default placeholder secrets"
    }
    rsp = client.post(f"/pods/templates/{test_template_secrets}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert test_template_tag_secrets in result['tag_timestamp']
    
    # Check metadata contains placeholder warnings
    response_data = rsp.json()
    assert 'metadata' in response_data
    if response_data['metadata']:
        metadata = response_data['metadata']
        if 'secret_placeholders' in metadata:
            placeholders = metadata['secret_placeholders']
            # Should have 2 placeholders
            assert len(placeholders) >= 2
            env_vars = [p['env_var'] for p in placeholders]
            assert 'DB_HOST' in env_vars
            assert 'DB_PORT' in env_vars


def test_add_template_tag_with_required_placeholder(headers):
    """Template tag with ${:?description} required placeholder should succeed."""
    tag_def = {
        "pod_definition": {
            "image": "postgres:15",
            "secret_map": {
                "DB_PASSWORD": "${:?Database password - required, no default}",
                "API_KEY": "${:?External API key}"
            },
            "environment_variables": {
                "POSTGRES_PASSWORD": "${pods:secrets:DB_PASSWORD}",
                "EXTERNAL_API_KEY": "${pods:secrets:API_KEY}"
            }
        },
        "tag": test_template_tag_secrets_required,
        "commit_message": "Template with required placeholder secrets"
    }
    rsp = client.post(f"/pods/templates/{test_template_secrets}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert test_template_tag_secrets_required in result['tag_timestamp']
    
    # Check metadata contains placeholder warnings with has_default=False
    response_data = rsp.json()
    if response_data.get('metadata') and response_data['metadata'].get('secret_placeholders'):
        placeholders = response_data['metadata']['secret_placeholders']
        for p in placeholders:
            # Required placeholders should have has_default=False
            assert p['has_default'] is False


def test_add_template_tag_with_direct_secret_ref_fails(headers):
    """Template tag with ${secret:name} direct reference should FAIL validation."""
    tag_def = {
        "pod_definition": {
            "image": "postgres:15",
            "secret_map": {
                "DB_PASSWORD": "${secret:mydbsecret}"  # Direct secret ref - NOT allowed in templates
            }
        },
        "tag": test_template_tag_secrets_invalid,
        "commit_message": "This should fail - direct secret reference"
    }
    rsp = client.post(f"/pods/templates/{test_template_secrets}/tags", data=json.dumps(tag_def), headers=headers)
    
    # Should return 400 error
    print("error print: ", rsp.status_code, rsp.text)
    assert rsp.status_code == 400
    data = rsp.json()
    error_msg = data.get('message', '').lower()
    assert "cannot contain direct secret references" in error_msg or "template" in error_msg


def test_add_template_tag_with_explicit_secret_ref_fails(headers):
    """Template tag with ${secret:user:name} explicit reference should FAIL."""
    tag_def = {
        "pod_definition": {
            "image": "postgres:15",
            "secret_map": {
                "API_KEY": "${secret:someuser:myapikey}"  # Explicit secret ref - NOT allowed
            }
        },
        "tag": "shouldfail",
        "commit_message": "This should fail - explicit secret reference"
    }
    rsp = client.post(f"/pods/templates/{test_template_secrets}/tags", data=json.dumps(tag_def), headers=headers)
    
    # Should return 400 error
    print("error print: ", rsp.status_code, rsp.text)
    assert rsp.status_code == 400
    data = rsp.json()
    error_msg = data.get('message', '').lower()
    assert "cannot contain direct secret references" in error_msg or "template" in error_msg


def test_add_template_tag_with_invalid_env_var_ref_fails(headers):
    """Template with environment_variables referencing non-existent secret_map key should fail."""
    tag_def = {
        "pod_definition": {
            "image": "postgres:15",
            "secret_map": {
                "DB_HOST": "${pods:default:localhost:?Database host}"
            },
            "environment_variables": {
                "DATABASE_URL": "postgres://${pods:secrets:MISSING_KEY}@host/db"  # MISSING_KEY not in secret_map
            }
        },
        "tag": "shouldfailenvref",
        "commit_message": "This should fail - env var refs missing key"
    }
    rsp = client.post(f"/pods/templates/{test_template_secrets}/tags", data=json.dumps(tag_def), headers=headers)
    
    # Should return 400 error
    print("error print: ", rsp.status_code, rsp.text)
    assert rsp.status_code == 400
    data = rsp.json()
    error_msg = data.get('message', '').lower()
    assert "missing_key" in error_msg or "does not exist" in error_msg


def test_get_template_tag_with_placeholders(headers):
    """Get template tag and verify pod_definition contains secret_map."""
    rsp = client.get(f"/pods/templates/{test_template_secrets}/tags", headers=headers)
    result = basic_response_checks(rsp)
    
    # Find the tag with secrets
    found = False
    for tag in result:
        if test_template_tag_secrets in tag.get('tag_timestamp', ''):
            found = True
            pod_def = tag.get('pod_definition', {})
            assert 'secret_map' in pod_def
            assert 'DB_HOST' in pod_def['secret_map']
            break
    assert found, f"Tag {test_template_tag_secrets} not found"


def test_create_pod_from_template_with_placeholders(headers):
    """Create pod from template with placeholders - should work with defaults."""
    pod_def = {
        "pod_id": test_pod_secrets_template,
        "template": f"{test_template_secrets}:{test_template_tag_secrets}"
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['pod_id'] == test_pod_secrets_template
    # Pod should inherit template's secret_map with defaults applied
    # The exact behavior depends on implementation


def test_create_pod_with_placeholder_override(headers):
    """Create pod from template and override placeholder with actual secret."""
    pod_def = {
        "pod_id": test_pod_secrets_override,
        "template": f"{test_template_secrets}:{test_template_tag_secrets}",
        "secret_map": {
            "DB_HOST": "production.db.example.com"  # Override the default
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['pod_id'] == test_pod_secrets_override


def test_get_derived_pod_shows_merged_secrets(headers):
    """Get derived pod should show merged secret_map (template + pod overrides)."""
    rsp = client.get(f"/pods/{test_pod_secrets_override}/derived?include_configs=true", headers=headers)
    result = basic_response_checks(rsp)
    
    # The derived pod should have the merged secret_map
    # DB_HOST should be overridden, DB_PORT should use template default
    if 'secret_map' in result:
        secret_map = result['secret_map']
        # Check that override took effect
        if 'DB_HOST' in secret_map:
            assert secret_map['DB_HOST'] == "production.db.example.com"


##### Template Overrides Integration Tests (25Q4 Feature)
# Tests for template_overrides field on pods - allows partial override of template volume_mounts and secret_map


def test_create_template_for_overrides(headers):
    """Create a template to test template_overrides functionality."""
    template_def = {
        "template_id": test_template_overrides,
        "description": "Template for testing template_overrides feature",
        "metatags": ["test", "overrides", "volume_mounts", "secret_map"],
    }
    rsp = client.post("/pods/templates", data=json.dumps(template_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['template_id'] == test_template_overrides
    time.sleep(2)


def test_add_template_tag_with_volume_mounts_and_secrets(headers):
    """Create template tag with complex volume_mounts and secret_map for override testing."""
    tag_def = {
        "pod_definition": {
            "image": "postgres:15",
            "description": "Template with volume_mounts and secret_map for override testing",
            "volume_mounts": {
                "/data": {
                    "type": "ephemeral",
                    "config_content": "# Data directory placeholder",
                    "config_permissions": "0755"
                },
                "/config": {
                    "type": "ephemeral",
                    "config_content": "template_config=true\nkey=template_value",
                    "config_permissions": "0644",
                    "config_filename": "app.conf"
                },
                "/logs": {
                    "type": "ephemeral",
                    "config_content": "# Logs directory placeholder",
                    "config_permissions": "0755"
                }
            },
            "secret_map": {
                "DB_HOST": "${pods:default:localhost:?Database hostname}",
                "DB_PORT": "${pods:default:5432:?Database port}",
                "DB_PASSWORD": "${:?Database password - required}",
                "API_KEY": "${pods:default:test-key:?API key}"
            },
            "environment_variables": {
                "POSTGRES_HOST": "${pods:secrets:DB_HOST}",
                "POSTGRES_PORT": "${pods:secrets:DB_PORT}",
                "POSTGRES_PASSWORD": "${pods:secrets:DB_PASSWORD}"
            },
            "networking": {
                "default": {
                    "port": 5432,
                    "protocol": "postgres"
                }
            }
        },
        "tag": test_template_tag_overrides,
        "commit_message": "Template with volume_mounts and secret_map for override testing"
    }
    rsp = client.post(f"/pods/templates/{test_template_overrides}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert test_template_tag_overrides in result['tag_timestamp']


def test_create_pod_with_volume_mount_override(headers):
    """Create pod with template_overrides to override volume_mount config fields."""
    pod_def = {
        "pod_id": test_pod_overrides_vm,
        "template": f"{test_template_overrides}:{test_template_tag_overrides}",
        "template_overrides": {
            "volume_mounts": {
                "/data": {"config_content": "# Custom data config", "config_permissions": "0700"},
                "/config": {"config_content": "custom_config=true\nkey=custom_value", "config_permissions": "0600"}
            }
        },
        "secret_map": {
            "DB_PASSWORD": "testpassword123"
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['pod_id'] == test_pod_overrides_vm
    assert test_template_overrides in result['template']


def test_get_derived_pod_with_volume_mount_override(headers):
    """Verify derived pod has merged volume_mounts with overrides applied."""
    rsp = client.get(f"/pods/{test_pod_overrides_vm}/derived?include_configs=true", headers=headers)
    result = basic_response_checks(rsp)
    
    # Check volume_mounts have overrides applied
    assert 'volume_mounts' in result
    vm = result['volume_mounts']
    
    # /data - config_content and config_permissions overridden
    assert '/data' in vm
    assert vm['/data']['config_content'] == "# Custom data config"
    assert vm['/data']['config_permissions'] == "0700"
    assert vm['/data']['type'] == "ephemeral"  # Preserved from template
    
    # /config - config_content and config_permissions overridden
    assert '/config' in vm
    assert vm['/config']['config_content'] == "custom_config=true\nkey=custom_value"
    assert vm['/config']['config_permissions'] == "0600"
    assert vm['/config']['type'] == "ephemeral"  # Preserved from template
    
    # /logs - unchanged from template
    assert '/logs' in vm
    assert vm['/logs']['config_content'] == "# Logs directory placeholder"
    assert vm['/logs']['type'] == "ephemeral"


def test_create_pod_with_secret_map_override(headers):
    """Create pod with template_overrides to override secret_map values."""
    pod_def = {
        "pod_id": test_pod_overrides_sm,
        "template": f"{test_template_overrides}:{test_template_tag_overrides}",
        "template_overrides": {
            "secret_map": {
                "DB_HOST": "production.db.example.com",
                "API_KEY": "my-custom-api-key"
            }
        },
        "secret_map": {
            "DB_PASSWORD": "testpassword456"
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['pod_id'] == test_pod_overrides_sm
    assert test_template_overrides in result['template']


def test_get_derived_pod_with_secret_map_override(headers):
    """Verify derived pod has merged secret_map with overrides applied."""
    rsp = client.get(f"/pods/{test_pod_overrides_sm}/derived", headers=headers)
    result = basic_response_checks(rsp)
    
    # Check secret_map has overrides applied
    if 'secret_map' in result:
        sm = result['secret_map']
        
        # DB_HOST overridden via template_overrides
        if 'DB_HOST' in sm:
            assert sm['DB_HOST'] == "production.db.example.com"
        
        # API_KEY overridden via template_overrides
        if 'API_KEY' in sm:
            assert sm['API_KEY'] == "my-custom-api-key"
        
        # DB_PORT should have template default value
        if 'DB_PORT' in sm:
            # Either the default value or the placeholder
            assert sm['DB_PORT'] in ["5432", "${pods:default:5432:?Database port}"]


def test_create_pod_with_both_overrides(headers):
    """Create pod overriding both volume_mounts and secret_map via template_overrides."""
    pod_def = {
        "pod_id": test_pod_overrides_both,
        "template": f"{test_template_overrides}:{test_template_tag_overrides}",
        "template_overrides": {
            "volume_mounts": {
                "/data": {"config_content": "# Combined data override", "config_permissions": "0750"},
                "/logs": {"config_content": "# Combined logs override"}
            },
            "secret_map": {
                "DB_HOST": "combined.db.example.com",
                "DB_PORT": "5433"
            }
        },
        "secret_map": {
            "DB_PASSWORD": "combinedpassword789"
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['pod_id'] == test_pod_overrides_both
    assert test_template_overrides in result['template']


def test_get_derived_pod_with_both_overrides(headers):
    """Verify derived pod has both volume_mounts and secret_map overrides applied."""
    rsp = client.get(f"/pods/{test_pod_overrides_both}/derived?include_configs=true", headers=headers)
    result = basic_response_checks(rsp)
    
    # Check volume_mounts
    assert 'volume_mounts' in result
    vm = result['volume_mounts']
    
    # /data - config fields overridden
    assert '/data' in vm
    assert vm['/data']['config_content'] == "# Combined data override"
    assert vm['/data']['config_permissions'] == "0750"
    assert vm['/data']['type'] == "ephemeral"
    
    # /logs - config_content overridden
    assert '/logs' in vm
    assert vm['/logs']['config_content'] == "# Combined logs override"
    
    # /config - unchanged
    assert '/config' in vm
    assert vm['/config']['type'] == "ephemeral"
    assert vm['/config']['config_content'] == "template_config=true\nkey=template_value"
    
    # Check secret_map
    if 'secret_map' in result:
        sm = result['secret_map']
        if 'DB_HOST' in sm:
            assert sm['DB_HOST'] == "combined.db.example.com"
        if 'DB_PORT' in sm:
            assert sm['DB_PORT'] == "5433"

