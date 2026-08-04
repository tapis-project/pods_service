"""
Unit tests for combine_pod_and_template_recursively function in models_templates_utils.py

Tests verify the priority order: pod modified > template setting > pod default
and test recursive template chaining, infinite loop detection, and field-specific merge logic.

CONSOLIDATED from original 86 tests (~2600 lines) to ~30 tests (~800 lines)
"""
import pytest
from unittest.mock import patch, MagicMock

import sys
sys.path.append('/home/tapis/service')


# ============================================================================
# MOCK FIXTURES AND HELPER CLASSES
# ============================================================================

class MockResources:
    """Mock Resources class"""
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
        return {k: getattr(self, k) for k in ['cpu_request', 'cpu_limit', 'mem_request', 
                'mem_limit', 'gpus', 'ephemeral_storage_request', 'ephemeral_storage_limit']}


class MockPod:
    """Mock Pod object for testing combine_pod_and_template_recursively"""
    def __init__(self, pod_id="testpod", tenant_id="dev", site_id="tacc", image="",
                 description="", command=None, arguments=None, environment_variables=None,
                 secret_map=None, volume_mounts=None, networking=None, resources=None,
                 compute_queue="default", time_to_stop_default=43200, time_to_stop_instance=None,
                 modified_fields=None, template=""):
        self.pod_id = pod_id
        self.tenant_id = tenant_id
        self.site_id = site_id
        self.image = image
        self.description = description
        self.command = command
        self.arguments = arguments or []
        self.environment_variables = environment_variables or {}
        self.secret_map = secret_map or {}
        self.volume_mounts = volume_mounts or {}
        self.networking = networking or {"default": {"protocol": "http", "port": 5000}}
        self.resources = resources or MockResources()
        self.compute_queue = compute_queue
        self.time_to_stop_default = time_to_stop_default
        self.time_to_stop_instance = time_to_stop_instance
        self.modified_fields = modified_fields or []
        self.template = template


class MockTemplateTagPodDefinition:
    """Mock TemplateTagPodDefinition"""
    def __init__(self, image=None, template=None, description=None, command=None,
                 arguments=None, environment_variables=None, secret_map=None,
                 volume_mounts=None, networking=None, resources=None,
                 compute_queue="default", time_to_stop_default=None, time_to_stop_instance=None):
        self.image = image
        self.template = template
        self.description = description
        self.command = command
        self.arguments = arguments  # Keep None to match real TemplateTagPodDefinition default
        self.environment_variables = environment_variables or {}
        self.secret_map = secret_map or {}
        self.volume_mounts = volume_mounts or {}
        self.networking = networking or {}
        self.resources = resources or {}
        self.compute_queue = compute_queue
        self.time_to_stop_default = time_to_stop_default
        self.time_to_stop_instance = time_to_stop_instance

    def dict(self):
        return {k: getattr(self, k) for k in ['image', 'template', 'description', 'command',
                'arguments', 'environment_variables', 'secret_map', 'volume_mounts',
                'networking', 'resources', 'compute_queue', 'time_to_stop_default', 'time_to_stop_instance']}


class MockTemplateTag:
    """Mock TemplateTag"""
    def __init__(self, template_id="testtemplate", tag="latest", 
                 tag_timestamp="latest@2024-01-01", pod_definition=None):
        self.template_id = template_id
        self.tag = tag
        self.tag_timestamp = tag_timestamp
        self.pod_definition = pod_definition or MockTemplateTagPodDefinition().dict()


class MockTemplate:
    def __init__(self, template_id="testtemplate"):
        self.template_id = template_id


class MockTenantCache:
    def get_tenant_config(self, tenant_id):
        class TC:
            base_url = "https://dev.tapis.io"
        return TC()


class MockT:
    def __init__(self):
        self.tenant_cache = MockTenantCache()


# ============================================================================
# PYTEST FIXTURES
# ============================================================================

@pytest.fixture
def mock_t():
    return MockT()


