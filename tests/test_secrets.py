"""
Tests for the Pods Secrets API endpoints.

Tests cover:
- CRUD operations for secrets (list, create, get, get_value, update, delete)
- Permission enforcement (READ, USER, ADMIN levels)
- Scope validation (user vs pod scope)
- Secret name validation and error cases
- secret_map format validation
- Integration with pods via secret_map and environment_variables
- Template secret_map resolution (verified via exec)
"""
import os
import sys
import json
import time
import pytest
from datetime import datetime

from tests.test_utils import (
    headers, response_format, basic_response_checks,
    regular_headers, privileged_headers,
    exec_command, verify_env_var, verify_file_content, wait_for_pod_status
)

# Allows us to import pods's modules.
sys.path.append('/home/tapis/service')
from api import api

# Set up client for testing
from fastapi.testclient import TestClient

# base_url: The base URL to use for requests, must be valid Tapis URL.
# raise_server_exceptions: If True, the client will raise exceptions from the server rather the normal client errors.
client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)


# Set up test variables with unique timestamps to avoid collisions
test_timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
test_secret_1 = f"testsecret1_{test_timestamp}"
test_secret_2 = f"testsecret2_{test_timestamp}"
test_secret_pod_scope = f"testsecretpod_{test_timestamp}"
test_secret_readonly = f"testsecretro_{test_timestamp}"  # writable=False (write-once)
test_secret_writeonly = f"testsecretwo_{test_timestamp}"  # readable=False
test_secret_locked = f"testsecretlocked_{test_timestamp}"  # readable=False, writable=False
test_secret_full = f"testsecretfull_{test_timestamp}"  # readable=True, writable=True (default)
test_secret_error = f"testsecreterror_{test_timestamp}"
test_pod_with_secrets = f"testpodwithsecrets{test_timestamp}"
test_pod_legacy_secrets = f"testpodlegacysecrets{test_timestamp}"

# Template secret resolution tests - variables
test_template_secret_id = f"testtmplsecret{test_timestamp}"
test_template_for_secrets = f"testsecrettmpl{test_timestamp}"
test_pod_from_template = f"testpodfromtmpl{test_timestamp}"
test_pod_direct_secrets = f"testpoddirect{test_timestamp}"

# Description syntax tests - variables
test_pod_description_syntax = f"testpoddesc{test_timestamp}"

# Syntax warning tests - variables (testing ? vs :? detection)
test_pod_syntax_warning = f"testpodsyntaxwarn{test_timestamp}"


##### Teardown
@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    """Clean up all test secrets and pods after tests complete."""
    yield None
    
    time.sleep(3) # have to wait for last pod to actually get created before deletion

    # Clean up pods
    pods_to_delete = [
        test_pod_with_secrets,
        f"testpodplaceholders{test_timestamp}",
        f"testpodnoplacehold{test_timestamp}",
        test_pod_from_template,
        test_pod_direct_secrets,
        test_pod_legacy_secrets,
        test_pod_description_syntax,
        test_pod_syntax_warning,
    ]
    for pod_id in pods_to_delete:
        try:
            rsp = client.delete(f'/pods/{pod_id}', headers=headers)
            if rsp.status_code not in (200, 404):
                print(f"Warning: Failed to delete pod {pod_id}: {rsp.status_code} - {rsp.text}")
        except Exception:
            print(f"Warning: Exception deleting pod {pod_id}: {e}")
            pass
    
    # Clean up secrets
    secrets_to_delete = [
        test_secret_1,
        test_secret_2,
        test_secret_pod_scope,
        test_secret_readonly,
        test_secret_writeonly,
        test_secret_locked,
        test_secret_full,
        test_secret_error,
        f"testsecretresolved_{test_timestamp}",
        test_template_secret_id,
    ]
    for secret_id in secrets_to_delete:
        try:
            client.delete(f'/pods/secrets/{secret_id}', headers=headers)
        except Exception:
            pass

    # Clean up templates
    templates_to_delete = [
        test_template_for_secrets,
    ]
    for template_id in templates_to_delete:
        try:
            client.delete(f'/pods/templates/{template_id}', headers=headers)
        except Exception:
            pass


##### Testing Secrets CRUD Operations

def test_list_secrets_empty(headers):
    """Test listing secrets returns a valid response (may be empty or have existing secrets)."""
    rsp = client.get("/pods/secrets", headers=headers)
    result = basic_response_checks(rsp)
    assert result is not None
    assert isinstance(result, list)


