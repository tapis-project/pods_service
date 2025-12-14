"""
Unit tests for combine_pod_and_template_recursively function in models_templates_utils.py

These tests verify the priority order: pod modified > template setting > pod default
and test recursive template chaining, infinite loop detection, and all field-specific merge logic.
"""
import os
import sys
import json
import time
import pytest
from unittest.mock import Mock, patch, MagicMock
from types import SimpleNamespace

from tests.test_utils import headers, response_format, basic_response_checks, t

# Allows us to import pods's modules.
sys.path.append('/home/tapis/service')


# ============================================================================
# MOCK FIXTURES AND HELPER CLASSES
# ============================================================================

class MockResources:
    """Mock Resources class that mimics the behavior of the real Resources model"""
    def __init__(self, cpu_request=250, cpu_limit=2000, mem_request=256, mem_limit=3000,
                 gpus=0, ephemeral_storage_request=200, ephemeral_storage_limit=1000):
        self.cpu_request = cpu_request
        self.cpu_limit = cpu_limit
        self.mem_request = mem_request
        self.mem_limit = mem_limit
        self.gpus = gpus
        self.ephemeral_storage_request = ephemeral_storage_request
        self.ephemeral_storage_limit = ephemeral_storage_limit

    def dict(self):
        return {
            'cpu_request': self.cpu_request,
            'cpu_limit': self.cpu_limit,
            'mem_request': self.mem_request,
            'mem_limit': self.mem_limit,
            'gpus': self.gpus,
            'ephemeral_storage_request': self.ephemeral_storage_request,
            'ephemeral_storage_limit': self.ephemeral_storage_limit,
        }


class MockNetworking:
    """Mock Networking class"""
    def __init__(self, protocol="http", port=5000, url="", tapis_auth=False):
        self.protocol = protocol
        self.port = port
        self.url = url
        self.tapis_auth = tapis_auth

    def dict(self):
        return {
            'protocol': self.protocol,
            'port': self.port,
            'url': self.url,
            'tapis_auth': self.tapis_auth,
        }


class MockPod:
    """Mock Pod object for testing combine_pod_and_template_recursively"""
    def __init__(
        self,
        pod_id="testpod",
        tenant_id="dev",
        site_id="tacc",
        image="",
        description="",
        command=None,
        arguments=None,
        environment_variables=None,
        volume_mounts=None,
        networking=None,
        resources=None,
        compute_queue="default",
        time_to_stop_default=43200,
        time_to_stop_instance=None,
        modified_fields=None,
        template=""
    ):
        self.pod_id = pod_id
        self.tenant_id = tenant_id
        self.site_id = site_id
        self.image = image
        self.description = description
        self.command = command
        self.arguments = arguments
        self.environment_variables = environment_variables if environment_variables is not None else {}
        self.volume_mounts = volume_mounts if volume_mounts is not None else {}
        self.networking = networking if networking is not None else {"default": {"protocol": "http", "port": 5000}}
        self.resources = resources if resources is not None else MockResources()
        self.compute_queue = compute_queue
        self.time_to_stop_default = time_to_stop_default
        self.time_to_stop_instance = time_to_stop_instance
        self.modified_fields = modified_fields if modified_fields is not None else []
        self.template = template


class MockTemplateTagPodDefinition:
    """Mock TemplateTagPodDefinition"""
    def __init__(
        self,
        image=None,
        template=None,
        description=None,
        command=None,
        arguments=None,
        environment_variables=None,
        volume_mounts=None,
        networking=None,
        resources=None,
        compute_queue="default",
        time_to_stop_default=None,
        time_to_stop_instance=None,
    ):
        self.image = image
        self.template = template
        self.description = description
        self.command = command
        self.arguments = arguments
        self.environment_variables = environment_variables if environment_variables is not None else {}
        self.volume_mounts = volume_mounts if volume_mounts is not None else {}
        self.networking = networking if networking is not None else {}
        self.resources = resources if resources is not None else {}
        self.compute_queue = compute_queue
        self.time_to_stop_default = time_to_stop_default
        self.time_to_stop_instance = time_to_stop_instance

    def dict(self):
        return {
            'image': self.image,
            'template': self.template,
            'description': self.description,
            'command': self.command,
            'arguments': self.arguments,
            'environment_variables': self.environment_variables,
            'volume_mounts': self.volume_mounts,
            'networking': self.networking,
            'resources': self.resources,
            'compute_queue': self.compute_queue,
            'time_to_stop_default': self.time_to_stop_default,
            'time_to_stop_instance': self.time_to_stop_instance,
        }


class MockTemplateTag:
    """Mock TemplateTag"""
    def __init__(self, template_id="testtemplate", tag="latest", tag_timestamp="latest@2024-01-01-00:00:00",
                 pod_definition=None):
        self.template_id = template_id
        self.tag = tag
        self.tag_timestamp = tag_timestamp
        self.pod_definition = pod_definition if pod_definition is not None else MockTemplateTagPodDefinition().dict()


class MockTemplate:
    """Mock Template"""
    def __init__(self, template_id="testtemplate"):
        self.template_id = template_id


class MockTenantConfig:
    """Mock tenant config for URL generation"""
    def __init__(self, base_url="https://dev.tapis.io"):
        self.base_url = base_url


class MockTenantCache:
    """Mock tenant cache"""
    def get_tenant_config(self, tenant_id):
        return MockTenantConfig()


class MockT:
    """Mock t object from __init__"""
    def __init__(self):
        self.tenant_cache = MockTenantCache()


# ============================================================================
# PYTEST FIXTURES
# ============================================================================

@pytest.fixture
def mock_t():
    """Fixture to mock the t object"""
    return MockT()


@pytest.fixture
def default_pod():
    """Create a default pod with no modifications"""
    return MockPod()


@pytest.fixture
def empty_template_pod_definition():
    """Return an empty TemplateTagPodDefinition dict (baseline for comparison)"""
    return MockTemplateTagPodDefinition().dict()