def make_template(template_id="testtemplate", **pod_def_kwargs):
    """Helper to create a template with given pod_definition fields"""
    base = MockTemplateTagPodDefinition().dict()
    base.update(pod_def_kwargs)
    return MockTemplateTag(template_id=template_id, pod_definition=base)


# ============================================================================
# TEST: get_modified_template_fields
# ============================================================================

class TestGetModifiedTemplateFields:
    """Tests for get_modified_template_fields function"""
    
    def test_returns_empty_when_no_changes(self):
        """When template matches original, return empty dict"""
        from models_templates_utils import get_modified_template_fields
        original = MockTemplateTagPodDefinition().dict()
        modified = MockTemplateTagPodDefinition().dict()
        assert get_modified_template_fields(original, modified) == {}

    def test_returns_changed_fields(self):
        """When fields differ, return the modified values"""
        from models_templates_utils import get_modified_template_fields
        original = MockTemplateTagPodDefinition().dict()
        modified = MockTemplateTagPodDefinition(image="postgres:15", description="A postgres template").dict()
        result = get_modified_template_fields(original, modified)
        assert result['image'] == "postgres:15"
        assert result['description'] == "A postgres template"

    def test_resources_null_subfields_removed(self):
        """Null subfields in resources should be removed"""
        from models_templates_utils import get_modified_template_fields
        original = MockTemplateTagPodDefinition().dict()
        modified = MockTemplateTagPodDefinition()
        modified.resources = {'cpu_request': 500, 'cpu_limit': None, 'mem_request': None}
        result = get_modified_template_fields(original, modified.dict())
        assert 'resources' in result
        assert 'cpu_request' in result['resources']
        assert 'cpu_limit' not in result['resources']


# ============================================================================
# TEST: Simple Field Overrides (Consolidated - was 7 separate tests)
# ============================================================================