def test_create_secret(headers):
    """Test creating a new secret with user scope."""
    secret_def = {
        "secret_id": test_secret_1,
        "secret_value": "my_super_secret_password_123",
        "description": "Test secret for integration tests",
        "scope": "user",
        "readable": True,
        "writable": True
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    # Verify response contains expected fields
    assert result['secret_id'] == test_secret_1
    assert result['description'] == "Test secret for integration tests"
    assert result['scope'] == "user"
    assert result['readable'] == True
    assert result['writable'] == True
    assert 'sk_secret_name' in result
    assert 'creation_ts' in result
    assert 'added_by' in result
    # Secret value should NOT be returned
    assert 'secret_value' not in result


def test_create_secret_minimal(headers):
    """Test creating a secret with minimal required fields."""
    secret_def = {
        "secret_id": test_secret_2,
        "secret_value": "another_secret_value"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['secret_id'] == test_secret_2
    assert result['scope'] == "user"  # Default
    assert result['readable'] == True  # Default
    assert result['writable'] == True  # Default


def test_create_secret_duplicate_conflict(headers):
    """Test that creating a secret with an existing name returns 409 Conflict."""
    secret_def = {
        "secret_id": test_secret_1,
        "secret_value": "attempt_to_recreate"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    
    # Should return 409 Conflict
    assert rsp.status_code == 409
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "already exists" in error_msg
    assert "put" in error_msg  # Should suggest using PUT


def test_create_secret_duplicate_by_other_user_conflict(privileged_headers, headers):
    """Test that a different user cannot create a secret with the same name (409 Conflict)."""
    # test_secret_1 was created by the regular user (headers)
    # Try to create the same secret with privileged_headers (different user)
    secret_def = {
        "secret_id": test_secret_1,
        "secret_value": "attempt_by_other_user"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=privileged_headers)
    
    # Should return 409 Conflict (secret exists, regardless of who owns it)
    assert rsp.status_code == 409
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "already exists" in error_msg


def test_list_secrets_after_create(headers):
    """Test that created secrets appear in the list."""
    rsp = client.get("/pods/secrets", headers=headers)
    result = basic_response_checks(rsp)
    
    secret_ids = [s['secret_id'] for s in result]
    assert test_secret_1 in secret_ids
    assert test_secret_2 in secret_ids


def test_get_secret_before_update(headers):
    """Test getting secret metadata before any updates (original values)."""
    rsp = client.get(f"/pods/secrets/{test_secret_1}", headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['secret_id'] == test_secret_1
    assert result['description'] == "Test secret for integration tests"
    # Secret value should NOT be returned
    assert 'secret_value' not in result


def test_get_secret_not_found(headers):
    """Test getting a non-existent secret returns an error."""
    rsp = client.get("/pods/secrets/nonexistent_secret_xyz", headers=headers)
    assert rsp.status_code == 404
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "not found" in error_msg


def test_get_secret_value_before_update(headers):
    """Test getting the actual secret value before any updates (original value)."""
    rsp = client.get(f"/pods/secrets/{test_secret_1}/value", headers=headers)
    result = basic_response_checks(rsp)
    
    assert 'secret_value' in result
    assert result['secret_value'] == "my_super_secret_password_123"


def test_update_secret_description(headers):
    """Test updating a secret's description."""
    update_def = {
        "description": "Updated description for test secret"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_1}", data=json.dumps(update_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['description'] == "Updated description for test secret"


def test_update_secret_value(headers):
    """Test updating a secret's value."""
    update_def = {
        "secret_value": "new_secret_value_456"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_1}", data=json.dumps(update_def), headers=headers)
    result = basic_response_checks(rsp)
    
    # Verify the value was updated by fetching it
    rsp = client.get(f"/pods/secrets/{test_secret_1}/value", headers=headers)
    result = basic_response_checks(rsp)
    assert result['secret_value'] == "new_secret_value_456"


def test_update_secret_value_and_description_together(headers):
    """Test updating both value and description in a single PUT request."""
    update_def = {
        "secret_value": "combined_update_value",
        "description": "Combined update description"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_1}", data=json.dumps(update_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['description'] == "Combined update description"
    
    # Verify the value was updated
    rsp = client.get(f"/pods/secrets/{test_secret_1}/value", headers=headers)
    result = basic_response_checks(rsp)
    assert result['secret_value'] == "combined_update_value"


def test_get_secret_after_update(headers):
    """Test getting secret metadata after updates (updated values)."""
    rsp = client.get(f"/pods/secrets/{test_secret_1}", headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['secret_id'] == test_secret_1
    assert result['description'] == "Combined update description"
    # Secret value should NOT be returned
    assert 'secret_value' not in result


def test_get_secret_value_after_update(headers):
    """Test getting the actual secret value after updates (updated value)."""
    rsp = client.get(f"/pods/secrets/{test_secret_1}/value", headers=headers)
    result = basic_response_checks(rsp)
    
    assert 'secret_value' in result
    assert result['secret_value'] == "combined_update_value"


def test_delete_secret(headers):
    """Test deleting a secret."""
    rsp = client.delete(f"/pods/secrets/{test_secret_2}", headers=headers)
    result = basic_response_checks(rsp)
    
    assert result == test_secret_2 or test_secret_2 in str(result)
    
    # Verify it's gone
    rsp = client.get(f"/pods/secrets/{test_secret_2}", headers=headers)
    assert rsp.status_code == 404
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "not found" in error_msg


##### Testing Secret Scopes

def test_create_secret_pod_scope(headers):
    """Test creating a secret with pod scope."""
    secret_def = {
        "secret_id": test_secret_pod_scope,
        "secret_value": "pod_scoped_secret",
        "scope": "pod",
        "pod_id": "mypod123"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['secret_id'] == test_secret_pod_scope
    assert result['scope'] == "pod"
    assert result['pod_id'] == "mypod123"


def test_create_secret_pod_scope_missing_pod_id_error(headers):
    """Test that pod scope without pod_id returns an error."""
    secret_def = {
        "secret_id": test_secret_error,
        "secret_value": "will_fail",
        "scope": "pod"
        # pod_id intentionally missing
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    data = response_format(rsp)
    
    assert rsp.status_code == 400
    assert "pod_id" in str(data['message']).lower()


def test_create_secret_user_scope_with_pod_id_error(headers):
    """Test that user scope with pod_id returns an error."""
    secret_def = {
        "secret_id": test_secret_error,
        "secret_value": "will_fail",
        "scope": "user",
        "pod_id": "shouldnt_be_here"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    data = response_format(rsp)
    
    assert rsp.status_code == 400
    assert "pod_id" in str(data['message']).lower()


##### Testing Write-Once Secrets (writable=False)

def test_create_writeonce_secret(headers):
    """Test creating a write-once secret (writable=False)."""
    secret_def = {
        "secret_id": test_secret_readonly,
        "secret_value": "writeonce_value",
        "writable": False
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['writable'] == False
    assert result['readable'] == True  # Default


def test_update_writeonce_secret_value_error(headers):
    """Test that updating a write-once secret's value returns 403."""
    update_def = {
        "secret_value": "try_to_change"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_readonly}", data=json.dumps(update_def), headers=headers)
    
    # Should fail because secret is write-once (writable=False)
    assert rsp.status_code == 403
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "writable=false" in error_msg or "write-once" in error_msg


def test_update_writeonce_secret_description_ok(headers):
    """Test that updating a write-once secret's description is allowed."""
    update_def = {
        "description": "Description can still be updated for write-once secrets"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_readonly}", data=json.dumps(update_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['description'] == "Description can still be updated for write-once secrets"


def test_recreate_writeonce_secret_conflict(headers):
    """Test that re-POSTing a write-once secret returns 409 Conflict (POST is create-only)."""
    secret_def = {
        "secret_id": test_secret_readonly,
        "secret_value": "try_to_recreate_value"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    
    # Should return 409 Conflict (POST is create-only, use PUT to update)
    assert rsp.status_code == 409
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "already exists" in error_msg


def test_get_writeonce_secret_value_ok(headers):
    """Test that getting a write-once secret's value is allowed (readable=True by default)."""
    rsp = client.get(f"/pods/secrets/{test_secret_readonly}/value", headers=headers)
    result = basic_response_checks(rsp)
    
    assert 'secret_value' in result
    assert result['secret_value'] == "writeonce_value"


##### Testing Write-Only Secrets (readable=False)

def test_create_writeonly_secret(headers):
    """Test creating a write-only secret (readable=False)."""
    secret_def = {
        "secret_id": test_secret_writeonly,
        "secret_value": "writeonly_value",
        "readable": False,
        "writable": True
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['readable'] == False
    assert result['writable'] == True


def test_get_writeonly_secret_value_error(headers):
    """Test that getting a write-only secret's value returns 403."""
    rsp = client.get(f"/pods/secrets/{test_secret_writeonly}/value", headers=headers)
    
    # Should fail because secret is write-only (readable=False)
    assert rsp.status_code == 403
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "readable=false" in error_msg
    assert "secret_map" in error_msg  # Should mention pod injection still works


def test_update_writeonly_secret_value_ok(headers):
    """Test that updating a write-only secret's value is allowed (writable=True)."""
    update_def = {
        "secret_value": "updated_writeonly_value"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_writeonly}", data=json.dumps(update_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['secret_id'] == test_secret_writeonly


def test_recreate_writeonly_secret_conflict(headers):
    """Test that re-POSTing a write-only secret returns 409 Conflict (POST is create-only)."""
    secret_def = {
        "secret_id": test_secret_writeonly,
        "secret_value": "recreated_writeonly_value"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    
    # Should return 409 Conflict (POST is create-only, use PUT to update)
    assert rsp.status_code == 409
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "already exists" in error_msg


##### Testing Locked Secrets (readable=False, writable=False)

def test_create_locked_secret(headers):
    """Test creating a locked secret (readable=False, writable=False)."""
    secret_def = {
        "secret_id": test_secret_locked,
        "secret_value": "locked_value",
        "readable": False,
        "writable": False
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['readable'] == False
    assert result['writable'] == False


def test_get_locked_secret_value_error(headers):
    """Test that getting a locked secret's value returns 403."""
    rsp = client.get(f"/pods/secrets/{test_secret_locked}/value", headers=headers)
    
    assert rsp.status_code == 403
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "readable=false" in error_msg


def test_update_locked_secret_value_error(headers):
    """Test that updating a locked secret's value returns 403."""
    update_def = {
        "secret_value": "try_to_change_locked"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_locked}", data=json.dumps(update_def), headers=headers)
    
    assert rsp.status_code == 403
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "writable=false" in error_msg


def test_update_locked_secret_description_ok(headers):
    """Test that updating a locked secret's description is still allowed."""
    update_def = {
        "description": "Description updates always work"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_locked}", data=json.dumps(update_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['description'] == "Description updates always work"


def test_recreate_locked_secret_conflict(headers):
    """Test that re-POSTing a locked secret returns 409 Conflict (POST is create-only)."""
    secret_def = {
        "secret_id": test_secret_locked,
        "secret_value": "try_to_recreate_locked"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    
    # Should return 409 Conflict (POST is create-only)
    assert rsp.status_code == 409


##### Testing Full Access Secrets (readable=True, writable=True - default)

def test_create_full_access_secret(headers):
    """Test creating a full access secret (readable=True, writable=True)."""
    secret_def = {
        "secret_id": test_secret_full,
        "secret_value": "full_access_value",
        "readable": True,
        "writable": True,
        "description": "Full access secret"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['readable'] == True
    assert result['writable'] == True
    assert result['secret_id'] == test_secret_full


def test_get_full_access_secret_value_ok(headers):
    """Test that getting a full access secret's value works."""
    rsp = client.get(f"/pods/secrets/{test_secret_full}/value", headers=headers)
    result = basic_response_checks(rsp)
    
    assert 'secret_value' in result
    assert result['secret_value'] == "full_access_value"


def test_update_full_access_secret_value_ok(headers):
    """Test that updating a full access secret's value works."""
    update_def = {
        "secret_value": "updated_full_access_value"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_full}", data=json.dumps(update_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['secret_id'] == test_secret_full


def test_recreate_full_access_secret_conflict(headers):
    """Test that re-POSTing a full access secret returns 409 Conflict (POST is create-only)."""
    secret_def = {
        "secret_id": test_secret_full,
        "secret_value": "recreated_full_access_value",
        "description": "Recreated description"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    
    # Should return 409 Conflict (POST is create-only, use PUT to update)
    assert rsp.status_code == 409
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "already exists" in error_msg


##### Testing Secret Name Validation

def test_create_secret_invalid_name_spaces(headers):
    """Test that secret names with spaces are rejected."""
    secret_def = {
        "secret_id": "invalid name with spaces",
        "secret_value": "value"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    data = response_format(rsp)
    
    assert rsp.status_code == 400
    assert any("space" in msg.lower() or "alphanumeric" in msg.lower() for msg in (data['message'] if isinstance(data['message'], list) else [data['message']]))


def test_create_secret_invalid_name_special_chars(headers):
    """Test that secret names with special characters are rejected."""
    secret_def = {
        "secret_id": "invalid@name!",
        "secret_value": "value"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    data = response_format(rsp)
    
    assert rsp.status_code == 400


def test_create_secret_valid_name_with_underscore_dash(headers):
    """Test that secret names with underscores and dashes are allowed."""
    valid_name = f"valid_name-with-dash_{test_timestamp}"
    secret_def = {
        "secret_id": valid_name,
        "secret_value": "value"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['secret_id'] == valid_name
    
    # Cleanup
    client.delete(f"/pods/secrets/{valid_name}", headers=headers)


def test_create_secret_id_too_long(headers):
    """Test that secret names over 110 characters are rejected."""
    long_name = "a" * 111
    secret_def = {
        "secret_id": long_name,
        "secret_value": "value"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    
    assert rsp.status_code == 400 or rsp.status_code == 422  # Validation error


##### Testing Invalid scope values

def test_create_secret_invalid_scope(headers):
    """Test that invalid scope values are rejected."""
    secret_def = {
        "secret_id": test_secret_error,
        "secret_value": "value",
        "scope": "invalid_scope"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    data = response_format(rsp)
    
    assert rsp.status_code == 400


def test_create_secret_readable_writable_booleans(headers):
    """Test that readable and writable accept boolean values."""
    # Test with explicit True values
    secret_def = {
        "secret_id": f"bool_test_{test_timestamp}",
        "secret_value": "value",
        "readable": True,
        "writable": True
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['readable'] == True
    assert result['writable'] == True
    
    # Cleanup
    client.delete(f"/pods/secrets/bool_test_{test_timestamp}", headers=headers)


##### Testing Description Validation

def test_create_secret_description_non_ascii(headers):
    """Test that non-ASCII descriptions are rejected."""
    secret_def = {
        "secret_id": test_secret_error,
        "secret_value": "value",
        "description": "café résumé"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    data = response_format(rsp)
    
    assert rsp.status_code == 400
    assert any("ascii" in msg.lower() for msg in (data['message'] if isinstance(data['message'], list) else [data['message']]))


def test_create_secret_description_too_long(headers):
    """Test that descriptions over 500 characters are rejected."""
    long_desc = "a" * 501
    secret_def = {
        "secret_id": test_secret_error,
        "secret_value": "value",
        "description": long_desc
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    data = response_format(rsp)
    
    assert rsp.status_code == 400
    assert any("500" in msg or "character" in msg.lower() for msg in (data['message'] if isinstance(data['message'], list) else [data['message']]))


##### Testing Secrets with Pods (Integration)

def test_create_pod_with_secret_map(headers):
    """Test creating a pod with secret_map referencing an existing secret."""
    # First ensure we have a secret to reference
    integration_secret = f"pod_integration_secret_{test_timestamp}"
    secret_def = {
        "secret_id": integration_secret,
        "secret_value": "integration_test_value"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    # May already exist or update, that's ok - just ensure it's there
    
    # Create pod with secret_map
    pod_def = {
        "pod_id": test_pod_with_secrets,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with secrets",
        "secret_map": {
            "DB_PASSWORD": f"${{secret:{integration_secret}}}"
        },
        "environment_variables": {
            "DATABASE_URL": "postgres://user:${pods:secrets:DB_PASSWORD}@localhost/db"
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    
    # If pod creation succeeded, verify secret_map is in result
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_with_secrets
    assert 'secret_map' in result
    assert 'DB_PASSWORD' in result['secret_map']
    
    # Cleanup the integration secret
    client.delete(f"/pods/secrets/{integration_secret}", headers=headers)


def test_pod_secret_map_validation(headers):
    """Test that secret_map keys are validated for alphanumeric format."""
    pod_def = {
        "pod_id": f"testpodvalidation_{test_timestamp}",
        "image": "notchristiangarcia/testserver:fastapi",
        "secret_map": {
            "invalid key with spaces": "some_value"
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    data = response_format(rsp)
    
    assert rsp.status_code == 400
    assert any("alphanumeric" in msg.lower() or "secret_map" in msg.lower() 
               for msg in (data['message'] if isinstance(data['message'], list) else [data['message']]))


##### Testing Pod Creation Placeholder Metadata

# These tests verify that pod creation returns helpful metadata about unresolved placeholders
# in the secret_map, informing users about required/optional placeholders they can override.

test_pod_placeholders = f"testpodplaceholders{test_timestamp}"

def test_pod_creation_returns_placeholder_metadata(headers):
    """Test that creating a pod with placeholders returns available_placeholders metadata."""
    pod_def = {
        "pod_id": test_pod_placeholders,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with placeholders in secret_map",
        "status_requested": "OFF",  # Don't start - just create to see placeholder metadata
        "secret_map": {
            "REQUIRED_KEY": "${:?This is a required secret}",
            "OPTIONAL_KEY": "${pods:default:fallback_value:?This is optional}"
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    
    data = rsp.json()
    assert rsp.status_code == 200, f"Pod creation failed: {data}"
    
    # Check metadata contains placeholder info
    metadata = data.get('metadata', {})
    assert 'available_placeholders' in metadata, f"Expected available_placeholders in metadata: {metadata}"
    
    placeholders = metadata['available_placeholders']
    assert 'required' in placeholders
    assert 'optional' in placeholders
    
    # Check required placeholder - now a simple string list
    assert len(placeholders['required']) == 1
    required_str = placeholders['required'][0]
    assert "REQUIRED_KEY" in required_str
    assert "This is a required secret" in required_str
    assert "REQUIRED:" in required_str
    
    # Check optional placeholder - now a simple string list
    assert len(placeholders['optional']) == 1
    optional_str = placeholders['optional'][0]
    assert "OPTIONAL_KEY" in optional_str
    assert "This is optional" in optional_str
    assert "Default: 'fallback_value'" in optional_str
    
    # Clean up
    client.delete(f"/pods/{test_pod_placeholders}", headers=headers)


test_pod_no_placeholders = f"testpodnoplacehold{test_timestamp}"

def test_pod_creation_no_placeholder_metadata_when_resolved(headers):
    """Test that pods with actual secrets (not placeholders) don't return placeholder metadata."""
    # First create a secret to reference
    secret_id = f"testsecretresolved_{test_timestamp}"
    secret_def = {
        "secret_id": secret_id,
        "secret_value": "actual_secret_value"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    
    # Create pod with resolved secret reference
    pod_def = {
        "pod_id": test_pod_no_placeholders,
        "image": "notchristiangarcia/testserver:fastapi",
        "secret_map": {
            "DB_PASSWORD": f"${{secret:{secret_id}}}"
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    
    data = rsp.json()
    assert rsp.status_code == 200, f"Pod creation failed: {data}"
    
    # Metadata should be empty or not contain available_placeholders
    metadata = data.get('metadata', {})
    available = metadata.get('available_placeholders', {})
    
    # If available_placeholders exists, it should have empty required/optional lists
    if available:
        assert len(available.get('required', [])) == 0, f"Expected no required placeholders: {available}"
        assert len(available.get('optional', [])) == 0, f"Expected no optional placeholders: {available}"
    
    # Clean up
    client.delete(f"/pods/{test_pod_no_placeholders}", headers=headers)
    client.delete(f"/pods/secrets/{secret_id}", headers=headers)


##### Testing Legacy <<tapissecret_...>> Replacement

# These tests verify that the legacy <<tapissecret_user_username>> and <<tapissecret_user_password>>
# placeholders in environment_variables get replaced with values from the Password table at pod start.
# This feature will be replaced at some point, just can't deprecate fully yet.

def test_create_pod_with_tapissecret_placeholders(headers):
    """Test creating a pod with <<tapissecret_...>> placeholders in environment_variables."""
    pod_def = {
        "pod_id": test_pod_legacy_secrets,
        "image": "notchristiangarcia/testserver:fastapi",
        "description": "Test pod with legacy tapissecret placeholders",
        "environment_variables": {
            "MY_USERNAME": "<<tapissecret_user_username>>",
            "MY_PASSWORD": "<<tapissecret_user_password>>",
            "ADMIN_USER": "<<tapissecret_admin_username>>",
            "COMBINED": "user:<<tapissecret_user_username>>:pass:<<tapissecret_user_password>>"
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_legacy_secrets
    # The placeholders should still be present in the stored pod (replaced at start time)
    assert "<<tapissecret_user_username>>" in result['environment_variables'].get('MY_USERNAME', '')


def test_get_derived_pod_replaces_tapissecret(headers):
    """Test that GET /pods/{pod_id}/derived replaces <<tapissecret_...>> placeholders."""
    rsp = client.get(f"/pods/{test_pod_legacy_secrets}/derived", headers=headers)
        
    result = basic_response_checks(rsp)
    
    # In the derived pod, tapissecret placeholders should be replaced with actual values
    env_vars = result.get('environment_variables', {})
    
    # MY_USERNAME should be replaced with pod_id (user_username = pod_id)
    my_username = env_vars.get('MY_USERNAME', '')
    assert "<<tapissecret_" not in my_username, f"Placeholder not replaced in MY_USERNAME: {my_username}"
    assert my_username == test_pod_legacy_secrets, f"Expected pod_id as username, got: {my_username}"
    
    # MY_PASSWORD should be replaced with a generated password (30 chars)
    my_password = env_vars.get('MY_PASSWORD', '')
    assert "<<tapissecret_" not in my_password, f"Placeholder not replaced in MY_PASSWORD: {my_password}"
    assert len(my_password) == 30, f"Expected 30-char password, got length {len(my_password)}"
    
    # ADMIN_USER should be replaced with "podsservice"
    admin_user = env_vars.get('ADMIN_USER', '')
    assert "<<tapissecret_" not in admin_user, f"Placeholder not replaced in ADMIN_USER: {admin_user}"
    assert admin_user == "podsservice", f"Expected 'podsservice' for admin username, got: {admin_user}"
    
    # COMBINED should have both placeholders replaced
    combined = env_vars.get('COMBINED', '')
    assert "<<tapissecret_" not in combined, f"Placeholders not replaced in COMBINED: {combined}"
    assert f"user:{test_pod_legacy_secrets}:pass:" in combined


##### Template Secret Resolution Tests #####
# These tests verify that secrets defined in templates are properly resolved
# when pods use those templates. The bug was that template secret_map entries
# were merged AFTER resolution, so they never got resolved.

def test_template_secret_create_secret(headers):
    """Create a secret to be used for template secret resolution tests."""
    secret_def = {
        "secret_id": test_template_secret_id,
        "secret_value": "template_secret_value_12345",
        "description": "Secret for template resolution tests",
        "scope": "user",
        "readable": True,
        "writable": True
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['secret_id'] == test_template_secret_id


def test_template_secret_create_template(headers):
    """Create a template for testing secret resolution from templates."""
    # Step 1: Create the template (just metadata)
    template_def = {
        "template_id": test_template_for_secrets,
        "description": "Template for testing secret resolution from templates",
        "metatags": ["test", "secrets", "resolution"]
    }
    rsp = client.post("/pods/templates", data=json.dumps(template_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['template_id'] == test_template_for_secrets
    time.sleep(1)


def test_template_secret_add_tag(headers):
    """Add a tag to the template with secret_map placeholders that pod creators will override."""
    # Step 2: Add a tag with the pod_definition
    # Templates define PLACEHOLDERS in secret_map, not direct secret references
    # Pod creators override these placeholders with their actual secrets
    # Format: ${:?description} for required placeholder
    tag_def = {
        "pod_definition": {
            "image": "notchristiangarcia/testserver:fastapi",
            "description": "Template tag with secret_map placeholders",
            "command": [],
            "environment_variables": {
                "HOST": "${pods:secrets:DB_HOST}",
                "PORT": "5432",
                "TEMPLATE_VAR": "from_template"
            },
            "volume_mounts": {
                "/etc/myapp/config.ini": {
                    "type": "ephemeral",
                    "config_content": "[database]\nhost = ${pods:secrets:DB_HOST}\nport = 5432\n"
                }
            },
            "secret_map": {
                "DB_HOST": "${:?Database host secret - provide your secret reference}"
            },
            "resources": {
                "cpu_request": 250,
                "mem_request": 256,
                "cpu_limit": 1000,
                "mem_limit": 1024
            }
        },
        "tag": "secrettest",
        "commit_message": "Template tag with secret_map placeholders for resolution testing"
    }
    rsp = client.post(f"/pods/templates/{test_template_for_secrets}/tags", data=json.dumps(tag_def), headers=headers)
    result = basic_response_checks(rsp)
    assert "secrettest" in result['tag_timestamp']


def test_template_secret_create_pod_from_template(headers):
    """Create a pod using the template, overriding the secret_map placeholder with actual secret."""
    # The template has a placeholder for DB_HOST - we override it with our actual secret
    pod_def = {
        "pod_id": test_pod_from_template,
        "description": "Pod from template for secret resolution test",
        "template": f"{test_template_for_secrets}:secrettest",
        "environment_variables": {
            "POD_VAR": "from_pod"
        },
        "secret_map": {
            # Override the template's DB_HOST placeholder with our actual secret reference
            "DB_HOST": "${secret:" + test_template_secret_id + "}"
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_from_template
    assert test_template_for_secrets in result['template']
    # Verify secret_map was merged (pod's override should be present)
    assert 'secret_map' in result
    assert 'DB_HOST' in result['secret_map']


def test_template_secret_derived_shows_resolved(headers):
    """Verify that GET /pods/{id}/derived with resolve_secrets=true shows resolved secrets."""
    # Wait briefly for pod to be created
    time.sleep(2)
    
    rsp = client.get(
        f'/pods/{test_pod_from_template}/derived?resolve_secrets=true&include_configs=true',
        headers=headers
    )
    result = basic_response_checks(rsp)
    
    # Check environment_variables have secrets resolved
    env_vars = result.get('environment_variables', {})
    host_value = env_vars.get('HOST', '')
    
    # Should NOT contain the placeholder syntax
    assert "${pods:secrets:" not in host_value, f"Secret not resolved in HOST: {host_value}"
    # Should contain the actual secret value
    assert host_value == "template_secret_value_12345", f"HOST should be secret value, got: {host_value}"
    
    # Check volume_mounts config_content has secrets resolved
    volume_mounts = result.get('volume_mounts', {})
    config_mount = volume_mounts.get('/etc/myapp/config.ini', {})
    config_content = config_mount.get('config_content', '')
    assert "${pods:secrets:" not in config_content, f"Secret not resolved in config_content: {config_content}"
    assert "template_secret_value_12345" in config_content, f"Secret value not in config_content: {config_content}"


def test_template_secret_wait_for_pod_running(headers):
    """Wait for the pod from template to reach RUNNING status."""
    success, result = wait_for_pod_status(client, test_pod_from_template, "RUNNING", headers, max_attempts=30)
    if not success:
        pytest.fail(f"Pod did not reach Running status: {result}")


def test_template_secret_exec_verify_env_vars(headers):
    """Use exec to verify actual environment variable values inside the running pod."""
    # Verify HOST contains actual secret value, not placeholder
    passed, actual, error = verify_env_var(client, test_pod_from_template, "HOST", "template_secret_value_12345", headers)
    assert passed, f"HOST verification failed: {error}. Actual: '{actual}'"
    assert "${pods:secrets:" not in actual, f"HOST still has unresolved placeholder: {actual}"
    
    # Verify PORT is also present (from template)
    passed, actual, error = verify_env_var(client, test_pod_from_template, "PORT", "5432", headers)
    assert passed, f"PORT verification failed: {error}. Actual: '{actual}'"
    
    # Verify TEMPLATE_VAR from template
    passed, actual, error = verify_env_var(client, test_pod_from_template, "TEMPLATE_VAR", "from_template", headers)
    assert passed, f"TEMPLATE_VAR verification failed: {error}. Actual: '{actual}'"


def test_template_secret_exec_verify_config_content(headers):
    """Use exec to cat the config file and verify secret is resolved in the actual file."""
    # Verify config file contains actual secret value
    passed, content, error = verify_file_content(client, test_pod_from_template, "/etc/myapp/config.ini", "template_secret_value_12345", headers)
    assert passed, f"Config verification failed: {error}. Content: '{content}'"
    
    # Verify config file does NOT contain placeholder
    assert "${pods:secrets:" not in content, f"Config file still has unresolved placeholder: {content}"
    
    # Verify the structure is correct
    assert "[database]" in content, f"Config missing [database] section: {content}"
    assert "port = 5432" in content, f"Config missing port value: {content}"


def test_template_secret_stop_pod_from_template(headers):
    """Stop the pod from template to clean up."""
    rsp = client.get(f'/pods/{test_pod_from_template}/stop', headers=headers)
    # Either success or already stopped is fine
    assert rsp.status_code in [200, 400, 404]


##### Direct Secret Map Pod Tests (Comparison) #####
# These tests verify that secrets defined directly on pods (not via templates) also work

def test_direct_secret_create_pod_with_secret_map(headers):
    """Create a pod with secret_map defined directly on the pod (no template)."""
    # secret_map maps a KEY to a secret reference ${secret:secret_id}
    # environment_variables use ${pods:secrets:KEY} to get the resolved value
    pod_def = {
        "pod_id": test_pod_direct_secrets,
        "description": "Pod with direct secret_map for comparison",
        "image": "notchristiangarcia/testserver:fastapi",
        "environment_variables": {
            "DIRECT_SECRET": "${pods:secrets:MY_SECRET}",
            "NORMAL_VAR": "normal_value"
        },
        "secret_map": {
            "MY_SECRET": "${secret:" + test_template_secret_id + "}"
        },
        "resources": {
            "cpu_request": 250,
            "mem_request": 256,
            "cpu_limit": 1000,
            "mem_limit": 1024
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_direct_secrets


def test_direct_secret_derived_shows_resolved(headers):
    """Verify that direct secret_map also resolves correctly in /derived."""
    time.sleep(2)
    
    rsp = client.get(
        f'/pods/{test_pod_direct_secrets}/derived?resolve_secrets=true',
        headers=headers
    )
    result = basic_response_checks(rsp)
    
    env_vars = result.get('environment_variables', {})
    direct_secret = env_vars.get('DIRECT_SECRET', '')
    
    assert "${pods:secrets:" not in direct_secret, f"Secret not resolved: {direct_secret}"
    assert direct_secret == "template_secret_value_12345", f"Wrong value: {direct_secret}"


def test_direct_secret_wait_for_pod_running(headers):
    """Wait for the direct secret pod to reach RUNNING status."""
    success, result = wait_for_pod_status(client, test_pod_direct_secrets, "RUNNING", headers, max_attempts=30)
    if not success:
        pytest.fail(f"Pod did not reach Running status: {result}")


def test_direct_secret_exec_verify_env_vars(headers):
    """Use exec to verify environment variables in the direct secret pod."""
    # Verify DIRECT_SECRET contains actual secret value
    passed, actual, error = verify_env_var(client, test_pod_direct_secrets, "DIRECT_SECRET", "template_secret_value_12345", headers)
    assert passed, f"DIRECT_SECRET verification failed: {error}. Actual: '{actual}'"
    
    # Verify NORMAL_VAR is also present
    passed, actual, error = verify_env_var(client, test_pod_direct_secrets, "NORMAL_VAR", "normal_value", headers)
    assert passed, f"NORMAL_VAR verification failed: {error}. Actual: '{actual}'"


def test_direct_secret_stop_pod(headers):
    """Stop the direct secret pod to clean up."""
    rsp = client.get(f'/pods/{test_pod_direct_secrets}/stop', headers=headers)
    assert rsp.status_code in [200, 400, 404]


##### Description Syntax Tests (${pods:secrets:KEY:?description}) #####
# These tests verify that the :?description suffix on ${pods:secrets:KEY} references
# works correctly - descriptions are informational only and stripped during interpolation.

def test_description_syntax_create_pod(headers):
    """Create a pod using ${pods:secrets:KEY:?description} syntax in env vars and config."""
    pod_def = {
        "pod_id": test_pod_description_syntax,
        "description": "Pod testing :?description syntax in secret references",
        "image": "notchristiangarcia/testserver:fastapi",
        "environment_variables": {
            # Using :?description syntax - description should be stripped at runtime
            "DB_HOST": "${pods:secrets:HOST_SECRET:?Database hostname}",
            "DB_PORT": "${pods:secrets:PORT_SECRET:?Database port number}",
            # Mix of with and without description
            "DB_USER": "${pods:secrets:USER_SECRET}",
            # Inline with description
            "DB_URL": "postgres://${pods:secrets:USER_SECRET:?Username}:${pods:secrets:PASS_SECRET:?Password}@${pods:secrets:HOST_SECRET:?Host}/mydb",
            "NORMAL_VAR": "no_secrets_here"
        },
        "volume_mounts": {
            "/etc/app/db.ini": {
                "type": "ephemeral",
                "config_content": "[database]\nhost = ${pods:secrets:HOST_SECRET:?Database server hostname}\nport = ${pods:secrets:PORT_SECRET:?Port number}\nuser = ${pods:secrets:USER_SECRET}\npassword = ${pods:secrets:PASS_SECRET:?Database password - keep secret}\n"
            }
        },
        "secret_map": {
            "HOST_SECRET": "${secret:" + test_template_secret_id + "}",
            "PORT_SECRET": "5432",  # Literal value
            "USER_SECRET": "testuser",  # Literal value
            "PASS_SECRET": "supersecretpass123"
        },
        "resources": {
            "cpu_request": 250,
            "mem_request": 256,
            "cpu_limit": 1000,
            "mem_limit": 1024
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_description_syntax
    # Verify secret_map is stored
    assert 'secret_map' in result
    assert 'HOST_SECRET' in result['secret_map']


def test_description_syntax_derived_resolves_correctly(headers):
    """Verify /derived endpoint strips descriptions and resolves secrets."""
    time.sleep(2)
    
    rsp = client.get(
        f'/pods/{test_pod_description_syntax}/derived?resolve_secrets=true&include_configs=true',
        headers=headers
    )
    result = basic_response_checks(rsp)
    
    env_vars = result.get('environment_variables', {})
    
    # DB_HOST should be resolved, description stripped
    db_host = env_vars.get('DB_HOST', '')
    assert db_host == "template_secret_value_12345", f"DB_HOST wrong value: {db_host}"
    assert ":?" not in db_host, f"Description not stripped from DB_HOST: {db_host}"
    assert "Database hostname" not in db_host, f"Description text in DB_HOST: {db_host}"
    
    # DB_PORT should be resolved (literal value)
    db_port = env_vars.get('DB_PORT', '')
    assert db_port == "5432", f"DB_PORT wrong value: {db_port}"
    assert ":?" not in db_port, f"Description not stripped from DB_PORT: {db_port}"
    
    # DB_USER should be resolved (no description in original)
    db_user = env_vars.get('DB_USER', '')
    assert db_user == "testuser", f"DB_USER wrong value: {db_user}"
    
    # DB_URL should have all secrets resolved, all descriptions stripped
    db_url = env_vars.get('DB_URL', '')
    expected_url = "postgres://testuser:supersecretpass123@template_secret_value_12345/mydb"
    assert db_url == expected_url, f"DB_URL wrong value: {db_url}, expected: {expected_url}"
    assert ":?" not in db_url, f"Description not stripped from DB_URL: {db_url}"
    assert "${pods:secrets:" not in db_url, f"Placeholder not resolved in DB_URL: {db_url}"
    
    # Check config_content in volume_mounts
    volume_mounts = result.get('volume_mounts', {})
    config_mount = volume_mounts.get('/etc/app/db.ini', {})
    config_content = config_mount.get('config_content', '')
    
    # Config should have all secrets resolved, all descriptions stripped
    assert "template_secret_value_12345" in config_content, f"HOST not resolved in config: {config_content}"
    assert "5432" in config_content, f"PORT not in config: {config_content}"
    assert "testuser" in config_content, f"USER not in config: {config_content}"
    assert "supersecretpass123" in config_content, f"PASS not resolved in config: {config_content}"
    assert ":?" not in config_content, f"Description not stripped from config: {config_content}"
    assert "${pods:secrets:" not in config_content, f"Placeholder not resolved in config: {config_content}"
    assert "Database server hostname" not in config_content, f"Description text in config: {config_content}"


def test_description_syntax_wait_for_pod_running(headers):
    """Wait for the description syntax test pod to reach RUNNING status."""
    success, result = wait_for_pod_status(client, test_pod_description_syntax, "RUNNING", headers, max_attempts=30)
    if not success:
        pytest.fail(f"Pod did not reach Running status: {result}")


def test_description_syntax_exec_verify_env_vars(headers):
    """Use exec to verify environment variables have descriptions stripped in running pod."""
    # Verify DB_HOST contains actual secret value, no description
    passed, actual, error = verify_env_var(client, test_pod_description_syntax, "DB_HOST", "template_secret_value_12345", headers)
    assert passed, f"DB_HOST verification failed: {error}. Actual: '{actual}'"
    assert ":?" not in actual, f"Description not stripped in DB_HOST: {actual}"
    
    # Verify DB_PORT
    passed, actual, error = verify_env_var(client, test_pod_description_syntax, "DB_PORT", "5432", headers)
    assert passed, f"DB_PORT verification failed: {error}. Actual: '{actual}'"
    
    # Verify DB_USER
    passed, actual, error = verify_env_var(client, test_pod_description_syntax, "DB_USER", "testuser", headers)
    assert passed, f"DB_USER verification failed: {error}. Actual: '{actual}'"
    
    # Verify DB_URL has all parts resolved and no descriptions
    expected_url = "postgres://testuser:supersecretpass123@template_secret_value_12345/mydb"
    passed, actual, error = verify_env_var(client, test_pod_description_syntax, "DB_URL", expected_url, headers)
    assert passed, f"DB_URL verification failed: {error}. Actual: '{actual}'"
    assert ":?" not in actual, f"Description not stripped in DB_URL: {actual}"
    assert "${pods:secrets:" not in actual, f"Placeholder not resolved in DB_URL: {actual}"


def test_description_syntax_exec_verify_config_content(headers):
    """Use exec to cat the config file and verify descriptions are stripped."""
    # Verify config file contains resolved values
    passed, content, error = verify_file_content(
        client, test_pod_description_syntax, "/etc/app/db.ini", 
        "template_secret_value_12345", headers
    )
    assert passed, f"Config HOST verification failed: {error}. Content: '{content}'"
    
    # Verify other values are present
    assert "5432" in content, f"PORT not in config: {content}"
    assert "testuser" in content, f"USER not in config: {content}"
    assert "supersecretpass123" in content, f"PASS not in config: {content}"
    
    # Verify no descriptions remain
    assert ":?" not in content, f"Description marker still in config: {content}"
    assert "${pods:secrets:" not in content, f"Placeholder not resolved in config: {content}"
    assert "Database server hostname" not in content, f"Description text in config: {content}"
    assert "keep secret" not in content, f"Description text 'keep secret' in config: {content}"
    
    # Verify structure is correct
    assert "[database]" in content, f"Config missing [database] section: {content}"


def test_description_syntax_stop_pod(headers):
    """Stop the description syntax test pod to clean up."""
    rsp = client.get(f'/pods/{test_pod_description_syntax}/stop', headers=headers)
    assert rsp.status_code in [200, 400, 404]


##### Syntax Warning Tests (${pods:secrets:KEY?desc} vs ${pods:secrets:KEY:?desc}) #####
# These tests verify that the system detects and warns about the common mistake of using
# ${pods:secrets:KEY?description} instead of the correct ${pods:secrets:KEY:?description}.
# The incorrect syntax (missing colon) won't match and won't resolve, so warnings help users.

def test_syntax_warning_create_pod_with_wrong_syntax(headers):
    """Create a pod using incorrect ? syntax (should still create but warn)."""
    # Using WRONG syntax: ${pods:secrets:KEY?desc} instead of ${pods:secrets:KEY:?desc}
    # This tests that the system detects this common mistake
    pod_def = {
        "pod_id": test_pod_syntax_warning,
        "description": "Pod testing syntax warning for ? vs :?",
        "image": "notchristiangarcia/testserver:fastapi",
        "environment_variables": {
            # WRONG syntax - ? instead of :? - this won't resolve!
            "WRONG_SYNTAX": "${pods:secrets:BAD_KEY?This description wont work}",
            # CORRECT syntax - :? - this WILL resolve
            "CORRECT_SYNTAX": "${pods:secrets:GOOD_KEY:?This description works}",
            # Normal var for comparison
            "NORMAL_VAR": "just_a_normal_value"
        },
        "volume_mounts": {
            "/etc/app/test.conf": {
                "type": "ephemeral",
                "config_content": "# Wrong syntax in config\nwrong_key = ${pods:secrets:CONFIG_BAD?wrong desc}\n# Correct syntax\ncorrect_key = ${pods:secrets:CONFIG_GOOD:?correct desc}\n"
            }
        },
        "secret_map": {
            "BAD_KEY": "bad_value_wont_resolve",
            "GOOD_KEY": "good_value_will_resolve",
            "CONFIG_BAD": "config_bad_value",
            "CONFIG_GOOD": "config_good_value"
        },
        "resources": {
            "cpu_request": 250,
            "mem_request": 256,
            "cpu_limit": 1000,
            "mem_limit": 1024
        }
    }
    rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['pod_id'] == test_pod_syntax_warning


def test_syntax_warning_wait_for_pod_running(headers):
    """Wait for the syntax warning test pod to reach RUNNING status."""
    success, result = wait_for_pod_status(client, test_pod_syntax_warning, "RUNNING", headers, max_attempts=30)
    if not success:
        pytest.fail(f"Pod did not reach Running status: {result}")


def test_syntax_warning_correct_syntax_resolves(headers):
    """Verify that correct :? syntax resolves properly in the running pod."""
    # The CORRECT_SYNTAX env var should have the secret resolved and description stripped
    passed, actual, error = verify_env_var(client, test_pod_syntax_warning, "CORRECT_SYNTAX", "good_value_will_resolve", headers)
    assert passed, f"CORRECT_SYNTAX verification failed: {error}. Actual: '{actual}'"
    assert ":?" not in actual, f"Description not stripped from CORRECT_SYNTAX: {actual}"
    assert "${pods:secrets:" not in actual, f"Placeholder not resolved in CORRECT_SYNTAX: {actual}"


def test_syntax_warning_wrong_syntax_not_resolved(headers):
    """Verify that wrong ? syntax does NOT resolve (remains as literal)."""
    # The WRONG_SYNTAX env var should NOT be resolved because ? doesn't match the pattern
    # It will remain as the literal string since the pattern didn't match
    passed, actual, error = verify_env_var(client, test_pod_syntax_warning, "WRONG_SYNTAX", "${pods:secrets:BAD_KEY?This description wont work}", headers)
    assert passed, f"WRONG_SYNTAX should remain unresolved: {error}. Actual: '{actual}'"
    # The placeholder should still be there because ? syntax doesn't match
    assert "${pods:secrets:" in actual, f"Expected unresolved placeholder in WRONG_SYNTAX, got: {actual}"
    assert "?" in actual, f"Expected ? to remain (wrong syntax), got: {actual}"


def test_syntax_warning_config_correct_syntax_resolves(headers):
    """Verify correct :? syntax resolves in config file."""
    # Check that correct_key line has resolved value
    passed, content, error = verify_file_content(
        client, test_pod_syntax_warning, "/etc/app/test.conf",
        "config_good_value", headers
    )
    assert passed, f"Config correct syntax verification failed: {error}. Content: '{content}'"
    assert ":?" not in content or "${pods:secrets:CONFIG_GOOD:?" not in content, \
        f"Description not stripped from correct syntax in config: {content}"


def test_syntax_warning_config_wrong_syntax_not_resolved(headers):
    """Verify wrong ? syntax does NOT resolve in config file."""
    # Check that wrong_key line still has the unresolved placeholder
    passed, content, error = verify_file_content(
        client, test_pod_syntax_warning, "/etc/app/test.conf",
        "${pods:secrets:CONFIG_BAD?wrong desc}", headers
    )
    assert passed, f"Config wrong syntax should remain unresolved: {error}. Content: '{content}'"
    # The wrong syntax placeholder should still be in the file
    assert "${pods:secrets:CONFIG_BAD?" in content, \
        f"Expected unresolved wrong syntax in config, got: {content}"


def test_syntax_warning_stop_pod(headers):
    """Stop the syntax warning test pod to clean up."""
    rsp = client.get(f'/pods/{test_pod_syntax_warning}/stop', headers=headers)
    assert rsp.status_code in [200, 400, 404]

