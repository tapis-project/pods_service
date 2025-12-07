"""
Tests for the Pods Secrets API endpoints.

Tests cover:
- CRUD operations for secrets (list, create, get, get_value, update, delete)
- Permission enforcement (READ, USER, ADMIN levels)
- Scope validation (user vs pod scope)
- Secret name validation and error cases
- secret_map format validation
- Integration with pods via secret_map and environment_variables
"""
import os
import sys
import json
import time
import pytest
from datetime import datetime

from tests.test_utils import (
    headers, response_format, basic_response_checks,
    regular_headers, privileged_headers
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
test_secret_readonly = f"testsecretro_{test_timestamp}"
test_secret_error = f"testsecreterror_{test_timestamp}"
test_pod_with_secrets = f"testpodwithsecrets{test_timestamp}"


##### Teardown
@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    """Clean up all test secrets and pods after tests complete."""
    yield None
    # Clean up secrets
    secrets_to_delete = [
        test_secret_1,
        test_secret_2,
        test_secret_pod_scope,
        test_secret_readonly,
        test_secret_error,
    ]
    for secret_id in secrets_to_delete:
        try:
            rsp = client.delete(f'/pods/secrets/{secret_id}', headers=headers)
            # Ignore errors during cleanup
        except Exception:
            pass
    
    # Clean up pod if created
    try:
        rsp = client.delete(f'/pods/{test_pod_with_secrets}', headers=headers)
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
        "read_write": "read_write"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    # Verify response contains expected fields
    assert result['secret_id'] == test_secret_1
    assert result['description'] == "Test secret for integration tests"
    assert result['scope'] == "user"
    assert result['read_write'] == "read_write"
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
    assert result['read_write'] == "read_write"  # Default


def test_create_secret_duplicate_by_author_allowed(headers):
    """Test that the same author can re-create a secret (updates the value)."""
    secret_def = {
        "secret_id": test_secret_1,
        "secret_value": "updated_value_version_1"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    # Should succeed and update the secret
    assert result['secret_id'] == test_secret_1
    assert "updated" in rsp.json()['message'].lower()


def test_create_secret_duplicate_by_author_multiple_times(headers):
    """Test that the same author can re-create a secret multiple times."""
    # Update again
    secret_def = {
        "secret_id": test_secret_1,
        "secret_value": "value_version_2"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['secret_id'] == test_secret_1
    
    # Update again
    secret_def["secret_value"] = "value_version_3"
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    assert result['secret_id'] == test_secret_1


def test_create_secret_duplicate_by_other_user_error(privileged_headers, headers):
    """Test that a different user cannot create a secret with the same name."""
    # test_secret_1 was created by the regular user (headers)
    # Try to create the same secret with privileged_headers (different user)
    secret_def = {
        "secret_id": test_secret_1,
        "secret_value": "attempt_by_other_user"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=privileged_headers)
    
    # Should return 400 error with helpful message
    assert rsp.status_code == 400
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "already in use by another user" in error_msg or "another user" in error_msg


def test_list_secrets_after_create(headers):
    """Test that created secrets appear in the list."""
    rsp = client.get("/pods/secrets", headers=headers)
    result = basic_response_checks(rsp)
    
    secret_ids = [s['secret_id'] for s in result]
    assert test_secret_1 in secret_ids
    assert test_secret_2 in secret_ids


def test_get_secret(headers):
    """Test getting secret metadata (not the value)."""
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


def test_get_secret_value(headers):
    """Test getting the actual secret value (always returns latest version)."""
    rsp = client.get(f"/pods/secrets/{test_secret_1}/value", headers=headers)
    result = basic_response_checks(rsp)
    
    assert 'secret_value' in result
    # After multiple re-creates, should return the latest value (version 3)
    assert result['secret_value'] == "value_version_3"


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


##### Testing Read-Only Secrets

def test_create_readonly_secret(headers):
    """Test creating a read-only secret."""
    secret_def = {
        "secret_id": test_secret_readonly,
        "secret_value": "readonly_value",
        "read_write": "read"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['read_write'] == "read"


def test_update_readonly_secret_value_error(headers):
    """Test that updating a read-only secret's value returns an error."""
    update_def = {
        "secret_value": "try_to_change"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_readonly}", data=json.dumps(update_def), headers=headers)
    
    # Should fail because secret is read-only
    assert rsp.status_code == 400
    data = rsp.json()
    error_msg = data.get('detail', data.get('message', '')).lower()
    assert "read-only" in error_msg or "cannot" in error_msg


def test_update_readonly_secret_description_ok(headers):
    """Test that updating a read-only secret's description is allowed."""
    update_def = {
        "description": "Description can still be updated"
    }
    rsp = client.put(f"/pods/secrets/{test_secret_readonly}", data=json.dumps(update_def), headers=headers)
    result = basic_response_checks(rsp)
    
    assert result['description'] == "Description can still be updated"


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


##### Testing Invalid read_write and scope values

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


def test_create_secret_invalid_read_write(headers):
    """Test that invalid read_write values are rejected."""
    secret_def = {
        "secret_id": test_secret_error,
        "secret_value": "value",
        "read_write": "invalid_mode"
    }
    rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
    data = response_format(rsp)
    
    assert rsp.status_code == 400


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


##### Testing Legacy <<tapissecret_...>> Replacement

# These tests verify that the legacy <<tapissecret_user_username>> and <<tapissecret_user_password>>
# placeholders in environment_variables get replaced with values from the Password table at pod start.
# This feature will be replaced at some point, just can't deprecate fully yet.

test_pod_legacy_secrets = f"testpodlegacysecrets{test_timestamp}"

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


def test_cleanup_legacy_secrets_pod(headers):
    """Cleanup the legacy secrets test pod."""
    rsp = client.delete(f"/pods/{test_pod_legacy_secrets}", headers=headers)
    # Don't assert success, pod may already be deleted or never created


##### Cleanup test - runs last

def test_final_cleanup(headers):
    """Final cleanup of test secret 1 and pod scope secret."""
    # Delete remaining test secrets
    for secret_id in [test_secret_1, test_secret_pod_scope, test_secret_readonly]:
        rsp = client.delete(f"/pods/secrets/{secret_id}", headers=headers)
        # Just ensure the call was made, don't assert success (may already be deleted)
    
    # Delete test pod
    rsp = client.delete(f"/pods/{test_pod_with_secrets}", headers=headers)
    
    # Verify secrets are gone
    rsp = client.get("/pods/secrets", headers=headers)
    result = basic_response_checks(rsp)
    secret_ids = [s['secret_id'] for s in result]
    
    # Our test secrets should be gone
    assert test_secret_1 not in secret_ids
    assert test_secret_2 not in secret_ids