class TestSimpleFieldOverrides:
    """Test priority for all simple fields: pod modified > template > pod default"""
    
    @pytest.mark.parametrize("field,template_val,pod_default,pod_modified", [
        ("image", "template-image:v1", "", "my-image:v2"),
        ("description", "Template desc", "", "My custom desc"),
        ("compute_queue", "gpu", "default", "high-memory"),
        ("time_to_stop_default", 7200, 43200, 86400),
        ("time_to_stop_instance", 3600, None, 1800),
    ])
    def test_field_priority(self, field, template_val, pod_default, pod_modified):
        """Test that template values override defaults, but pod modifications override template"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        template = make_template(**{field: template_val})
        
        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            
            # Test: template overrides pod default
            pod = MockPod(**{field: pod_default}, modified_fields=[])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert getattr(result, field) == template_val
            
            # Test: pod modified overrides template
            pod = MockPod(**{field: pod_modified}, modified_fields=[field])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert getattr(result, field) == pod_modified

    def test_time_to_stop_default_negative_one_from_template(self):
        """Test that template can set time_to_stop_default to -1 (infinite TTL)."""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        # Template sets time_to_stop_default to -1 (no auto-stop)
        template = make_template(time_to_stop_default=-1)
        
        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            
            # Pod uses default 43200, but template should override to -1
            pod = MockPod(time_to_stop_default=43200, modified_fields=[])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.time_to_stop_default == -1, f"Expected -1, got {result.time_to_stop_default}"
    
    def test_time_to_stop_instance_negative_one_from_template(self):
        """Test that template can set time_to_stop_instance to -1 (infinite TTL)."""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        # Template sets time_to_stop_instance to -1 (no auto-stop)
        template = make_template(time_to_stop_instance=-1)
        
        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            
            # Pod has no time_to_stop_instance set (None), template should set to -1
            pod = MockPod(time_to_stop_instance=None, modified_fields=[])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.time_to_stop_instance == -1, f"Expected -1, got {result.time_to_stop_instance}"
    
    def test_pod_overrides_template_negative_one(self):
        """Test that pod can override template's -1 with explicit value."""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        # Template sets time_to_stop_default to -1
        template = make_template(time_to_stop_default=-1)
        
        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            
            # Pod explicitly sets time_to_stop_default to 7200
            pod = MockPod(time_to_stop_default=7200, modified_fields=['time_to_stop_default'])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.time_to_stop_default == 7200, f"Pod override should win, got {result.time_to_stop_default}"

    def test_command_and_arguments_override(self):
        """Test command and arguments (list fields) override correctly"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        template = make_template(command=['python', 'app.py'], arguments=['--port', '8080'])
        
        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            
            # Template values applied when pod not modified
            pod = MockPod(command=None, arguments=None, modified_fields=[])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.command == ['python', 'app.py']
            assert result.arguments == ['--port', '8080']
            
            # Pod modifications preserved
            pod = MockPod(command=['./start.sh'], arguments=['--debug'], 
                         modified_fields=['command', 'arguments'])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.command == ['./start.sh']
            assert result.arguments == ['--debug']


# ============================================================================
# TEST: Resources Merge (Consolidated - was 4+ tests)
# ============================================================================

class TestResourcesMerge:
    """Test resource field merging with priority: pod modified > closer template > deeper template"""
    
    def test_all_resource_fields_priority(self):
        """Comprehensive test of all 7 resource subfields with template chain"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        
        # Deeper template sets all resources
        template2 = make_template(
            template_id="template2",
            resources={'cpu_request': 100, 'cpu_limit': 1000, 'mem_request': 128, 
                      'mem_limit': 1024, 'gpus': 0, 'ephemeral_storage_request': 512,
                      'ephemeral_storage_limit': 1024}
        )
        
        # Closer template overrides some
        template1_def = MockTemplateTagPodDefinition().dict()
        template1_def['template'] = 'template2:latest@2024-01-01'
        template1_def['resources'] = {'cpu_request': 200, 'mem_limit': 2048, 'ephemeral_storage_request': 1024}
        template1 = MockTemplateTag(template_id="template1", pod_definition=template1_def)
        
        def derive_side_effect(name, *args, **kwargs):
            if 'template2' in name:
                return ("template2:latest@2024-01-01", MockTemplate("template2"), template2)
            return ("template1:latest@2024-01-01", MockTemplate("template1"), template1)
        
        with patch('models_templates_utils.derive_template_info', side_effect=derive_side_effect), \
             patch('models_templates_utils.t', mock_t_obj):
            
            # Pod modifies only gpus
            pod = MockPod(resources=MockResources(gpus=2), modified_fields=['resources.gpus'])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            
            # Verify priority order for each field
            assert result.resources['cpu_request'] == 200  # closer template
            assert result.resources['cpu_limit'] == 1000   # deeper template
            assert result.resources['mem_request'] == 128  # deeper template
            assert result.resources['mem_limit'] == 2048   # closer template
            assert result.resources['gpus'] == 2           # pod modified
            assert result.resources['ephemeral_storage_request'] == 1024  # closer template
            assert result.resources['ephemeral_storage_limit'] == 1024    # deeper template


# ============================================================================
# TEST: Networking Merge (Consolidated - was 4 tests)
# ============================================================================