@pytest.fixture
def template_with_resources():
    """Template that sets specific resource values"""
    return MockTemplateTag(
        pod_definition={
            'image': None,
            'template': None,
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {
                'cpu_request': 1000,
                'cpu_limit': 4000,
                'mem_request': 512,
                'mem_limit': 8000,
                'gpus': 1,
                'ephemeral_storage_request': 500,
                'ephemeral_storage_limit': 2000,
            },
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )


@pytest.fixture
def template_with_networking():
    """Template that sets networking configuration"""
    return MockTemplateTag(
        pod_definition={
            'image': None,
            'template': None,
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {
                'default': {'protocol': 'http', 'port': 8080, 'tapis_auth': True},
                'api': {'protocol': 'http', 'port': 3000},
            },
            'resources': {},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )


@pytest.fixture
def template_with_env_vars():
    """Template that sets environment variables"""
    return MockTemplateTag(
        pod_definition={
            'image': None,
            'template': None,
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {
                'DB_HOST': 'localhost',
                'DB_PORT': '5432',
                'TEMPLATE_VAR': 'from_template',
            },
            'volume_mounts': {},
            'networking': {},
            'resources': {},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )


@pytest.fixture
def template_with_volume_mounts():
    """Template that sets volume mounts"""
    return MockTemplateTag(
        pod_definition={
            'image': None,
            'template': None,
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {
                'data-volume': {'type': 'tapisvolume', 'mount_path': '/data'},
                'config-volume': {'type': 'pvc', 'mount_path': '/config'},
            },
            'networking': {},
            'resources': {},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )


@pytest.fixture
def template_with_simple_fields():
    """Template that sets simple fields"""
    return MockTemplateTag(
        pod_definition={
            'image': 'template-image:v1',
            'template': None,
            'description': 'Template description',
            'command': ['python', 'app.py'],
            'arguments': ['--port', '8080'],
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {},
            'compute_queue': 'gpu',
            'time_to_stop_default': 7200,
            'time_to_stop_instance': 3600,
        }
    )


# ============================================================================
# TEST: get_modified_template_fields
# ============================================================================

def test_get_modified_template_fields_returns_empty_when_no_changes():
    """When template matches original, return empty dict"""
    from models_templates_utils import get_modified_template_fields
    
    original = MockTemplateTagPodDefinition().dict()
    modified = MockTemplateTagPodDefinition().dict()
    
    result = get_modified_template_fields(original, modified)
    assert result == {}


def test_get_modified_template_fields_returns_changed_fields():
    """When fields differ, return the modified values"""
    from models_templates_utils import get_modified_template_fields
    
    original = MockTemplateTagPodDefinition().dict()
    modified = MockTemplateTagPodDefinition(
        image="postgres:15",
        description="A postgres template"
    ).dict()
    
    result = get_modified_template_fields(original, modified)
    assert 'image' in result
    assert result['image'] == "postgres:15"
    assert 'description' in result
    assert result['description'] == "A postgres template"


def test_get_modified_template_fields_resources_null_subfields_removed():
    """Null subfields in resources should be removed"""
    from models_templates_utils import get_modified_template_fields
    
    original = MockTemplateTagPodDefinition().dict()
    modified_template = MockTemplateTagPodDefinition()
    modified_template.resources = {'cpu_request': 500, 'cpu_limit': None, 'mem_request': None}
    modified = modified_template.dict()
    
    result = get_modified_template_fields(original, modified)
    
    # Should have resources but only with non-null values
    assert 'resources' in result
    assert 'cpu_request' in result['resources']
    assert 'cpu_limit' not in result['resources']
    assert 'mem_request' not in result['resources']


# ============================================================================
# TEST: Resources Merge
# ============================================================================

@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_template_resources_override_pod_defaults(mock_t_obj, mock_derive, template_with_resources):
    """Template resources should override pod defaults when pod hasn't modified them"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_resources)
    
    pod = MockPod(
        resources=MockResources(),  # defaults
        modified_fields=[]  # nothing modified by user
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Template values should be applied
    assert result.resources['cpu_request'] == 1000
    assert result.resources['cpu_limit'] == 4000
    assert result.resources['mem_request'] == 512
    assert result.resources['mem_limit'] == 8000
    assert result.resources['gpus'] == 1


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_pod_modified_resources_preserved(mock_t_obj, mock_derive, template_with_resources):
    """Pod's user-modified resources should be preserved over template values"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_resources)
    
    pod = MockPod(
        resources=MockResources(cpu_request=2000, mem_limit=16000),
        modified_fields=['resources.cpu_request', 'resources.mem_limit']  # user modified these
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # User-modified fields should be preserved
    assert result.resources['cpu_request'] == 2000  # user modified
    assert result.resources['mem_limit'] == 16000   # user modified
    # Template values for non-modified fields
    assert result.resources['cpu_limit'] == 4000    # from template
    assert result.resources['mem_request'] == 512   # from template
    assert result.resources['gpus'] == 1            # from template


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_all_resource_subfields_handled(mock_t_obj, mock_derive, template_with_resources):
    """All 7 resource subfields should be properly merged"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_resources)
    
    pod = MockPod(
        resources=MockResources(
            cpu_request=250, cpu_limit=2000,
            mem_request=256, mem_limit=3000,
            gpus=0,
            ephemeral_storage_request=200, ephemeral_storage_limit=1000
        ),
        modified_fields=['resources.gpus']  # only gpus modified
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # gpus should be pod's value (user modified)
    assert result.resources['gpus'] == 0
    # All others should be template values
    assert result.resources['cpu_request'] == 1000
    assert result.resources['cpu_limit'] == 4000
    assert result.resources['mem_request'] == 512
    assert result.resources['mem_limit'] == 8000
    assert result.resources['ephemeral_storage_request'] == 500
    assert result.resources['ephemeral_storage_limit'] == 2000


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_partial_template_resources(mock_t_obj, mock_derive):
    """Template with only some resource fields should only override those"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    # Template only sets cpu_request
    partial_template = MockTemplateTag(
        pod_definition={
            'image': None,
            'template': None,
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {'cpu_request': 1000},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), partial_template)
    
    pod = MockPod(
        resources=MockResources(cpu_request=250, cpu_limit=2000, mem_request=256, mem_limit=3000),
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Only cpu_request should come from template
    assert result.resources['cpu_request'] == 1000
    # Others should be pod defaults
    assert result.resources['cpu_limit'] == 2000
    assert result.resources['mem_request'] == 256
    assert result.resources['mem_limit'] == 3000


# ============================================================================
# TEST: Networking Merge
# ============================================================================

@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_template_networking_applied(mock_t_obj, mock_derive, template_with_networking):
    """Template networking should be applied to pod"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_networking)
    
    pod = MockPod(
        pod_id="mypod",
        networking={"default": {"protocol": "http", "port": 5000}},
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Template networking should be applied
    assert 'default' in result.networking
    assert 'api' in result.networking
    assert result.networking['default']['port'] == 8080
    assert result.networking['default']['tapis_auth'] == True
    assert result.networking['api']['port'] == 3000


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_pod_modified_networking_preserved(mock_t_obj, mock_derive, template_with_networking):
    """Pod's modified networking should override template"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_networking)
    
    pod = MockPod(
        pod_id="mypod",
        networking={
            "default": {"protocol": "http", "port": 9000, "tapis_auth": False}
        },
        modified_fields=['networking']  # user modified networking
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Pod's networking for 'default' should override template
    assert result.networking['default']['port'] == 9000
    assert result.networking['default']['tapis_auth'] == False
    # 'api' from template should still be there
    assert 'api' in result.networking


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_url_generated_for_default_network(mock_t_obj, mock_derive, template_with_networking):
    """URL should be generated correctly for default network"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_networking)
    
    pod = MockPod(
        pod_id="mypod",
        networking={"default": {"protocol": "http", "port": 5000}},
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # URL should be generated for default network
    assert 'url' in result.networking['default']
    assert 'mypod.pods.' in result.networking['default']['url']


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_url_generated_for_named_network(mock_t_obj, mock_derive, template_with_networking):
    """URL should be generated correctly for named networks"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_networking)
    
    pod = MockPod(
        pod_id="mypod",
        networking={"default": {"protocol": "http", "port": 5000}},
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # URL for named network should include the network name
    assert 'url' in result.networking['api']
    assert 'mypod-api.pods.' in result.networking['api']['url']


# ============================================================================
# TEST: Environment Variables Merge
# ============================================================================

@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_env_vars_merged_when_flag_true(mock_t_obj, mock_derive, template_with_env_vars):
    """When _TAPIS_INTERNAL_USE_TEMPLATE_ENVS=True, merge template + pod envs"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_env_vars)
    
    pod = MockPod(
        environment_variables={
            'MY_VAR': 'my_value',
            '_TAPIS_INTERNAL_USE_TEMPLATE_ENVS': 'True',
        },
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Should have both template and pod env vars
    assert result.environment_variables['DB_HOST'] == 'localhost'
    assert result.environment_variables['DB_PORT'] == '5432'
    assert result.environment_variables['MY_VAR'] == 'my_value'


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_pod_env_vars_override_template(mock_t_obj, mock_derive, template_with_env_vars):
    """Pod env vars should override template env vars with same key when pod has modified environment_variables"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_env_vars)
    
    pod = MockPod(
        environment_variables={
            'DB_HOST': 'production-db.example.com',  # override template
            '_TAPIS_INTERNAL_USE_TEMPLATE_ENVS': 'True',
        },
        modified_fields=['environment_variables']  # pod's env vars should take priority
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Pod's value should override template's value
    assert result.environment_variables['DB_HOST'] == 'production-db.example.com'
    # Template-only vars should still be present
    assert result.environment_variables['DB_PORT'] == '5432'


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_env_vars_not_merged_when_flag_false(mock_t_obj, mock_derive, template_with_env_vars):
    """When _TAPIS_INTERNAL_USE_TEMPLATE_ENVS=False, use only pod envs"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_env_vars)
    
    pod = MockPod(
        environment_variables={
            'MY_VAR': 'my_value',
            '_TAPIS_INTERNAL_USE_TEMPLATE_ENVS': 'False',
        },
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Should only have pod env vars, not template vars
    assert result.environment_variables['MY_VAR'] == 'my_value'
    # This behavior leaves pod's env_vars unchanged when flag is False
    # Template vars should NOT be added
    assert 'DB_HOST' not in result.environment_variables or result.environment_variables.get('DB_HOST') is None


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_env_vars_default_merge_behavior(mock_t_obj, mock_derive, template_with_env_vars):
    """Default behavior (no flag) should merge like True"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_env_vars)
    
    pod = MockPod(
        environment_variables={'MY_VAR': 'my_value'},  # no flag set
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Default behavior is True, so should merge
    assert result.environment_variables['DB_HOST'] == 'localhost'
    assert result.environment_variables['MY_VAR'] == 'my_value'


# ============================================================================
# TEST: Volume Mounts Merge
# ============================================================================

@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_volume_mounts_merged_when_flag_true(mock_t_obj, mock_derive, template_with_volume_mounts):
    """When _TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES=True, merge template + pod volumes"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_volume_mounts)
    
    pod = MockPod(
        environment_variables={'_TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES': 'True'},
        volume_mounts={
            'my-volume': {'type': 'tapisvolume', 'mount_path': '/mnt/mydata'},
        },
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Should have both template and pod volume mounts
    assert 'data-volume' in result.volume_mounts
    assert 'config-volume' in result.volume_mounts
    assert 'my-volume' in result.volume_mounts


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_pod_volumes_override_template(mock_t_obj, mock_derive, template_with_volume_mounts):
    """Pod volumes should override template volumes with same key"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_volume_mounts)
    
    pod = MockPod(
        environment_variables={'_TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES': 'True'},
        volume_mounts={
            'data-volume': {'type': 'tapisvolume', 'mount_path': '/custom/path'},  # override
        },
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Pod's value should override
    assert result.volume_mounts['data-volume']['mount_path'] == '/custom/path'
    # Template-only volumes should still be present
    assert 'config-volume' in result.volume_mounts


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_volume_mounts_not_merged_when_flag_false(mock_t_obj, mock_derive, template_with_volume_mounts):
    """When _TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES=False, use only pod volumes"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_volume_mounts)
    
    pod = MockPod(
        environment_variables={'_TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES': 'False'},
        volume_mounts={
            'my-volume': {'type': 'tapisvolume', 'mount_path': '/mnt/mydata'},
        },
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Should only have pod volume mounts
    assert 'my-volume' in result.volume_mounts
    assert 'data-volume' not in result.volume_mounts
    assert 'config-volume' not in result.volume_mounts


# ============================================================================
# TEST: Template Chaining (Recursive)
# ============================================================================

@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_two_level_template_chain(mock_t_obj, mock_derive):
    """Test template1 -> template2 chain"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    # Base template (template2) - sets image and cpu_request
    template2 = MockTemplateTag(
        template_id="template2",
        pod_definition={
            'image': 'base-image:latest',
            'template': None,  # No parent
            'description': 'Base template',
            'command': None,
            'arguments': None,
            'environment_variables': {'BASE_VAR': 'base_value'},
            'volume_mounts': {},
            'networking': {},
            'resources': {'cpu_request': 500},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    # Child template (template1) - inherits from template2, overrides image
    template1 = MockTemplateTag(
        template_id="template1",
        pod_definition={
            'image': 'child-image:latest',  # overrides base
            'template': 'template2:latest@2024-01-01',  # references template2
            'description': None,  # inherits from template2
            'command': None,
            'arguments': None,
            'environment_variables': {'CHILD_VAR': 'child_value'},
            'volume_mounts': {},
            'networking': {},
            'resources': {'cpu_limit': 3000},  # adds cpu_limit
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    def derive_side_effect(template_name, *args, **kwargs):
        if 'template2' in template_name:
            return ("template2:latest@2024-01-01", MockTemplate("template2"), template2)
        else:
            return ("template1:latest@2024-01-01", MockTemplate("template1"), template1)
    
    mock_derive.side_effect = derive_side_effect
    
    pod = MockPod(
        environment_variables={},
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Should have template1's image (overrides template2)
    assert result.image == 'child-image:latest'
    # Should have template2's description (template1 didn't set it)
    assert result.description == 'Base template'
    # Should have both env vars merged
    assert result.environment_variables.get('BASE_VAR') == 'base_value'
    assert result.environment_variables.get('CHILD_VAR') == 'child_value'
    # Should have resources from both
    assert result.resources.get('cpu_request') == 500  # from template2
    assert result.resources.get('cpu_limit') == 3000   # from template1


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_three_level_template_chain(mock_t_obj, mock_derive):
    """Test template1 -> template2 -> template3 chain"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    # Innermost template (template3)
    template3 = MockTemplateTag(
        template_id="template3",
        pod_definition={
            'image': 'base-image:v1',
            'template': None,
            'description': 'Level 3 description',
            'command': ['/bin/bash'],
            'arguments': None,
            'environment_variables': {'LEVEL': '3'},
            'volume_mounts': {},
            'networking': {},
            'resources': {'cpu_request': 100, 'mem_request': 128},
            'compute_queue': 'default',
            'time_to_stop_default': 3600,
            'time_to_stop_instance': None,
        }
    )
    
    # Middle template (template2)
    template2 = MockTemplateTag(
        template_id="template2",
        pod_definition={
            'image': 'middle-image:v2',
            'template': 'template3:latest@2024-01-01',
            'description': None,  # inherit from template3
            'command': None,  # inherit from template3
            'arguments': ['--verbose'],
            'environment_variables': {'LEVEL': '2', 'MIDDLE_VAR': 'middle'},
            'volume_mounts': {},
            'networking': {},
            'resources': {'cpu_request': 200},  # override template3
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    # Outermost template (template1)
    template1 = MockTemplateTag(
        template_id="template1",
        pod_definition={
            'image': None,  # inherit from template2
            'template': 'template2:latest@2024-01-01',
            'description': 'Level 1 description',  # override
            'command': None,
            'arguments': None,
            'environment_variables': {'LEVEL': '1'},  # override
            'volume_mounts': {},
            'networking': {},
            'resources': {'cpu_limit': 1000},  # add new
            'compute_queue': 'gpu',  # override
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    def derive_side_effect(template_name, *args, **kwargs):
        if 'template3' in template_name:
            return ("template3:latest@2024-01-01", MockTemplate("template3"), template3)
        elif 'template2' in template_name:
            return ("template2:latest@2024-01-01", MockTemplate("template2"), template2)
        else:
            return ("template1:latest@2024-01-01", MockTemplate("template1"), template1)
    
    mock_derive.side_effect = derive_side_effect
    
    pod = MockPod(
        environment_variables={},
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # image: template1(None) -> template2(middle-image) -> template3(base-image)
    # After processing: template2's image should be applied (it overrides template3)
    assert result.image == 'middle-image:v2'
    
    # description: template1 sets it, so it wins
    assert result.description == 'Level 1 description'
    
    # command: template1(None) -> template2(None) -> template3(['/bin/bash'])
    assert result.command == ['/bin/bash']
    
    # arguments: template2 sets it
    assert result.arguments == ['--verbose']
    
    # LEVEL env var: closer templates override deeper ones
    # template1 sets LEVEL='1', which should override template2's '2' and template3's '3'
    assert result.environment_variables.get('LEVEL') == '1'
    assert result.environment_variables.get('MIDDLE_VAR') == 'middle'
    
    # compute_queue: template1 sets 'gpu'
    assert result.compute_queue == 'gpu'


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_pod_overrides_template_chain(mock_t_obj, mock_derive):
    """Pod's modified fields should override entire template chain"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    template2 = MockTemplateTag(
        template_id="template2",
        pod_definition={
            'image': 'template2-image:latest',
            'template': None,
            'description': 'Template 2 description',
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {'cpu_request': 500},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    template1 = MockTemplateTag(
        template_id="template1",
        pod_definition={
            'image': 'template1-image:latest',
            'template': 'template2:latest@2024-01-01',
            'description': 'Template 1 description',
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {'cpu_request': 1000},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    def derive_side_effect(template_name, *args, **kwargs):
        if 'template2' in template_name:
            return ("template2:latest@2024-01-01", MockTemplate("template2"), template2)
        else:
            return ("template1:latest@2024-01-01", MockTemplate("template1"), template1)
    
    mock_derive.side_effect = derive_side_effect
    
    # Pod with user-modified fields
    pod = MockPod(
        image='my-custom-image:v1',
        description='My pod description',
        resources=MockResources(cpu_request=2000),
        modified_fields=['image', 'description', 'resources.cpu_request']
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Pod's values should be preserved
    assert result.image == 'my-custom-image:v1'
    assert result.description == 'My pod description'
    assert result.resources['cpu_request'] == 2000


# ============================================================================
# TEST: Infinite Loop Detection
# ============================================================================

@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_self_referencing_template(mock_t_obj, mock_derive):
    """Template referencing itself should raise ValueError"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    # Template that references itself
    template1 = MockTemplateTag(
        template_id="template1",
        pod_definition={
            'image': 'some-image:latest',
            'template': 'template1:latest@2024-01-01',  # self-reference!
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate("template1"), template1)
    
    pod = MockPod()
    
    with pytest.raises(ValueError) as excinfo:
        combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    assert "Infinite loop detected" in str(excinfo.value)
    assert "template1" in str(excinfo.value)


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_circular_template_chain(mock_t_obj, mock_derive):
    """Circular chain (A -> B -> A) should raise ValueError"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    # Template A references B
    template_a = MockTemplateTag(
        template_id="template_a",
        pod_definition={
            'image': 'image-a:latest',
            'template': 'template_b:latest@2024-01-01',
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    # Template B references A (circular!)
    template_b = MockTemplateTag(
        template_id="template_b",
        pod_definition={
            'image': 'image-b:latest',
            'template': 'template_a:latest@2024-01-01',  # circular reference!
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    def derive_side_effect(template_name, *args, **kwargs):
        if 'template_b' in template_name:
            return ("template_b:latest@2024-01-01", MockTemplate("template_b"), template_b)
        else:
            return ("template_a:latest@2024-01-01", MockTemplate("template_a"), template_a)
    
    mock_derive.side_effect = derive_side_effect
    
    pod = MockPod()
    
    with pytest.raises(ValueError) as excinfo:
        combine_pod_and_template_recursively(pod, "template_a", tenant="dev", site="tacc")
    
    assert "Infinite loop detected" in str(excinfo.value)


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_three_level_circular_chain(mock_t_obj, mock_derive):
    """Three-level circular chain (A -> B -> C -> A) should raise ValueError"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    template_c = MockTemplateTag(
        template_id="template_c",
        pod_definition={
            'image': 'image-c:latest',
            'template': 'template_a:latest@2024-01-01',  # back to A!
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    template_b = MockTemplateTag(
        template_id="template_b",
        pod_definition={
            'image': 'image-b:latest',
            'template': 'template_c:latest@2024-01-01',
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    template_a = MockTemplateTag(
        template_id="template_a",
        pod_definition={
            'image': 'image-a:latest',
            'template': 'template_b:latest@2024-01-01',
            'description': None,
            'command': None,
            'arguments': None,
            'environment_variables': {},
            'volume_mounts': {},
            'networking': {},
            'resources': {},
            'compute_queue': 'default',
            'time_to_stop_default': None,
            'time_to_stop_instance': None,
        }
    )
    
    def derive_side_effect(template_name, *args, **kwargs):
        if 'template_c' in template_name:
            return ("template_c:latest@2024-01-01", MockTemplate("template_c"), template_c)
        elif 'template_b' in template_name:
            return ("template_b:latest@2024-01-01", MockTemplate("template_b"), template_b)
        else:
            return ("template_a:latest@2024-01-01", MockTemplate("template_a"), template_a)
    
    mock_derive.side_effect = derive_side_effect
    
    pod = MockPod()
    
    with pytest.raises(ValueError) as excinfo:
        combine_pod_and_template_recursively(pod, "template_a", tenant="dev", site="tacc")
    
    assert "Infinite loop detected" in str(excinfo.value)


# ============================================================================
# TEST: Simple Field Overrides
# ============================================================================

@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_image_override(mock_t_obj, mock_derive, template_with_simple_fields):
    """Test image field priority"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_simple_fields)
    
    # Pod with default image (not modified)
    pod = MockPod(image="", modified_fields=[])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.image == 'template-image:v1'
    
    # Pod with modified image
    pod = MockPod(image="my-image:v2", modified_fields=['image'])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.image == 'my-image:v2'


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_description_override(mock_t_obj, mock_derive, template_with_simple_fields):
    """Test description field priority"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_simple_fields)
    
    # Pod with default description (not modified)
    pod = MockPod(description="", modified_fields=[])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.description == 'Template description'
    
    # Pod with modified description
    pod = MockPod(description="My custom description", modified_fields=['description'])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.description == 'My custom description'


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_command_override(mock_t_obj, mock_derive, template_with_simple_fields):
    """Test command field priority"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_simple_fields)
    
    # Pod with default command (not modified)
    pod = MockPod(command=None, modified_fields=[])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.command == ['python', 'app.py']
    
    # Pod with modified command
    pod = MockPod(command=['./start.sh'], modified_fields=['command'])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.command == ['./start.sh']


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_arguments_override(mock_t_obj, mock_derive, template_with_simple_fields):
    """Test arguments field priority"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_simple_fields)
    
    # Pod with default arguments (not modified)
    pod = MockPod(arguments=None, modified_fields=[])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.arguments == ['--port', '8080']
    
    # Pod with modified arguments
    pod = MockPod(arguments=['--debug'], modified_fields=['arguments'])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.arguments == ['--debug']


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_compute_queue_override(mock_t_obj, mock_derive, template_with_simple_fields):
    """Test compute_queue field priority"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_simple_fields)
    
    # Pod with default compute_queue (not modified)
    pod = MockPod(compute_queue="default", modified_fields=[])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.compute_queue == 'gpu'
    
    # Pod with modified compute_queue
    pod = MockPod(compute_queue="high-memory", modified_fields=['compute_queue'])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.compute_queue == 'high-memory'


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_time_to_stop_default_override(mock_t_obj, mock_derive, template_with_simple_fields):
    """Test time_to_stop_default field priority"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_simple_fields)
    
    # Pod with default time_to_stop_default (not modified)
    pod = MockPod(time_to_stop_default=43200, modified_fields=[])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.time_to_stop_default == 7200
    
    # Pod with modified time_to_stop_default
    pod = MockPod(time_to_stop_default=86400, modified_fields=['time_to_stop_default'])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.time_to_stop_default == 86400


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_time_to_stop_instance_override(mock_t_obj, mock_derive, template_with_simple_fields):
    """Test time_to_stop_instance field priority"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template_with_simple_fields)
    
    # Pod with default time_to_stop_instance (not modified)
    pod = MockPod(time_to_stop_instance=None, modified_fields=[])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.time_to_stop_instance == 3600
    
    # Pod with modified time_to_stop_instance
    pod = MockPod(time_to_stop_instance=1800, modified_fields=['time_to_stop_instance'])
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    assert result.time_to_stop_instance == 1800


# ============================================================================
# TEST: No Template Case
# ============================================================================

def test_no_template_returns_unchanged_pod():
    """When template_name is None/empty, pod should be returned unchanged"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    pod = MockPod(
        image="my-image:v1",
        description="My description",
        resources=MockResources(cpu_request=500)
    )
    
    result = combine_pod_and_template_recursively(pod, None, tenant="dev", site="tacc")
    
    assert result.image == "my-image:v1"
    assert result.description == "My description"


def test_empty_template_returns_unchanged_pod():
    """When template_name is empty string, pod should be returned unchanged"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    pod = MockPod(
        image="my-image:v1",
        description="My description"
    )
    
    result = combine_pod_and_template_recursively(pod, "", tenant="dev", site="tacc")
    
    assert result.image == "my-image:v1"
    assert result.description == "My description"


# ============================================================================
# TEST: Three-Level Priority for All Resource Fields
# Priority: pod modified > closer template > deeper template > pod defaults
# ============================================================================

@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_resource_priority_ephemeral_storage_request(mock_t_obj, mock_derive):
    """Test ephemeral_storage_request: closer template overrides deeper template"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    # Deeper template sets ephemeral_storage_request = 2048
    template2 = MockTemplateTag(
        template_id="template2",
        pod_definition={
            'image': None, 'template': None, 'description': None, 'command': None,
            'arguments': None, 'environment_variables': {}, 'volume_mounts': {},
            'networking': {}, 'compute_queue': 'default',
            'time_to_stop_default': None, 'time_to_stop_instance': None,
            'resources': {'ephemeral_storage_request': 2048, 'ephemeral_storage_limit': 4096},
        }
    )
    
    # Closer template sets ephemeral_storage_request = 3072
    template1 = MockTemplateTag(
        template_id="template1",
        pod_definition={
            'image': None, 'template': 'template2:latest@2024-01-01', 'description': None,
            'command': None, 'arguments': None, 'environment_variables': {},
            'volume_mounts': {}, 'networking': {}, 'compute_queue': 'default',
            'time_to_stop_default': None, 'time_to_stop_instance': None,
            'resources': {'ephemeral_storage_request': 3072, 'ephemeral_storage_limit': 6144},
        }
    )
    
    def derive_side_effect(template_name, *args, **kwargs):
        if 'template2' in template_name:
            return ("template2:latest@2024-01-01", MockTemplate("template2"), template2)
        else:
            return ("template1:latest@2024-01-01", MockTemplate("template1"), template1)
    
    mock_derive.side_effect = derive_side_effect
    
    # Pod with default values (not modified)
    pod = MockPod(
        resources=MockResources(ephemeral_storage_request=4096),  # pod default
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Closer template (template1) should win: 3072, not 2048 (deeper) or 4096 (pod default)
    assert result.resources['ephemeral_storage_request'] == 3072
    assert result.resources['ephemeral_storage_limit'] == 6144


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_resource_priority_pod_modified_overrides_all_templates(mock_t_obj, mock_derive):
    """Test pod modified resource overrides entire template chain"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    # Deeper template
    template2 = MockTemplateTag(
        template_id="template2",
        pod_definition={
            'image': None, 'template': None, 'description': None, 'command': None,
            'arguments': None, 'environment_variables': {}, 'volume_mounts': {},
            'networking': {}, 'compute_queue': 'default',
            'time_to_stop_default': None, 'time_to_stop_instance': None,
            'resources': {'ephemeral_storage_request': 2048},
        }
    )
    
    # Closer template
    template1 = MockTemplateTag(
        template_id="template1",
        pod_definition={
            'image': None, 'template': 'template2:latest@2024-01-01', 'description': None,
            'command': None, 'arguments': None, 'environment_variables': {},
            'volume_mounts': {}, 'networking': {}, 'compute_queue': 'default',
            'time_to_stop_default': None, 'time_to_stop_instance': None,
            'resources': {'ephemeral_storage_request': 3072},
        }
    )
    
    def derive_side_effect(template_name, *args, **kwargs):
        if 'template2' in template_name:
            return ("template2:latest@2024-01-01", MockTemplate("template2"), template2)
        else:
            return ("template1:latest@2024-01-01", MockTemplate("template1"), template1)
    
    mock_derive.side_effect = derive_side_effect
    
    # Pod with explicitly modified ephemeral_storage_request
    pod = MockPod(
        resources=MockResources(ephemeral_storage_request=1024),
        modified_fields=['resources.ephemeral_storage_request']  # user modified
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # Pod's modified value should win: 1024, not 3072 (closer) or 2048 (deeper)
    assert result.resources['ephemeral_storage_request'] == 1024


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_resource_priority_all_seven_fields(mock_t_obj, mock_derive):
    """Test all 7 resource subfields follow priority: pod > closer template > deeper template"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    # Deeper template sets all resources
    template2 = MockTemplateTag(
        template_id="template2",
        pod_definition={
            'image': None, 'template': None, 'description': None, 'command': None,
            'arguments': None, 'environment_variables': {}, 'volume_mounts': {},
            'networking': {}, 'compute_queue': 'default',
            'time_to_stop_default': None, 'time_to_stop_instance': None,
            'resources': {
                'cpu_request': 100,
                'cpu_limit': 1000,
                'mem_request': 128,
                'mem_limit': 1024,
                'gpus': 0,
                'ephemeral_storage_request': 512,
                'ephemeral_storage_limit': 1024,
            },
        }
    )
    
    # Closer template overrides some resources
    template1 = MockTemplateTag(
        template_id="template1",
        pod_definition={
            'image': None, 'template': 'template2:latest@2024-01-01', 'description': None,
            'command': None, 'arguments': None, 'environment_variables': {},
            'volume_mounts': {}, 'networking': {}, 'compute_queue': 'default',
            'time_to_stop_default': None, 'time_to_stop_instance': None,
            'resources': {
                'cpu_request': 200,  # overrides template2
                'mem_limit': 2048,   # overrides template2
                'ephemeral_storage_request': 1024,  # overrides template2
            },
        }
    )
    
    def derive_side_effect(template_name, *args, **kwargs):
        if 'template2' in template_name:
            return ("template2:latest@2024-01-01", MockTemplate("template2"), template2)
        else:
            return ("template1:latest@2024-01-01", MockTemplate("template1"), template1)
    
    mock_derive.side_effect = derive_side_effect
    
    # Pod with some modified resources
    pod = MockPod(
        resources=MockResources(
            cpu_request=500,  # will be overridden by template unless modified
            cpu_limit=3000,   # pod default
            mem_request=256,  # pod default
            mem_limit=3000,   # pod default
            gpus=2,           # user modified
            ephemeral_storage_request=4096,  # pod default
            ephemeral_storage_limit=8192,    # pod default
        ),
        modified_fields=['resources.gpus']  # only gpus is user-modified
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # cpu_request: template1 sets 200, should use that (closer template wins)
    assert result.resources['cpu_request'] == 200
    # cpu_limit: template2 sets 1000, template1 doesn't set it -> use template2's value
    assert result.resources['cpu_limit'] == 1000
    # mem_request: template2 sets 128, template1 doesn't set it -> use template2's value
    assert result.resources['mem_request'] == 128
    # mem_limit: template1 sets 2048 (overrides template2's 1024)
    assert result.resources['mem_limit'] == 2048
    # gpus: pod modified to 2, should override templates
    assert result.resources['gpus'] == 2
    # ephemeral_storage_request: template1 sets 1024 (overrides template2's 512)
    assert result.resources['ephemeral_storage_request'] == 1024
    # ephemeral_storage_limit: template2 sets 1024, template1 doesn't set it -> use template2's value
    assert result.resources['ephemeral_storage_limit'] == 1024


@patch('models_templates_utils.derive_template_info')
@patch('models_templates_utils.t')
def test_resource_priority_deeper_template_only(mock_t_obj, mock_derive):
    """Test that deeper template values are used when closer template doesn't set them"""
    from models_templates_utils import combine_pod_and_template_recursively
    
    mock_t_obj.tenant_cache = MockTenantCache()
    
    # Deeper template sets ephemeral storage
    template2 = MockTemplateTag(
        template_id="template2",
        pod_definition={
            'image': 'base-image:v1', 'template': None, 'description': None,
            'command': None, 'arguments': None, 'environment_variables': {},
            'volume_mounts': {}, 'networking': {}, 'compute_queue': 'default',
            'time_to_stop_default': None, 'time_to_stop_instance': None,
            'resources': {'ephemeral_storage_request': 2048, 'ephemeral_storage_limit': 4096},
        }
    )
    
    # Closer template only sets cpu, not ephemeral
    template1 = MockTemplateTag(
        template_id="template1",
        pod_definition={
            'image': None, 'template': 'template2:latest@2024-01-01', 'description': None,
            'command': None, 'arguments': None, 'environment_variables': {},
            'volume_mounts': {}, 'networking': {}, 'compute_queue': 'default',
            'time_to_stop_default': None, 'time_to_stop_instance': None,
            'resources': {'cpu_request': 500},  # only sets cpu, not ephemeral
        }
    )
    
    def derive_side_effect(template_name, *args, **kwargs):
        if 'template2' in template_name:
            return ("template2:latest@2024-01-01", MockTemplate("template2"), template2)
        else:
            return ("template1:latest@2024-01-01", MockTemplate("template1"), template1)
    
    mock_derive.side_effect = derive_side_effect
    
    pod = MockPod(
        resources=MockResources(ephemeral_storage_request=4096),  # pod default
        modified_fields=[]
    )
    
    result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
    
    # ephemeral_storage_request: template1 doesn't set it, use template2's value (2048)
    assert result.resources['ephemeral_storage_request'] == 2048
    # cpu_request: template1 sets it (500)
    assert result.resources['cpu_request'] == 500


# ============================================================================
# TEST: Template Overrides - TemplateOverrides Model Validation
# ============================================================================

class MockTemplateOverrides:
    """Mock TemplateOverrides class for testing template override functionality"""
    def __init__(self, volume_mounts=None):
        self.volume_mounts = volume_mounts if volume_mounts is not None else {}
        # Validate volume mount names
        for template_vol, user_vol in self.volume_mounts.items():
            if not self._is_valid_volume_name(user_vol):
                raise ValueError(f"Invalid volume name '{user_vol}': must be lowercase alphanumeric characters or underscores, starting with a letter")
    
    @staticmethod
    def _is_valid_volume_name(name):
        """Validate volume name: lowercase alphanumeric and underscores, starts with letter"""
        import re
        return bool(re.match(r'^[a-z][a-z0-9_]*$', name))


def apply_template_overrides_mock(pod):
    """
    Mock implementation of apply_template_overrides for testing.
    Applies template_overrides to a pod's volume_mounts by renaming template volumes to user volumes.
    """
    if not hasattr(pod, 'template_overrides') or pod.template_overrides is None:
        return pod
    
    if not pod.template_overrides.volume_mounts:
        return pod
    
    # Apply volume mount overrides
    new_volume_mounts = {}
    for vol_name, vol_config in pod.volume_mounts.items():
        if vol_name in pod.template_overrides.volume_mounts:
            # Rename this volume to the user's volume name
            new_vol_name = pod.template_overrides.volume_mounts[vol_name]
            new_volume_mounts[new_vol_name] = vol_config
        else:
            # Keep the original volume
            new_volume_mounts[vol_name] = vol_config
    
    pod.volume_mounts = new_volume_mounts
    return pod


class MockPodWithOverrides:
    """Mock pod object for testing template overrides"""
    def __init__(self):
        self.volume_mounts = {
            "shared_vol1": {"type": "tapisvolume", "mount_path": "/data1"},
            "shared_vol2": {"type": "tapisvolume", "mount_path": "/data2"},
            "shared_vol3": {"type": "tapisvolume", "mount_path": "/data3"},
        }
        self.template_overrides = None


class TestTemplateOverridesModel:
    """Test the TemplateOverrides model validation"""
    
    def test_valid_volume_mounts(self):
        """Test valid volume mount overrides"""
        overrides = MockTemplateOverrides(
            volume_mounts={
                "template_vol": "user_vol123"
            }
        )
        assert overrides.volume_mounts == {"template_vol": "user_vol123"}
    
    def test_invalid_volume_name_uppercase(self):
        """Test that uppercase volume names are rejected"""
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            MockTemplateOverrides(
                volume_mounts={
                    "template_vol": "UserVol123"  # Contains uppercase
                }
            )
    
    def test_invalid_volume_name_special_chars(self):
        """Test that special characters in volume names are rejected"""
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            MockTemplateOverrides(
                volume_mounts={
                    "template_vol": "user-vol-123"  # Contains hyphens
                }
            )
    
    def test_invalid_volume_name_starts_with_number(self):
        """Test that volume names starting with numbers are rejected"""
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            MockTemplateOverrides(
                volume_mounts={
                    "template_vol": "123uservol"  # Starts with number
                }
            )
    
    def test_empty_overrides(self):
        """Test empty overrides are valid"""
        overrides = MockTemplateOverrides()
        assert overrides.volume_mounts == {}
    
    def test_multiple_volume_overrides(self):
        """Test multiple volume overrides"""
        overrides = MockTemplateOverrides(
            volume_mounts={
                "vol1": "myvol1",
                "vol2": "myvol2",
                "vol3": "myvol3"
            }
        )
        assert len(overrides.volume_mounts) == 3


class TestApplyTemplateOverrides:
    """Test the apply_template_overrides function"""
    
    def test_apply_single_override(self):
        """Test applying a single volume override"""
        pod = MockPodWithOverrides()
        pod.template_overrides = MockTemplateOverrides(
            volume_mounts={"shared_vol1": "myvol1"}
        )
        
        result = apply_template_overrides_mock(pod)
        
        # Check that shared_vol1 was replaced with myvol1
        assert "myvol1" in result.volume_mounts
        assert "shared_vol1" not in result.volume_mounts
        # Check that config was preserved
        assert result.volume_mounts["myvol1"]["mount_path"] == "/data1"
        # Check that other volumes weren't affected
        assert "shared_vol2" in result.volume_mounts
        assert "shared_vol3" in result.volume_mounts
    
    def test_apply_multiple_overrides(self):
        """Test applying multiple volume overrides"""
        pod = MockPodWithOverrides()
        pod.template_overrides = MockTemplateOverrides(
            volume_mounts={
                "shared_vol1": "myvol1",
                "shared_vol2": "myvol2"
            }
        )
        
        result = apply_template_overrides_mock(pod)
        
        # Check that both volumes were replaced
        assert "myvol1" in result.volume_mounts
        assert "myvol2" in result.volume_mounts
        assert "shared_vol1" not in result.volume_mounts
        assert "shared_vol2" not in result.volume_mounts
        # Check that unaffected volume remains
        assert "shared_vol3" in result.volume_mounts
    
    def test_apply_all_overrides(self):
        """Test overriding all volumes"""
        pod = MockPodWithOverrides()
        pod.template_overrides = MockTemplateOverrides(
            volume_mounts={
                "shared_vol1": "myvol1",
                "shared_vol2": "myvol2",
                "shared_vol3": "myvol3"
            }
        )
        
        result = apply_template_overrides_mock(pod)
        
        # Check that all shared volumes were replaced
        assert "myvol1" in result.volume_mounts
        assert "myvol2" in result.volume_mounts
        assert "myvol3" in result.volume_mounts
        assert "shared_vol1" not in result.volume_mounts
        assert "shared_vol2" not in result.volume_mounts
        assert "shared_vol3" not in result.volume_mounts
    
    def test_no_overrides(self):
        """Test pod without overrides remains unchanged"""
        pod = MockPodWithOverrides()
        pod.template_overrides = MockTemplateOverrides()
        
        result = apply_template_overrides_mock(pod)
        
        # Check that volumes weren't changed
        assert "shared_vol1" in result.volume_mounts
        assert "shared_vol2" in result.volume_mounts
        assert "shared_vol3" in result.volume_mounts
    
    def test_override_nonexistent_volume(self):
        """Test overriding a volume that doesn't exist in template"""
        pod = MockPodWithOverrides()
        pod.template_overrides = MockTemplateOverrides(
            volume_mounts={"nonexistent_vol": "myvol"}
        )
        
        result = apply_template_overrides_mock(pod)
        
        # Original volumes should remain
        assert "shared_vol1" in result.volume_mounts
        # Nonexistent override should be ignored
        assert "myvol" not in result.volume_mounts
    
    def test_pod_without_template_overrides_attribute(self):
        """Test pod without template_overrides attribute"""
        pod = MockPodWithOverrides()
        delattr(pod, 'template_overrides')
        
        result = apply_template_overrides_mock(pod)
        
        # Should return unchanged
        assert result == pod
    
    def test_empty_volume_mounts(self):
        """Test pod with no volume_mounts"""
        pod = MockPodWithOverrides()
        pod.volume_mounts = {}
        pod.template_overrides = MockTemplateOverrides(
            volume_mounts={"vol1": "myvol1"}
        )
        
        result = apply_template_overrides_mock(pod)
        
        # Should handle gracefully
        assert result.volume_mounts == {}


class TestTemplateOverridesIntegrationScenarios:
    """Test realistic usage scenarios for template overrides"""
    
    def test_ml_template_scenario(self):
        """Test ML template with training data override"""
        # Simulate an ML template with shared volumes
        pod = MockPodWithOverrides()
        pod.volume_mounts = {
            "shared_training_data": {"type": "tapisvolume", "mount_path": "/training_data"},
            "shared_models": {"type": "tapisvolume", "mount_path": "/models"},
            "shared_outputs": {"type": "tapisvolume", "mount_path": "/outputs"}
        }
        
        # User overrides training data and outputs with their own
        pod.template_overrides = MockTemplateOverrides(
            volume_mounts={
                "shared_training_data": "mytrainingdata",
                "shared_outputs": "myoutputs"
            }
        )
        
        result = apply_template_overrides_mock(pod)
        
        # Verify overrides
        assert "mytrainingdata" in result.volume_mounts
        assert "myoutputs" in result.volume_mounts
        # Verify shared models volume remains
        assert "shared_models" in result.volume_mounts
        # Verify mount paths preserved
        assert result.volume_mounts["mytrainingdata"]["mount_path"] == "/training_data"
        assert result.volume_mounts["myoutputs"]["mount_path"] == "/outputs"
    
    def test_database_template_scenario(self):
        """Test database template with data volume override"""
        pod = MockPodWithOverrides()
        pod.volume_mounts = {
            "postgres_data": {"type": "tapisvolume", "mount_path": "/var/lib/postgresql/data"},
            "postgres_backups": {"type": "tapisvolume", "mount_path": "/backups"}
        }
        
        # User provides their own data volume
        pod.template_overrides = MockTemplateOverrides(
            volume_mounts={
                "postgres_data": "mypostgresdata"
            }
        )
        
        result = apply_template_overrides_mock(pod)
        
        assert "mypostgresdata" in result.volume_mounts
        assert result.volume_mounts["mypostgresdata"]["mount_path"] == "/var/lib/postgresql/data"
        # Shared backup volume remains
        assert "postgres_backups" in result.volume_mounts
