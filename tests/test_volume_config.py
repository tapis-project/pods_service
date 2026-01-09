"""
Tests for object-based volume_mounts system with ephemeral and tapisvolume+config support.

Tests cover:
1. VolumeMount dict structure validation (required fields, types)
2. Ephemeral volume mounts (config_content, config_permissions)
3. VolumeMount config fields (config_content, config_filename, config_permissions, config_update_mode)
4. Template volume_mounts merge logic (object-based with null removal)
5. tapisvolume with config_content (for NFS config file writing)
"""

import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock

# Allows us to import pods's modules.
sys.path.append('/home/tapis/service')
from api import api

# Import test utilities like test_pods.py
from tests.test_utils import headers, response_format, basic_response_checks, regular_headers

# Import volume_mounts utilities at module level
from models_volume_mounts_utils import (
    VolumeMount,
    validate_volume_mount_entry,
    validate_mount_path,
    validate_volume_mounts_dict,
    validate_permissions_format,
    validate_volume_mounts_permissions,
    interpolate_config_content,
    merge_pod_volume_mounts_with_template,
    MAX_CONFIG_CONTENT_SIZE,
)

# Set up client for testing
from fastapi.testclient import TestClient
client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

import time
from datetime import datetime

# Test IDs
test_pod_ephemeral = "testpodephconfig"
test_pod_tapisvol = "testpodvolconfig"
test_volume_config = "testvolforconfig"
test_template_vm = "testtemplatevolmounts"

# Secret map integration test IDs (timestamped to avoid conflicts)
test_timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
test_secret_for_env = f"testsecretenv{test_timestamp}"
test_secret_for_config = f"testsecretcfg{test_timestamp}"
test_pod_secret_env = f"testpodsecretenv{test_timestamp}"
test_pod_secret_config = f"testpodsecretcfg{test_timestamp}"
test_pod_secret_both = f"testpodsecretboth{test_timestamp}"


# ============================================================================
# Teardown
# ============================================================================

@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    """Delete all test objects after tests complete."""
    yield None
    
    pods = [test_pod_ephemeral, test_pod_tapisvol, "testpodephonce", "testpodvolonce",
            "testpodtmpleph", "testpodoverride", "testpodremove",
            "testpodrandompass", "testpodrandommulti", "testpodnetref", "testpodnetcfg",
            test_pod_secret_env, test_pod_secret_config, test_pod_secret_both]
    volumes = [test_volume_config]
    templates = [test_template_vm]
    secrets = [test_secret_for_env, test_secret_for_config]
    
    for pod_id in pods:
        client.delete(f'/pods/{pod_id}', headers=headers)
    for vol_id in volumes:
        client.delete(f'/pods/volumes/{vol_id}', headers=headers)
    for template_id in templates:
        client.delete(f'/pods/templates/{template_id}', headers=headers)
    
    time.sleep(3)  # Wait for pods to be deleted before deleting secrets
    for secret_id in secrets:
        client.delete(f'/pods/secrets/{secret_id}', headers=headers)


# ============================================================================
# Unit Tests: VolumeMount Validation
# ============================================================================

class TestVolumeMountValidation:
    """Tests for volume_mounts validation functions."""
    
    @pytest.mark.parametrize("vol_type,config", [
        ("tapisvolume", {"type": "tapisvolume", "source_id": "vol1"}),
        ("tapissnapshot", {"type": "tapissnapshot", "source_id": "snap1"}),
        ("ephemeral", {"type": "ephemeral", "config_content": "test"}),
        ("pvc", {"type": "pvc", "source_id": "pvc1"}),
    ])
    def test_valid_types(self, vol_type, config):
        """All valid types should pass validation."""
        result = validate_volume_mount_entry(f"/mnt/{vol_type}", config)
        assert result is not None
        assert result.get("type") == vol_type
    
    def test_none_value_removes_mount(self):
        """None value should be valid (removes inherited mount)."""
        result = validate_volume_mount_entry("/mnt/removed", None)
        assert result is None
    
    @pytest.mark.parametrize("config,error_substr", [
        ({"source_id": "vol1"}, "must specify a 'type'"),
        ({"type": "invalid_type", "source_id": "vol1"}, "Invalid volume mount type"),
        ({"type": "ephemeral"}, "must specify 'config_content'"),
        ({"type": "ephemeral", "config_content": "test", "source_id": "vol"}, "should not have 'source_id'"),
        ({"type": "tapisvolume"}, "must specify 'source_id'"),
    ])
    def test_invalid_configs(self, config, error_substr):
        """Invalid configurations should fail with expected error."""
        with pytest.raises(ValueError) as exc_info:
            validate_volume_mount_entry("/mnt/data", config)
        assert error_substr in str(exc_info.value)
    
    @pytest.mark.parametrize("path,error_substr", [
        ("mnt/data", "must be an absolute path"),
        ("/mnt/../etc/passwd", "cannot contain '..'"),
    ])
    def test_invalid_mount_paths(self, path, error_substr):
        """Invalid mount paths should fail."""
        with pytest.raises(ValueError) as exc_info:
            validate_mount_path(path)
        assert error_substr in str(exc_info.value)
    
    def test_empty_and_none_dicts_valid(self):
        """Empty dict and None should be valid."""
        assert validate_volume_mounts_dict({}) == {}
        assert validate_volume_mounts_dict(None) == {}
    
    def test_multiple_mounts_valid(self):
        """Multiple valid mounts should pass."""
        mounts = {
            "/mnt/data": {"type": "tapisvolume", "source_id": "vol1"},
            "/etc/config.ini": {"type": "ephemeral", "config_content": "test"},
            "/snapshots": {"type": "tapissnapshot", "source_id": "snap1"},
        }
        result = validate_volume_mounts_dict(mounts)
        assert len(result) == 3
    
    def test_list_input_fails(self):
        """List input should raise error."""
        with pytest.raises(ValueError) as exc_info:
            validate_volume_mounts_dict([{"type": "tapisvolume"}])
        assert "must be a dict" in str(exc_info.value)