class TestNetworkingMerge:
    """Test networking merge and URL generation"""
    
    def test_networking_merge_and_urls(self):
        """Test networking merge from template and URL generation"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        template = make_template(
            networking={'default': {'protocol': 'http', 'port': 8080, 'tapis_auth': True},
                       'api': {'protocol': 'http', 'port': 3000}}
        )
        
        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            
            # Test template networking applied
            pod = MockPod(pod_id="mypod", networking={"default": {"protocol": "http", "port": 5000}},
                         modified_fields=[])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            
            assert result.networking['default']['port'] == 8080
            assert result.networking['default']['tapis_auth'] == True
            assert result.networking['api']['port'] == 3000
            assert 'mypod.pods.' in result.networking['default']['url']
            assert 'mypod-api.pods.' in result.networking['api']['url']
            
            # Test pod modifications preserved
            pod = MockPod(pod_id="mypod",
                         networking={"default": {"protocol": "http", "port": 9000, "tapis_auth": False}},
                         modified_fields=['networking'])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.networking['default']['port'] == 9000
            assert 'api' in result.networking  # Template's 'api' still present


# ============================================================================
# TEST: Environment Variables Merge (Consolidated - was 4 tests)
# ============================================================================

class TestEnvironmentVariablesMerge:
    """Test environment_variables merge behavior"""
    
    def test_env_vars_merge_behavior(self):
        """Test env vars merge with _TAPIS_INTERNAL_USE_TEMPLATE_ENVS flag"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        template = make_template(
            environment_variables={'DB_HOST': 'localhost', 'DB_PORT': '5432', 'TEMPLATE_VAR': 'from_template'}
        )
        
        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            
            # Default behavior merges (like True)
            pod = MockPod(environment_variables={'MY_VAR': 'my_value'}, modified_fields=[])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.environment_variables['DB_HOST'] == 'localhost'
            assert result.environment_variables['MY_VAR'] == 'my_value'
            
            # Pod override takes precedence when modified
            pod = MockPod(
                environment_variables={'DB_HOST': 'production.example.com', 
                                       '_TAPIS_INTERNAL_USE_TEMPLATE_ENVS': 'True'},
                modified_fields=['environment_variables'])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.environment_variables['DB_HOST'] == 'production.example.com'
            assert result.environment_variables['DB_PORT'] == '5432'


# ============================================================================
# TEST: Volume Mounts Merge (Consolidated - was 3 tests)
# ============================================================================

class TestVolumeMountsMerge:
    """Test volume_mounts merge behavior"""
    
    def test_volume_mounts_merge_behavior(self):
        """Test volume mounts merge with override and removal"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        template = make_template(
            volume_mounts={'/data': {'type': 'tapisvolume', 'source_id': 'datavolume', 'read_only': False},
                          '/config': {'type': 'pvc', 'source_id': 'configvolume', 'read_only': False}}
        )
        
        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            
            # Merge template + pod volumes
            pod = MockPod(
                environment_variables={'_TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES': 'True'},
                volume_mounts={'/mnt/mydata': {'type': 'tapisvolume', 'source_id': 'myvolume'}},
                modified_fields=[]
            )
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert '/data' in result.volume_mounts
            assert '/config' in result.volume_mounts
            assert '/mnt/mydata' in result.volume_mounts
            
            # Pod can override template volume
            pod = MockPod(
                environment_variables={'_TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES': 'True'},
                volume_mounts={'/data': {'type': 'tapisvolume', 'source_id': 'customdata', 'read_only': True}},
                modified_fields=[]
            )
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.volume_mounts['/data']['source_id'] == 'customdata'


# ============================================================================
# TEST: Secret Map Merge (Consolidated - was 4 tests)
# ============================================================================

class TestSecretMapMerge:
    """Test secret_map merge and override behavior"""
    
    def test_secret_map_inheritance_and_override(self):
        """Test secret_map inherited from template and pod overrides"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        template = make_template(
            image='postgres:15',
            secret_map={'DB_PASSWORD': '${:?Database password}', 
                       'DB_HOST': '${pods:default:localhost:?Database host}',
                       'DB_PORT': '${pods:default:5432:?Database port}'}
        )
        
        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            
            # Pod inherits template secrets
            pod = MockPod(secret_map={}, modified_fields=[])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert 'DB_PASSWORD' in result.secret_map
            assert 'DB_HOST' in result.secret_map
            
            # Pod can override template secrets
            pod = MockPod(
                secret_map={'DB_PASSWORD': '${secret:myactualpassword}', 
                           'DB_HOST': 'production.db.example.com'},
                modified_fields=['secret_map']
            )
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            assert result.secret_map['DB_PASSWORD'] == '${secret:myactualpassword}'
            assert result.secret_map['DB_HOST'] == 'production.db.example.com'
            assert result.secret_map['DB_PORT'] == '${pods:default:5432:?Database port}'  # inherited


