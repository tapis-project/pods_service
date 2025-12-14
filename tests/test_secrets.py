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
test_secret_readonly = f"testsecretro_{test_timestamp}"  # writable=False (write-once)
test_secret_writeonly = f"testsecretwo_{test_timestamp}"  # readable=False
test_secret_locked = f"testsecretlocked_{test_timestamp}"  # readable=False, writable=False
test_secret_full = f"testsecretfull_{test_timestamp}"  # readable=True, writable=True (default)
test_secret_error = f"testsecreterror_{test_timestamp}"
test_pod_with_secrets = f"testpodwithsecrets{test_timestamp}"
test_pod_legacy_secrets = f"testpodlegacysecrets{test_timestamp}"


##### Teardown
@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    """Clean up all test secrets and pods after tests complete."""
    yield None
    
    time.sleep(3) # have to wait for last pod to actually get created before deletion

    # Clean up pods
    pods_to_delete = [
        test_pod_with_secrets,
        test_pod_legacy_secrets,
        f"testpodplaceholders{test_timestamp}",
        f"testpodnoplacehold{test_timestamp}",
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
    ]
    for secret_id in secrets_to_delete:
        try:
            client.delete(f'/pods/secrets/{secret_id}', headers=headers)
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
        "secret_map": {
            "REQUIRED_KEY": "${:?This is a required secret}",
            "OPTIONAL_KEY": "${default:fallback_value:?This is optional}"
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