class TestVolumeMountConfigFields:
    """Tests for VolumeMount class config fields."""
    
    def test_ephemeral_with_all_fields(self):
        """Ephemeral mount with all config fields."""
        mount = VolumeMount(
            type="ephemeral",
            config_content="[database]\nhost=localhost",
            config_permissions="0600",
            config_update_mode="once"
        )
        assert mount.config_content == "[database]\nhost=localhost"
        assert mount.config_permissions == "0600"
        assert mount.config_update_mode == "once"
    
    def test_tapisvolume_with_config(self):
        """tapisvolume with config_content and config_filename."""
        mount = VolumeMount(
            type="tapisvolume",
            source_id="vol1",
            config_content="key=value",
            config_filename="app.conf"
        )
        assert mount.config_content == "key=value"
        assert mount.config_filename == "app.conf"
        assert mount.config_permissions == "0644"  # Default
    
    @pytest.mark.parametrize("mode", ["always", "ALWAYS", "once", "Once"])
    def test_config_update_mode_case_insensitive(self, mode):
        """config_update_mode should be case insensitive."""
        mount = VolumeMount(type="ephemeral", config_content="test", config_update_mode=mode)
        assert mount.config_update_mode == mode.lower()
    
    def test_config_update_mode_invalid(self):
        """Invalid config_update_mode should fail."""
        with pytest.raises(ValueError) as exc_info:
            VolumeMount(type="ephemeral", config_content="test", config_update_mode="invalid")
        assert "config_update_mode must be one of" in str(exc_info.value)
    
    def test_config_filename_path_traversal_fails(self):
        """config_filename with path traversal should fail."""
        with pytest.raises(ValueError):
            VolumeMount(type="tapisvolume", source_id="vol1", 
                       config_content="test", config_filename="../etc/passwd")
    
    @pytest.mark.parametrize("vol_type", ["tapissnapshot", "pvc"])
    def test_config_content_rejected_for_readonly_types(self, vol_type):
        """tapissnapshot and pvc should reject config_content."""
        with pytest.raises(ValueError) as exc_info:
            VolumeMount(type=vol_type, source_id="id1", config_content="test")
        assert "does not support config_content" in str(exc_info.value)
    
    def test_config_content_size_limit(self):
        """Config content exceeding 1MB should fail."""
        large_content = "x" * (MAX_CONFIG_CONTENT_SIZE + 1)
        with pytest.raises(ValueError) as exc_info:
            validate_volume_mount_entry("/etc/large.txt", 
                                       {"type": "ephemeral", "config_content": large_content})
        assert "exceeds maximum size" in str(exc_info.value)
    
    @pytest.mark.parametrize("perms", ["0644", "0755", "644", "755", "0600", "777"])
    def test_valid_permissions(self, perms):
        """Valid octal permissions should pass."""
        validate_permissions_format(perms)  # Should not raise
    
    @pytest.mark.parametrize("perms", ["rwx", "0888", "64", "12345"])
    def test_invalid_permissions(self, perms):
        """Invalid permissions format should fail."""
        with pytest.raises(ValueError):
            validate_permissions_format(perms)


class TestSecretInterpolation:
    """Tests for secret interpolation in config_content."""
    
    def test_interpolate_secrets_method(self):
        """VolumeMount.interpolate_secrets() should replace placeholders."""
        mount = VolumeMount(
            type="ephemeral",
            config_content="host=${pods:secrets:DB_HOST}\npass=${pods:secrets:DB_PASS}"
        )
        result = mount.interpolate_secrets({"DB_HOST": "localhost", "DB_PASS": "secret123"})
        assert "host=localhost" in result
        assert "pass=secret123" in result
    
    def test_interpolate_missing_key_fail(self):
        """Missing key with fail_on_missing=True should raise."""
        mount = VolumeMount(type="ephemeral", config_content="host=${pods:secrets:DB_HOST}")
        with pytest.raises(ValueError) as exc_info:
            mount.interpolate_secrets({}, fail_on_missing=True)
        assert "DB_HOST" in str(exc_info.value)
    
    def test_interpolate_missing_key_no_fail(self):
        """Missing key with fail_on_missing=False should leave placeholder."""
        mount = VolumeMount(type="ephemeral", config_content="host=${pods:secrets:DB_HOST}")
        result = mount.interpolate_secrets({}, fail_on_missing=False)
        assert "${pods:secrets:DB_HOST}" in result
    
    def test_interpolate_function_none_content(self):
        """interpolate_config_content with None should return empty string."""
        assert interpolate_config_content(None, {}) == ""
    
    def test_interpolate_no_placeholders(self):
        """Content without placeholders should return unchanged."""
        content = "static content"
        assert interpolate_config_content(content, {}) == content