# ============================================================================
# TEST: Template Chaining (Consolidated - was 4 tests)
# ============================================================================

class TestTemplateChaining:
    """Test recursive template chaining"""
    
    def test_three_level_chain(self):
        """Test template1 -> template2 -> template3 chain"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        
        # template3 (base)
        t3 = make_template(template_id="template3", image='base-image:v1', description='Level 3',
                          command=['/bin/bash'], environment_variables={'LEVEL': '3'},
                          resources={'cpu_request': 100}, time_to_stop_default=3600)
        
        # template2 (middle)
        t2_def = MockTemplateTagPodDefinition().dict()
        t2_def['template'] = 'template3:latest@2024-01-01'
        t2_def['image'] = 'middle-image:v2'
        t2_def['arguments'] = ['--verbose']
        t2_def['environment_variables'] = {'LEVEL': '2', 'MIDDLE_VAR': 'middle'}
        t2_def['resources'] = {'cpu_request': 200}
        t2 = MockTemplateTag(template_id="template2", pod_definition=t2_def)
        
        # template1 (outer)
        t1_def = MockTemplateTagPodDefinition().dict()
        t1_def['template'] = 'template2:latest@2024-01-01'
        t1_def['description'] = 'Level 1'
        t1_def['environment_variables'] = {'LEVEL': '1'}
        t1_def['resources'] = {'cpu_limit': 1000}
        t1_def['compute_queue'] = 'gpu'
        t1 = MockTemplateTag(template_id="template1", pod_definition=t1_def)
        
        def derive_side_effect(name, *args, **kwargs):
            if 'template3' in name:
                return ("template3:latest", MockTemplate("template3"), t3)
            elif 'template2' in name:
                return ("template2:latest", MockTemplate("template2"), t2)
            return ("template1:latest", MockTemplate("template1"), t1)
        
        with patch('models_templates_utils.derive_template_info', side_effect=derive_side_effect), \
             patch('models_templates_utils.t', mock_t_obj):
            
            pod = MockPod(modified_fields=[])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            
            # Test inheritance chain
            assert result.image == 'middle-image:v2'  # template2 overrides template3
            assert result.description == 'Level 1'    # template1 overrides
            assert result.command == ['/bin/bash']    # from template3
            assert result.arguments == ['--verbose']  # from template2
            assert result.environment_variables['LEVEL'] == '1'  # template1 overrides
            assert result.environment_variables['MIDDLE_VAR'] == 'middle'
            assert result.compute_queue == 'gpu'

    def test_pod_overrides_entire_chain(self):
        """Pod's modified fields override entire template chain"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        
        t2 = make_template(template_id="template2", image='template2-image', description='T2 desc',
                          resources={'cpu_request': 500})
        t1_def = MockTemplateTagPodDefinition().dict()
        t1_def['template'] = 'template2:latest'
        t1_def['image'] = 'template1-image'
        t1_def['description'] = 'T1 desc'
        t1_def['resources'] = {'cpu_request': 1000}
        t1 = MockTemplateTag(template_id="template1", pod_definition=t1_def)
        
        def derive_side_effect(name, *args, **kwargs):
            if 'template2' in name:
                return ("template2:latest", MockTemplate("template2"), t2)
            return ("template1:latest", MockTemplate("template1"), t1)
        
        with patch('models_templates_utils.derive_template_info', side_effect=derive_side_effect), \
             patch('models_templates_utils.t', mock_t_obj):
            
            pod = MockPod(image='my-custom-image', description='My pod desc',
                         resources=MockResources(cpu_request=2000),
                         modified_fields=['image', 'description', 'resources.cpu_request'])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            
            assert result.image == 'my-custom-image'
            assert result.description == 'My pod desc'
            assert result.resources['cpu_request'] == 2000


# ============================================================================
# TEST: Infinite Loop Detection (Consolidated - was 3 tests)
# ============================================================================