class TestMergeLogic:
    """Tests for volume_mounts merge during template inheritance."""
    
    def test_template_mounts_inherited(self):
        """Template mounts should be inherited when pod has none."""
        template = {"/mnt/data": {"type": "tapisvolume", "source_id": "template_vol"}}
        result = merge_pod_volume_mounts_with_template({}, template)
        assert result["/mnt/data"]["source_id"] == "template_vol"
    
    def test_pod_overrides_template(self):
        """Pod mount should override template mount at same path."""
        template = {"/mnt/data": {"type": "tapisvolume", "source_id": "template_vol"}}
        pod = {"/mnt/data": {"type": "tapisvolume", "source_id": "pod_vol"}}
        result = merge_pod_volume_mounts_with_template(pod, template)
        assert result["/mnt/data"]["source_id"] == "pod_vol"
    
    def test_null_removes_inherited_mount(self):
        """None value should remove inherited mount."""
        template = {"/mnt/data": {"type": "tapisvolume", "source_id": "template_vol"}}
        pod = {"/mnt/data": None}
        result = merge_pod_volume_mounts_with_template(pod, template)
        assert "/mnt/data" not in result
    
    def test_pod_adds_new_mount(self):
        """Pod can add new mounts not in template."""
        template = {"/mnt/data": {"type": "tapisvolume", "source_id": "vol1"}}
        pod = {"/etc/config": {"type": "ephemeral", "config_content": "test"}}
        result = merge_pod_volume_mounts_with_template(pod, template)
        assert "/mnt/data" in result and "/etc/config" in result
    
    def test_config_update_mode_preserved_in_merge(self):
        """config_update_mode should be preserved when merging."""
        template = {"/etc/cfg": {"type": "ephemeral", "config_content": "x", "config_update_mode": "once"}}
        result = merge_pod_volume_mounts_with_template({}, template)
        assert result["/etc/cfg"]["config_update_mode"] == "once"
    
    def test_both_empty(self):
        """Both empty should return empty dict."""
        assert merge_pod_volume_mounts_with_template({}, {}) == {}


class TestReadOnlyDefaults:
    """Tests for default read_only behavior by type."""
    
    @pytest.mark.parametrize("vol_type,source,expected", [
        ("tapisvolume", {"source_id": "vol1"}, False),
        ("tapissnapshot", {"source_id": "snap1"}, True),
        ("ephemeral", {"config_content": "test"}, True),
    ])
    def test_read_only_defaults(self, vol_type, source, expected):
        """Check read_only defaults by type."""
        mount = {"type": vol_type, **source}
        read_only = mount.get('read_only')
        if read_only is None:
            read_only = vol_type in ['tapissnapshot', 'ephemeral']
        assert read_only == expected


class TestPermissionValidation:
    """Tests for permission validation during volume_mounts processing."""
    
    def test_ephemeral_needs_no_permission_check(self):
        """Ephemeral mounts don't require resource permission checks."""
        mounts = {"/etc/config": {"type": "ephemeral", "config_content": "test"}}
        result = validate_volume_mounts_permissions(
            mounts, user="testuser", tenant="dev", site="tacc")
        assert result.is_valid
    
    def test_tapisvolume_not_found_fails(self):
        """tapisvolume with non-existent volume should fail."""
        mounts = {"/mnt/data": {"type": "tapisvolume", "source_id": "vol1"}}
        with patch('models_volumes.Volume') as mock_vol:
            mock_vol.db_get_with_pk.return_value = None
            result = validate_volume_mounts_permissions(
                mounts, user="testuser", tenant="dev", site="tacc")
        assert not result.is_valid
        assert any("vol1" in err for err in result.errors)
    
    def test_tapisvolume_found_passes(self):
        """tapisvolume with accessible volume should pass."""
        mounts = {"/mnt/data": {"type": "tapisvolume", "source_id": "vol1"}}
        mock_vol = MagicMock()
        mock_vol.volume_id = "vol1"
        with patch('models_volumes.Volume') as mock_cls:
            mock_cls.db_get_with_pk.return_value = mock_vol
            result = validate_volume_mounts_permissions(
                mounts, user="testuser", tenant="dev", site="tacc")
        assert result.is_valid


# ============================================================================
# API Integration Tests
# ============================================================================

class TestPodEphemeralConfig:
    """API tests for pods with ephemeral volume_mounts (ConfigMap)."""
    
    def test_create_pod_with_ephemeral_config(self, headers):
        """Test creating a pod with ephemeral config_content."""
        pod_def = {
            "pod_id": test_pod_ephemeral,
            "image": "notchristiangarcia/testserver:fastapi",
            "description": "Test pod with ephemeral config mount",
            "volume_mounts": {
                "/etc/app/config.ini": {
                    "type": "ephemeral",
                    "config_content": "[app]\nname=testapp",
                    "config_permissions": "0644"
                }
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        assert result['pod_id'] == test_pod_ephemeral
        assert '/etc/app/config.ini' in result['volume_mounts']
        assert result['volume_mounts']['/etc/app/config.ini']['type'] == 'ephemeral'
    
    def test_get_pod_ephemeral_with_include_configs(self, headers):
        """Test that ephemeral config is preserved with include_configs=true."""
        rsp = client.get(f"/pods/{test_pod_ephemeral}?include_configs=true", headers=headers)
        result = basic_response_checks(rsp)
        
        mount = result['volume_mounts'].get('/etc/app/config.ini')
        assert mount is not None
        assert mount['config_content'] == "[app]\nname=testapp"
    
    def test_create_pod_ephemeral_update_mode_once(self, headers):
        """Test creating pod with ephemeral config_update_mode='once'."""
        pod_def = {
            "pod_id": "testpodephonce",
            "image": "notchristiangarcia/testserver:fastapi",
            "volume_mounts": {
                "/etc/app/static.conf": {
                    "type": "ephemeral",
                    "config_content": "static=true",
                    "config_update_mode": "once"
                }
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        assert result['volume_mounts']['/etc/app/static.conf']['config_update_mode'] == 'once'


class TestTapisvolumeWithConfig:
    """API tests for tapisvolume with config_content (NFS-based)."""
    
    def test_create_volume_for_config(self, headers):
        """Create a volume to use for config file testing."""
        vol_def = {"volume_id": test_volume_config, "description": "Test volume for config"}
        rsp = client.post("/pods/volumes", data=json.dumps(vol_def), headers=headers)
        result = basic_response_checks(rsp)
        assert result['volume_id'] == test_volume_config
    
    def test_create_pod_tapisvolume_with_config(self, headers):
        """Test creating a pod with tapisvolume + config_content."""
        pod_def = {
            "pod_id": test_pod_tapisvol,
            "image": "notchristiangarcia/testserver:fastapi",
            "volume_mounts": {
                "/app/data": {
                    "type": "tapisvolume",
                    "source_id": test_volume_config,
                    "config_content": "[db]\nhost=localhost",
                    "config_filename": "db.ini",
                    "config_permissions": "0600",
                    "config_update_mode": "once"
                }
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        mount = result['volume_mounts'].get('/app/data')
        assert mount['type'] == 'tapisvolume'
        assert mount['config_filename'] == "db.ini"
        assert mount['config_update_mode'] == "once"
    
    def test_get_pod_tapisvolume_config_preserved(self, headers):
        """Test that tapisvolume config is preserved when getting pod."""
        rsp = client.get(f"/pods/{test_pod_tapisvol}?include_configs=true", headers=headers)
        result = basic_response_checks(rsp)
        
        mount = result['volume_mounts'].get('/app/data')
        assert mount['config_filename'] == "db.ini"
        assert mount['config_permissions'] == "0600"


class TestTemplateVolumeMounts:
    """API tests for templates with volume_mounts configurations."""
    
    def test_create_template(self, headers):
        """Create a template for volume_mounts testing."""
        template_def = {
            "template_id": test_template_vm,
            "description": "Template for volume_mounts testing"
        }
        rsp = client.post("/pods/templates", data=json.dumps(template_def), headers=headers)
        result = basic_response_checks(rsp)
        assert result['template_id'] == test_template_vm
    
    def test_add_template_tag_with_ephemeral(self, headers):
        """Test adding template tag with ephemeral volume_mounts."""
        tag_def = {
            "pod_definition": {
                "image": "notchristiangarcia/testserver:fastapi",
                "volume_mounts": {
                    "/etc/app/config.yml": {
                        "type": "ephemeral",
                        "config_content": "app:\n  name: myapp",
                        "config_update_mode": "always"
                    }
                }
            },
            "tag": "withephemeral",
            "commit_message": "Template tag with ephemeral config"
        }
        rsp = client.post(f"/pods/templates/{test_template_vm}/tags", 
                         data=json.dumps(tag_def), headers=headers)
        result = basic_response_checks(rsp)
        assert "withephemeral" in result['tag_timestamp']
    
    def test_create_pod_from_template(self, headers):
        """Test creating pod from template with ephemeral config."""
        pod_def = {
            "pod_id": "testpodtmpleph",
            "template": f"{test_template_vm}:withephemeral"
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        basic_response_checks(rsp)
        
        # Check derived config
        rsp = client.get("/pods/testpodtmpleph/derived", headers=headers)
        derived = basic_response_checks(rsp)
        
        mount = derived['volume_mounts'].get('/etc/app/config.yml')
        assert mount is not None
        assert mount['type'] == 'ephemeral'
    
    def test_pod_remove_template_mount_with_null(self, headers):
        """Test that pod can remove template mount using null."""
        pod_def = {
            "pod_id": "testpodremove",
            "template": f"{test_template_vm}:withephemeral",
            "volume_mounts": {"/etc/app/config.yml": None}
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        basic_response_checks(rsp)
        
        rsp = client.get("/pods/testpodremove/derived", headers=headers)
        derived = basic_response_checks(rsp)
        assert derived.get('volume_mounts', {}).get('/etc/app/config.yml') is None


class TestVolumeMountsValidationErrors:
    """API tests for volume_mounts validation error handling."""
    
    @pytest.mark.parametrize("pod_id,volume_mounts,error_substr", [
        ("testpodbadeph", {"/etc/cfg": {"type": "ephemeral"}}, "config_content"),
        ("testpodbadvol", {"/mnt/data": {"type": "tapisvolume"}}, "source_id"),
        ("testpodbadmode", {"/etc/cfg": {"type": "ephemeral", "config_content": "x", 
                                          "config_update_mode": "bad"}}, "config_update_mode"),
    ])
    def test_validation_errors(self, headers, pod_id, volume_mounts, error_substr):
        """Test various validation error cases."""
        pod_def = {
            "pod_id": pod_id,
            "image": "notchristiangarcia/testserver:fastapi",
            "volume_mounts": volume_mounts
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        
        assert rsp.status_code == 400
        data = response_format(rsp)
        assert any(error_substr in msg.lower() for msg in data['message'])


# ============================================================================
# Secret Map Integration Tests
# ============================================================================


class TestSecretMapIntegration:
    """Tests for secret_map injection into env vars and config_content."""
    
    @pytest.fixture(scope="class", autouse=True)
    def setup_secrets(self, headers):
        """Create test secrets before tests run."""
        secret_defs = [
            {"secret_id": test_secret_for_env, "secret_value": "env_secret_value_12345"},
            {"secret_id": test_secret_for_config, "secret_value": "config_secret_value_67890"},
        ]
        for secret_def in secret_defs:
            rsp = client.post("/pods/secrets", data=json.dumps(secret_def), headers=headers)
            if rsp.status_code == 409:
                rsp = client.put(f"/pods/secrets/{secret_def['secret_id']}", 
                               data=json.dumps({"secret_value": secret_def['secret_value']}), 
                               headers=headers)
        yield None
    
    def test_create_pod_with_secret_map_env(self, headers):
        """Create pod with secret_map referenced in environment_variables."""
        pod_def = {
            "pod_id": test_pod_secret_env,
            "image": "notchristiangarcia/testserver:fastapi",
            "description": "Test pod with secret_map env injection",
            "secret_map": {
                "DB_PASS": f"${{secret:{test_secret_for_env}}}"
            },
            "environment_variables": {
                "DATABASE_PASSWORD": "${pods:secrets:DB_PASS}",
                "COMBINED_URL": "postgres://user:${pods:secrets:DB_PASS}@localhost/db"
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        assert result['pod_id'] == test_pod_secret_env
        assert 'DB_PASS' in result.get('secret_map', {})
        assert '${pods:secrets:DB_PASS}' in result['environment_variables'].get('DATABASE_PASSWORD', '')
    
    def test_pod_secret_env_startup(self, headers):
        """Wait for secret env pod to become available."""
        i = 0
        while i < 30:
            rsp = client.get(f"/pods/{test_pod_secret_env}", headers=headers)
            result = basic_response_checks(rsp)
            if result['status'] == "AVAILABLE":
                break
            time.sleep(2)
            i += 1
        else:
            assert False, f"Pod {test_pod_secret_env} never became available"
        
        assert result['status'] == "AVAILABLE"
    
    def test_verify_secret_injected_in_env_via_exec(self, headers):
        """Verify secret is injected into pod env via exec printenv."""
        rsp = client.get(f"/pods/{test_pod_secret_env}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] != "AVAILABLE":
            pytest.skip(f"Pod not available, status: {result['status']}")
        
        exec_def = {"commands": [["printenv", "DATABASE_PASSWORD"]]}
        rsp = client.post(f"/pods/{test_pod_secret_env}/exec", 
                         data=json.dumps(exec_def), headers=headers)
        result = basic_response_checks(rsp)
        
        exec_results = result.get('execution_results', [])
        assert len(exec_results) > 0, f"No execution results: {result}"
        stdout = exec_results[0].get('stdout', '')
        assert exec_results[0].get('success', False), f"Command failed: {exec_results[0]}"
        assert "env_secret_value_12345" in stdout, f"Secret not found. Got: {stdout}"
    
    def test_verify_combined_url_secret_in_env(self, headers):
        """Verify inline secret interpolation in COMBINED_URL env var."""
        rsp = client.get(f"/pods/{test_pod_secret_env}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] != "AVAILABLE":
            pytest.skip(f"Pod not available, status: {result['status']}")
        
        exec_def = {"commands": [["printenv", "COMBINED_URL"]]}
        rsp = client.post(f"/pods/{test_pod_secret_env}/exec", 
                         data=json.dumps(exec_def), headers=headers)
        result = basic_response_checks(rsp)
        
        exec_results = result.get('execution_results', [])
        assert len(exec_results) > 0, f"No execution results: {result}"
        stdout = exec_results[0].get('stdout', '')
        assert exec_results[0].get('success', False), f"Command failed: {exec_results[0]}"
        assert "env_secret_value_12345" in stdout, f"Secret not interpolated. Got: {stdout}"
        assert "postgres://user:" in stdout, f"URL prefix missing. Got: {stdout}"
    
    def test_derived_endpoint_shows_secret_map(self, headers):
        """Verify /derived returns secret_map but NOT resolved values by default."""
        rsp = client.get(f"/pods/{test_pod_secret_env}/derived", headers=headers)
        result = basic_response_checks(rsp)
        
        assert 'secret_map' in result
        assert 'DB_PASS' in result['secret_map']
        assert "env_secret_value_12345" not in str(result['secret_map'])
    
    def test_create_pod_with_secret_in_config_content(self, headers):
        """Create pod with secret_map referenced in ephemeral config_content."""
        pod_def = {
            "pod_id": test_pod_secret_config,
            "image": "notchristiangarcia/testserver:fastapi",
            "description": "Test pod with secret in config_content",
            "secret_map": {
                "API_KEY": f"${{secret:{test_secret_for_config}}}"
            },
            "volume_mounts": {
                "/etc/app/config.ini": {
                    "type": "ephemeral",
                    "config_content": "[api]\nkey=${pods:secrets:API_KEY}\nhost=localhost",
                    "config_permissions": "0600"
                }
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        assert result['pod_id'] == test_pod_secret_config
        assert '/etc/app/config.ini' in result.get('volume_mounts', {})
    
    def test_pod_secret_config_startup(self, headers):
        """Wait for secret config pod to become available."""
        i = 0
        while i < 30:
            rsp = client.get(f"/pods/{test_pod_secret_config}", headers=headers)
            result = basic_response_checks(rsp)
            if result['status'] == "AVAILABLE":
                break
            time.sleep(2)
            i += 1
        else:
            assert False, f"Pod {test_pod_secret_config} never became available"
        
        assert result['status'] == "AVAILABLE"
    
    def test_verify_secret_injected_in_config_file(self, headers):
        """Verify secret is injected into mounted config file via exec cat."""
        rsp = client.get(f"/pods/{test_pod_secret_config}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] != "AVAILABLE":
            pytest.skip(f"Pod not available, status: {result['status']}")
        
        exec_def = {"commands": [["cat", "/etc/app/config.ini"]]}
        rsp = client.post(f"/pods/{test_pod_secret_config}/exec", 
                         data=json.dumps(exec_def), headers=headers)
        result = basic_response_checks(rsp)
        
        exec_results = result.get('execution_results', [])
        assert len(exec_results) > 0, f"No execution results: {result}"
        stdout = exec_results[0].get('stdout', '')
        assert exec_results[0].get('success', False), f"Command failed: {exec_results[0]}"
        
        assert "config_secret_value_67890" in stdout, f"Secret not found. Got: {stdout}"
        assert "[api]" in stdout, f"Config section missing. Got: {stdout}"
        assert "host=localhost" in stdout, f"Static config missing. Got: {stdout}"
    
    def test_derived_with_include_configs_shows_placeholders(self, headers):
        """Verify /derived?include_configs=true shows placeholders, not resolved values."""
        rsp = client.get(f"/pods/{test_pod_secret_config}/derived?include_configs=true", headers=headers)
        result = basic_response_checks(rsp)
        
        mount = result.get('volume_mounts', {}).get('/etc/app/config.ini', {})
        config_content = mount.get('config_content', '')
        
        assert "[api]" in config_content
        assert "host=localhost" in config_content
        assert "config_secret_value_67890" not in config_content
    
    def test_create_pod_with_both_env_and_config_secrets(self, headers):
        """Create pod using secret_map in both env vars and config_content."""
        pod_def = {
            "pod_id": test_pod_secret_both,
            "image": "notchristiangarcia/testserver:fastapi",
            "description": "Test pod with secrets in both env and config",
            "secret_map": {
                "SHARED_SECRET": f"${{secret:{test_secret_for_env}}}"
            },
            "environment_variables": {
                "ENV_SECRET": "${pods:secrets:SHARED_SECRET}"
            },
            "volume_mounts": {
                "/etc/app/shared.conf": {
                    "type": "ephemeral",
                    "config_content": "shared_key=${pods:secrets:SHARED_SECRET}"
                }
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        assert result['pod_id'] == test_pod_secret_both
    
    def test_pod_secret_both_startup(self, headers):
        """Wait for combined secrets pod to become available."""
        i = 0
        while i < 30:
            rsp = client.get(f"/pods/{test_pod_secret_both}", headers=headers)
            result = basic_response_checks(rsp)
            if result['status'] == "AVAILABLE":
                break
            time.sleep(2)
            i += 1
        else:
            assert False, f"Pod {test_pod_secret_both} never became available"
    
    def test_verify_same_secret_in_both_env_and_config(self, headers):
        """Verify same secret_map key resolves consistently in env and config."""
        rsp = client.get(f"/pods/{test_pod_secret_both}", headers=headers)
        result = basic_response_checks(rsp)
        if result['status'] != "AVAILABLE":
            pytest.skip(f"Pod not available, status: {result['status']}")
        
        # Check env var
        exec_def = {"commands": [["printenv", "ENV_SECRET"]]}
        rsp = client.post(f"/pods/{test_pod_secret_both}/exec", 
                         data=json.dumps(exec_def), headers=headers)
        result = basic_response_checks(rsp)
        exec_results = result.get('execution_results', [])
        assert len(exec_results) > 0, f"No execution results: {result}"
        env_value = exec_results[0].get('stdout', '').strip()
        
        # Check config file
        exec_def = {"commands": [["cat", "/etc/app/shared.conf"]]}
        rsp = client.post(f"/pods/{test_pod_secret_both}/exec", 
                         data=json.dumps(exec_def), headers=headers)
        result = basic_response_checks(rsp)
        exec_results = result.get('execution_results', [])
        assert len(exec_results) > 0, f"No execution results: {result}"
        config_content = exec_results[0].get('stdout', '')
        
        assert "env_secret_value_12345" in env_value, f"Secret not in env. Got: {env_value}"
        assert "env_secret_value_12345" in config_content, f"Secret not in config. Got: {config_content}"


class TestDerivedEndpointResolveSecrets:
    """Tests for /derived endpoint with resolve_secrets parameter (admin-only)."""
    
    def test_derived_without_resolve_secrets_hides_values(self, headers):
        """Default /derived should NOT show resolved secret values."""
        rsp = client.get(f"/pods/{test_pod_secret_env}", headers=headers)
        if rsp.status_code == 404:
            pytest.skip(f"Pod {test_pod_secret_env} not found")
        
        rsp = client.get(f"/pods/{test_pod_secret_env}/derived", headers=headers)
        result = basic_response_checks(rsp)
        
        env_vars = result.get('environment_variables', {})
        db_password = env_vars.get('DATABASE_PASSWORD', '')
        
        assert "env_secret_value_12345" not in db_password
        assert "${pods:secrets:" in db_password or "DB_PASS" in str(result.get('secret_map', {}))
    
    def test_derived_resolve_secrets_requires_admin(self, headers, regular_headers):
        """resolve_secrets=true should be rejected for non-admin users."""
        rsp = client.get(f"/pods/{test_pod_secret_env}", headers=headers)
        if rsp.status_code == 404:
            pytest.skip(f"Pod {test_pod_secret_env} not found")
        
        rsp = client.get(f"/pods/{test_pod_secret_env}/derived?resolve_secrets=true", 
                        headers=regular_headers)
        
        # 400/403 for admin error, 404 if no READ permission
        assert rsp.status_code in [400, 403, 404], f"Expected 400/403/404, got {rsp.status_code}"
        
        if rsp.status_code in [400, 403]:
            data = rsp.json()
            error_msg = data.get('message', '').lower()
            assert "not authorized" in error_msg
    
    def test_derived_resolve_secrets_shows_values_for_admin(self, headers):
        """resolve_secrets=true should show resolved values for admins."""
        rsp = client.get(f"/pods/{test_pod_secret_env}", headers=headers)
        if rsp.status_code == 404:
            pytest.skip(f"Pod {test_pod_secret_env} not found")
        
        rsp = client.get(
            f"/pods/{test_pod_secret_env}/derived?resolve_secrets=true&include_configs=true", 
            headers=headers
        )
        result = basic_response_checks(rsp)
        
        env_vars = result.get('environment_variables', {})
        db_password = env_vars.get('DATABASE_PASSWORD', '')
        
        assert "env_secret_value_12345" in db_password, \
            f"Admin should see resolved secret. Got: {db_password}"
        
        metadata = rsp.json().get('metadata', {})
        assert metadata.get('secrets_resolved') == True
    
    def test_derived_resolve_secrets_interpolates_config_content(self, headers):
        """resolve_secrets=true should also interpolate secrets in config_content."""
        rsp = client.get(f"/pods/{test_pod_secret_config}", headers=headers)
        if rsp.status_code == 404:
            pytest.skip(f"Pod {test_pod_secret_config} not found")
        
        rsp = client.get(
            f"/pods/{test_pod_secret_config}/derived?resolve_secrets=true&include_configs=true", 
            headers=headers
        )
        result = basic_response_checks(rsp)
        
        mount = result.get('volume_mounts', {}).get('/etc/app/config.ini', {})
        config_content = mount.get('config_content', '')
        
        assert "config_secret_value_67890" in config_content, \
            f"Config should have resolved secret. Got: {config_content}"
    
    def test_derived_without_resolve_secrets_preserves_placeholders_in_config(self, headers):
        """Without resolve_secrets, config_content should show placeholders."""
        rsp = client.get(f"/pods/{test_pod_secret_config}", headers=headers)
        if rsp.status_code == 404:
            pytest.skip(f"Pod {test_pod_secret_config} not found")
        
        rsp = client.get(
            f"/pods/{test_pod_secret_config}/derived?include_configs=true", 
            headers=headers
        )
        result = basic_response_checks(rsp)
        
        mount = result.get('volume_mounts', {}).get('/etc/app/config.ini', {})
        config_content = mount.get('config_content', '')
        
        assert "config_secret_value_67890" not in config_content
        assert "${pods:secrets:API_KEY}" in config_content


# ============================================================================
# Random Password & Pod Networking Integration Tests
# ============================================================================

class TestRandomPasswordIntegration:
    """Integration tests for ${pods:random:N} feature."""
    
    def test_random_password_generation_and_persistence(self, headers):
        """Create pod with random password, verify generation and persistence."""
        pod_def = {
            "pod_id": "testpodrandompass",
            "image": "notchristiangarcia/testserver:fastapi",
            "description": "Test random password",
            "status_requested": "OFF",
            "secret_map": {
                "PASS_32": "${pods:random:32}",
                "PASS_16": "${pods:random:16}"
            },
            "environment_variables": {
                "MY_PASSWORD": "${pods:secrets:PASS_32}",
                "SHORT_PASS": "${pods:secrets:PASS_16}"
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        secret_map = result.get('secret_map', {})
        # Verify passwords generated with correct lengths
        assert len(secret_map.get('PASS_32', '')) == 32
        assert len(secret_map.get('PASS_16', '')) == 16
        # Verify pattern is replaced (persisted)
        assert "${pods:random:" not in secret_map.get('PASS_32', '')
        # Verify passwords are different
        assert secret_map['PASS_32'] != secret_map['PASS_16']
        
        # Verify /derived shows env vars reference the secret_map keys
        rsp = client.get("/pods/testpodrandompass/derived", headers=headers)
        derived = basic_response_checks(rsp)
        env_vars = derived.get('environment_variables', {})
        assert env_vars.get('MY_PASSWORD') == "${pods:secrets:PASS_32}"
        assert env_vars.get('SHORT_PASS') == "${pods:secrets:PASS_16}"
        
        # Verify secret_map in derived still has resolved values
        derived_sm = derived.get('secret_map', {})
        assert derived_sm.get('PASS_32') == secret_map['PASS_32']
        assert derived_sm.get('PASS_16') == secret_map['PASS_16']
    
    def test_random_password_multiple_lengths(self, headers):
        """Test multiple random passwords of varying lengths."""
        pod_def = {
            "pod_id": "testpodrandommulti",
            "image": "notchristiangarcia/testserver:fastapi",
            "status_requested": "OFF",
            "secret_map": {
                "SHORT": "${pods:random:8}",
                "LONG": "${pods:random:64}"
            },
            "environment_variables": {
                "ENV_SHORT": "${pods:secrets:SHORT}",
                "ENV_LONG": "${pods:secrets:LONG}"
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        secret_map = result.get('secret_map', {})
        assert len(secret_map.get('SHORT', '')) == 8
        assert len(secret_map.get('LONG', '')) == 64
        
        # Verify persistence via GET
        rsp = client.get("/pods/testpodrandommulti", headers=headers)
        result = basic_response_checks(rsp)
        assert result.get('secret_map', {}).get('SHORT') == secret_map['SHORT']


class TestPodNetworkingIntegration:
    """Integration tests for ${pods:url} and ${pods:networking:*} features."""
    
    def test_pods_url_shorthand_resolution(self, headers):
        """Test ${pods:url} shorthand resolves to full URL."""
        pod_def = {
            "pod_id": "testpodnetref",
            "image": "notchristiangarcia/testserver:fastapi",
            "status_requested": "OFF",
            "secret_map": {
                "MY_URL": "${pods:url}",
                "CALLBACK": "https://${pods:url}/callback"
            },
            "environment_variables": {
                "POD_URL": "${pods:secrets:MY_URL}",
                "OAUTH_CALLBACK": "${pods:secrets:CALLBACK}"
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        secret_map = result.get('secret_map', {})
        my_url = secret_map.get('MY_URL', '')
        
        # Should be resolved, not the pattern
        assert "${pods:url}" not in my_url
        assert "testpodnetref" in my_url
        assert ".pods." in my_url
        # Callback should be properly constructed
        assert secret_map.get('CALLBACK', '').endswith('/callback')
        assert "testpodnetref" in secret_map.get('CALLBACK', '')
        
        # Verify persistence via GET
        rsp = client.get("/pods/testpodnetref", headers=headers)
        result = basic_response_checks(rsp)
        assert result.get('secret_map', {}).get('MY_URL') == my_url
    
    def test_pods_networking_explicit_fields(self, headers):
        """Test ${pods:networking:default:field} explicit syntax."""
        pod_def = {
            "pod_id": "testpodnetcfg",
            "image": "notchristiangarcia/testserver:fastapi",
            "status_requested": "OFF",
            "secret_map": {
                "HOST": "${pods:networking:default:url}",
                "PORT": "${pods:networking:default:port}",
                "PROTO": "${pods:networking:default:protocol}"
            },
            "environment_variables": {
                "SERVER_HOST": "${pods:secrets:HOST}",
                "SERVER_PORT": "${pods:secrets:PORT}",
                "SERVER_PROTO": "${pods:secrets:PROTO}"
            }
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        
        secret_map = result.get('secret_map', {})
        # URL should contain pod_id
        assert "testpodnetcfg" in secret_map.get('HOST', '')
        # Port should be 5000 (default)
        assert secret_map.get('PORT', '') == "5000"
        # Protocol should be http (default networking protocol)
        assert secret_map.get('PROTO', '') == "http"
        
        # Verify /derived shows env var references
        rsp = client.get("/pods/testpodnetcfg/derived", headers=headers)
        derived = basic_response_checks(rsp)
        env_vars = derived.get('environment_variables', {})
        assert env_vars.get('SERVER_HOST') == "${pods:secrets:HOST}"
        assert env_vars.get('SERVER_PORT') == "${pods:secrets:PORT}"