class TestInfiniteLoopDetection:
    """Test circular reference detection"""
    
    @pytest.mark.parametrize("chain_type", ["self", "two_level", "three_level"])
    def test_circular_references_detected(self, chain_type):
        """Test that circular template references raise ValueError"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        
        if chain_type == "self":
            # Self-reference: A -> A
            t_def = MockTemplateTagPodDefinition().dict()
            t_def['template'] = 'template1:latest'
            t1 = MockTemplateTag(template_id="template1", pod_definition=t_def)
            def derive(name, *a, **kw):
                return ("template1:latest", MockTemplate("template1"), t1)
        elif chain_type == "two_level":
            # A -> B -> A
            ta_def = MockTemplateTagPodDefinition().dict()
            ta_def['template'] = 'template_b:latest'
            ta = MockTemplateTag(template_id="template_a", pod_definition=ta_def)
            tb_def = MockTemplateTagPodDefinition().dict()
            tb_def['template'] = 'template_a:latest'
            tb = MockTemplateTag(template_id="template_b", pod_definition=tb_def)
            def derive(name, *a, **kw):
                if 'template_b' in name:
                    return ("template_b:latest", MockTemplate("template_b"), tb)
                return ("template_a:latest", MockTemplate("template_a"), ta)
        else:  # three_level
            # A -> B -> C -> A
            tc_def = MockTemplateTagPodDefinition().dict()
            tc_def['template'] = 'template_a:latest'
            tc = MockTemplateTag(template_id="template_c", pod_definition=tc_def)
            tb_def = MockTemplateTagPodDefinition().dict()
            tb_def['template'] = 'template_c:latest'
            tb = MockTemplateTag(template_id="template_b", pod_definition=tb_def)
            ta_def = MockTemplateTagPodDefinition().dict()
            ta_def['template'] = 'template_b:latest'
            ta = MockTemplateTag(template_id="template_a", pod_definition=ta_def)
            def derive(name, *a, **kw):
                if 'template_c' in name:
                    return ("template_c:latest", MockTemplate("template_c"), tc)
                elif 'template_b' in name:
                    return ("template_b:latest", MockTemplate("template_b"), tb)
                return ("template_a:latest", MockTemplate("template_a"), ta)
        
        with patch('models_templates_utils.derive_template_info', side_effect=derive), \
             patch('models_templates_utils.t', mock_t_obj):
            with pytest.raises(ValueError, match="Infinite loop detected"):
                combine_pod_and_template_recursively(MockPod(), 
                    "template1" if chain_type == "self" else "template_a",
                    tenant="dev", site="tacc")


# ============================================================================
# TEST: No Template Case
# ============================================================================

class TestNoTemplate:
    """Test behavior when no template specified"""
    
    def test_no_template_returns_unchanged(self):
        """When template_name is None/empty, pod is unchanged"""
        from models_templates_utils import combine_pod_and_template_recursively
        
        pod = MockPod(image="my-image:v1", description="My description")
        
        result = combine_pod_and_template_recursively(pod, None, tenant="dev", site="tacc")
        assert result.image == "my-image:v1"
        assert result.description == "My description"
        
        result = combine_pod_and_template_recursively(pod, "", tenant="dev", site="tacc")
        assert result.image == "my-image:v1"


# ============================================================================
# TEST: Template Overrides (Consolidated - was 20+ tests)
# ============================================================================

from models_volume_mounts_utils import TemplateOverrides
from models_templates_utils import apply_template_overrides


class TestTemplateOverridesModel:
    """Test TemplateOverrides Pydantic model validation"""
    
    def test_valid_overrides(self):
        """Test various valid override configurations"""
        # Valid volume mount override
        o = TemplateOverrides(volume_mounts={"/data": {"source_id": "my-volume"}})
        assert o.volume_mounts["/data"]["source_id"] == "my-volume"
        
        # Valid secret_map override  
        o = TemplateOverrides(secret_map={"DB_PASS": "${secret:my-pass}"})
        assert o.secret_map["DB_PASS"] == "${secret:my-pass}"
        
        # Multiple fields in single mount
        o = TemplateOverrides(volume_mounts={
            "/data": {"source_id": "vol", "read_only": True, "sub_path": "subdir"}
        })
        assert o.volume_mounts["/data"]["read_only"] == True
    
    @pytest.mark.parametrize("invalid_input,error_pattern", [
        ({"volume_mounts": {"data": {"source_id": "x"}}}, "must be a mount_path starting with '/'"),
        ({"volume_mounts": {"/x": {"source_id": "MyVol"}}}, "must be lowercase alphanumeric"),
        ({"volume_mounts": {"/x": {"source_id": "123vol"}}}, "must be lowercase alphanumeric"),
        ({"volume_mounts": {"/x": {"type": "invalid"}}}, "must be one of"),
        ({"volume_mounts": {"/x": {"read_only": "yes"}}}, "must be boolean"),
        ({"volume_mounts": {"/x": {"config_permissions": "999"}}}, "must be valid octal"),
        ({"volume_mounts": {"/x": {"config_filename": "sub/file"}}}, "cannot contain path separators"),
        ({"volume_mounts": {"/x": {"config_update_mode": "never"}}}, "must be one of"),
        ({"volume_mounts": {"/x": {"invalid_field": "val"}}}, "not a valid VolumeMount field"),
    ])
    def test_invalid_overrides_rejected(self, invalid_input, error_pattern):
        """Test that invalid overrides are properly rejected"""
        with pytest.raises(ValueError, match=error_pattern):
            TemplateOverrides(**invalid_input)


class TestApplyTemplateOverrides:
    """Test apply_template_overrides function"""
    
    def test_volume_mount_overrides(self):
        """Test applying volume mount overrides preserves other fields"""
        volume_mounts = {
            "/data": {"type": "tapisvolume", "source_id": "template-vol", 
                     "read_only": False, "sub_path": "subdir"}
        }
        overrides = {"volume_mounts": {"/data": {"source_id": "my-vol"}}}
        
        result_vm, result_sm, warnings = apply_template_overrides(volume_mounts, {}, overrides)
        
        assert result_vm["/data"]["source_id"] == "my-vol"
        assert result_vm["/data"]["type"] == "tapisvolume"  # preserved
        assert result_vm["/data"]["read_only"] == False     # preserved
        assert result_vm["/data"]["sub_path"] == "subdir"   # preserved
    
    def test_secret_map_overrides(self):
        """Test applying secret_map overrides"""
        secret_map = {"DB_PASS": "${:?Required}", "DB_HOST": "${pods:default:localhost}"}
        overrides = {"secret_map": {"DB_PASS": "${secret:my-pass}"}}
        
        result_vm, result_sm, warnings = apply_template_overrides({}, secret_map, overrides)
        
        assert result_sm["DB_PASS"] == "${secret:my-pass}"
        assert result_sm["DB_HOST"] == "${pods:default:localhost}"  # unchanged
    
    def test_nonexistent_paths_warn(self):
        """Test warnings for non-existent mount paths or secret keys"""
        vm, sm, warnings = apply_template_overrides(
            {"/data": {"type": "tapisvolume", "source_id": "x"}},
            {"EXISTING": "val"},
            {"volume_mounts": {"/nonexistent": {"source_id": "y"}},
             "secret_map": {"NEW_KEY": "newval"}}
        )
        assert any("/nonexistent" in w for w in warnings)
        assert any("NEW_KEY" in w for w in warnings)
    
    def test_does_not_mutate_originals(self):
        """Test that original dicts are not mutated"""
        original_vm = {"/data": {"type": "tapisvolume", "source_id": "orig"}}
        original_sm = {"KEY": "orig"}
        
        result_vm, result_sm, _ = apply_template_overrides(
            original_vm, original_sm,
            {"volume_mounts": {"/data": {"source_id": "new"}}, "secret_map": {"KEY": "new"}}
        )
        
        assert original_vm["/data"]["source_id"] == "orig"
        assert original_sm["KEY"] == "orig"
        assert result_vm["/data"]["source_id"] == "new"
        assert result_sm["KEY"] == "new"

    def test_all_volume_mount_fields_can_be_overridden(self):
        """Test that all VolumeMount fields can be individually overridden"""
        volume_mounts = {
            "/data": {
                "type": "tapisvolume", "source_id": "orig", "sub_path": "orig",
                "read_only": False, "config_content": "orig", "config_permissions": "0644",
                "config_filename": "orig.conf", "config_update_mode": "always"
            }
        }
        overrides = {"volume_mounts": {"/data": {
            "type": "tapissnapshot", "source_id": "new", "sub_path": "new",
            "read_only": True, "config_content": "new", "config_permissions": "0600",
            "config_filename": "new.conf", "config_update_mode": "once"
        }}}
        
        result_vm, _, _ = apply_template_overrides(volume_mounts, {}, overrides)
        
        assert result_vm["/data"]["type"] == "tapissnapshot"
        assert result_vm["/data"]["source_id"] == "new"
        assert result_vm["/data"]["sub_path"] == "new"
        assert result_vm["/data"]["read_only"] == True
        assert result_vm["/data"]["config_content"] == "new"
        assert result_vm["/data"]["config_permissions"] == "0600"
        assert result_vm["/data"]["config_filename"] == "new.conf"
        assert result_vm["/data"]["config_update_mode"] == "once"


class TestTemplateOverridesScenarios:
    """Test realistic usage scenarios"""
    
    def test_ml_training_template_scenario(self):
        """ML template: user overrides training data volume and S3 credentials"""
        volume_mounts = {
            "/training_data": {"type": "tapisvolume", "source_id": "shared-training", "read_only": True},
            "/models": {"type": "tapisvolume", "source_id": "shared-models"},
            "/outputs": {"type": "tapisvolume", "source_id": "shared-outputs"}
        }
        secret_map = {"S3_ACCESS_KEY": "${:?Required}", "S3_SECRET_KEY": "${:?Required}"}
        overrides = {
            "volume_mounts": {
                "/training_data": {"source_id": "my-training-data"},
                "/outputs": {"source_id": "my-outputs"}
            },
            "secret_map": {
                "S3_ACCESS_KEY": "${secret:my-s3-access}",
                "S3_SECRET_KEY": "${secret:my-s3-secret}"
            }
        }
        
        result_vm, result_sm, _ = apply_template_overrides(volume_mounts, secret_map, overrides)
        
        assert result_vm["/training_data"]["source_id"] == "my-training-data"
        assert result_vm["/training_data"]["read_only"] == True  # preserved
        assert result_vm["/outputs"]["source_id"] == "my-outputs"
        assert result_vm["/models"]["source_id"] == "shared-models"  # unchanged
        assert result_sm["S3_ACCESS_KEY"] == "${secret:my-s3-access}"


# ============================================================================
# TEST: Dotted resources.<field> override (regression: NameError pre-fix)
# ============================================================================

class TestDottedResourcesOverride:
    """A template whose pod_definition carries a dotted 'resources.<field>' key
    (the shape save-as-template writes for single-field overrides) must merge
    that field — the pre-fix code hit an undefined variable and crashed."""

    def test_dotted_resources_key_applies_without_crashing(self):
        from models_templates_utils import combine_pod_and_template_recursively

        mock_t_obj = MagicMock()
        mock_t_obj.tenant_cache = MockTenantCache()
        res = MockResources(gpus=1).dict()
        template = make_template(**{"resources": res, "resources.gpus": 1})

        with patch('models_templates_utils.derive_template_info') as mock_derive, \
             patch('models_templates_utils.t', mock_t_obj):
            mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
            pod = MockPod(modified_fields=[])
            result = combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")
            gpus = result.resources.gpus if hasattr(result.resources, "gpus") else result.resources["gpus"]
            assert gpus == 1
