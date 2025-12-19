"""
Volume mount validation utilities for object-based volume_mounts structure.

The volume_mounts field is a Dict[str, VolumeMount | None] where:
- Keys are mount_path strings
- Values are VolumeMount objects or None (to remove inherited mounts)
"""
from typing import Dict, Optional, Any, List
from pydantic import Field, field_validator, model_validator
import re
import hashlib

# Import base model - use try/except to handle potential import issues
try:
    from models_base import TapisModel
except ImportError:
    from service.models_base import TapisModel


# Valid volume mount types
VALID_VOLUME_MOUNT_TYPES = {"tapisvolume", "tapissnapshot", "ephemeral", "pvc"}

# Maximum config content size (1MB)
MAX_CONFIG_CONTENT_SIZE = 1024 * 1024  # 1MB


def interpolate_config_content(content: str, secret_map: Dict[str, str], fail_on_missing: bool = True) -> str:
    """
    Interpolate ${pods:secrets:KEY} placeholders in config content using secret_map values.
    
    Args:
        content: Config content with placeholders
        secret_map: Dict mapping keys to their resolved secret values
        fail_on_missing: If True, raise ValueError for missing keys. If False, leave placeholder.
        
    Returns:
        Interpolated config content string
    """
    if content is None:
        return ""
    
    result = content
    pattern = r'\$\{pods:secrets:([^}]+)\}'
    matches = re.findall(pattern, result)
    
    for key in matches:
        if key in secret_map:
            result = result.replace(f"${{pods:secrets:{key}}}", str(secret_map[key]))
        elif fail_on_missing:
            raise ValueError(f"Config interpolation failed: secret_map key '{key}' not found.")
    
    return result


def interpolate_legacy_secrets(content: str, pods_env: Dict[str, str]) -> str:
    """
    Interpolate legacy secret placeholders <<TAPIS_*>> and <<tapissecret_*>> in content.
    
    This is used for environment variables, command, arguments, and config_content
    interpolation in kubernetes_templates.py.
    
    Args:
        content: String content with legacy placeholders
        pods_env: Dict mapping secret keys to their values
        
    Returns:
        Interpolated content string

    TODO - Delete
    """
    if content is None or not pods_env:
        return content or ""
    
    result = content
    
    # Find and replace <<TAPIS_*>> patterns
    tapis_matches = re.findall(r'<<TAPIS_(.*?)>>', result)
    for match in tapis_matches:
        result = result.replace(f"<<TAPIS_{match}>>", pods_env.get(match, ""))
    
    # Find and replace <<tapissecret_*>> patterns
    tapissecret_matches = re.findall(r'<<tapissecret_(.*?)>>', result)
    for match in tapissecret_matches:
        result = result.replace(f"<<tapissecret_{match}>>", pods_env.get(match, ""))
    
    return result


def convert_legacy_volume_mounts_list(volume_mounts: Any) -> Dict[str, Any]:
    """
    Convert legacy list-based volume_mounts format to dict format.
    
    Legacy format: [{"mount_path": "/path", "type": "tapisvolume", ...}]
    New format: {"/path": {"type": "tapisvolume", ...}}
    
    Args:
        volume_mounts: List of volume mount dicts (legacy format)
        
    Returns:
        Dict of mount_path -> VolumeMount config (new format)

    TODO - Delete
    """
    if not isinstance(volume_mounts, list):
        raise ValueError("Expected a list for legacy volume_mounts format")
    
    result = {}
    for mount in volume_mounts:
        if not isinstance(mount, dict):
            raise ValueError(f"Each volume mount must be a dict. Got: {type(mount)}")
        
        mount_path = mount.get("mount_path")
        if not mount_path:
            raise ValueError("Legacy volume mount missing 'mount_path'")
        
        # Create new dict without mount_path key
        mount_config = {k: v for k, v in mount.items() if k != "mount_path"}
        result[mount_path] = mount_config
    
    return result


def validate_and_convert_volume_mounts(volume_mounts: Any, use_full_validation: bool = True) -> Dict[str, Any]:
    """
    Validate and convert volume_mounts from any format to normalized dict format.
    
    This handles:
    1. None -> empty dict
    2. Legacy list format -> dict format (with conversion)
    3. Dict format -> validated dict (using VolumeMount class for full validation)
    
    This function should be used by model validators to ensure consistent validation
    across pods and templates.
    
    Args:
        volume_mounts: Volume mounts in any format (None, list, or dict)
        use_full_validation: If True, use VolumeMount class for full field validation.
                            If False, use basic validate_volume_mount_entry.
        
    Returns:
        Validated dict of mount_path -> VolumeMount config
        
    Raises:
        ValueError: If validation fails
    """
    if volume_mounts is None:
        return {}
    
    # Handle legacy list format
    if isinstance(volume_mounts, list):
        volume_mounts = convert_legacy_volume_mounts_list(volume_mounts)
    
    if not isinstance(volume_mounts, dict):
        raise ValueError("volume_mounts must be a dict (object keyed by mount_path)")
    
    validated = {}
    for mount_path, mount_config in volume_mounts.items():
        # Validate mount_path
        validate_mount_path(mount_path)
        
        # None is valid (removes inherited mount)
        if mount_config is None:
            validated[mount_path] = None
            continue
        
        # Convert various object types to dict
        if hasattr(mount_config, 'model_dump'):
            mount_config = mount_config.model_dump()
        elif hasattr(mount_config, 'dict'):
            mount_config = mount_config.dict()
        
        if not isinstance(mount_config, dict):
            raise ValueError(f"Volume mount config at '{mount_path}' must be a dict or None")
        
        # Use VolumeMount class for full validation (validates all fields)
        if use_full_validation:
            try:
                vm = VolumeMount(**mount_config)
                validated[mount_path] = vm.model_dump(exclude_none=True)
            except Exception as e:
                raise ValueError(f"Invalid volume mount at '{mount_path}': {e}")
        else:
            # Basic validation only
            validated[mount_path] = validate_volume_mount_entry(mount_path, mount_config)
    
    return validated


class VolumeMount(TapisModel):
    """
    Volume mount configuration for attaching volumes, snapshots, or inline configs to pods.
    
    Object-keyed structure (v3) - mount_path is the key in volume_mounts dict:
    - type: One of 'tapisvolume', 'tapissnapshot', 'ephemeral', 'pvc'
    - source_id: Required for tapisvolume, tapissnapshot, pvc. Not used for ephemeral.
    - sub_path: Optional sub-path within the source to mount (not applicable for ephemeral)
    - read_only: Whether mount is read-only (default varies by type)
    - config_content: For ephemeral type, the inline config file content (max 1MB)
    - config_permissions: For ephemeral type, Unix file permissions (default 0644)
    """
    # Required field
    type: str = Field(..., description="Type of mount: 'tapisvolume', 'tapissnapshot', 'ephemeral', or 'pvc'.")
    
    # Required for storage types, not used for ephemeral
    source_id: Optional[str] = Field(None, description="ID of the volume, snapshot, or PVC to mount. Required for tapisvolume/tapissnapshot/pvc.")
    
    # Optional fields for storage types
    sub_path: str = Field("", description="Sub-path within the source volume/snapshot to mount. Not used for ephemeral.")
    read_only: Optional[bool] = Field(None, description="If true, mount will be read-only. Default: False for volumes/pvc, True for snapshots/ephemeral.")
    
    # Config fields (used for ephemeral and tapisvolume with inline config)
    config_content: Optional[str] = Field(None, description="Config file content. For ephemeral: mounted as ConfigMap. For tapisvolume: written to NFS. Supports ${pods:secrets:KEY} interpolation. Max 1MB.")
    config_permissions: str = Field("0644", description="Unix file permissions for config file (e.g., '0644', '0600').")
    config_filename: Optional[str] = Field(None, description="Filename for config file when using tapisvolume with config_content. Defaults to basename of mount_path.")
    config_update_mode: str = Field("always", description="Config update behavior: 'always' recreates config on each pod start, 'once' only creates if file/ConfigMap doesn't exist.")

    @field_validator('type')
    @classmethod
    def check_type(cls, v):
        v = v.lower()
        valid_types = list(VALID_VOLUME_MOUNT_TYPES)
        if v not in valid_types:
            raise ValueError(f"volume_mounts type must be one of: {valid_types}. Got: '{v}'")
        return v

    @field_validator('source_id')
    @classmethod
    def check_source_id(cls, v):
        if v is not None and v != "":
            # Regex match to ensure a-z0-9
            res = re.fullmatch(r'[a-z][a-z0-9\-]+', v)
            if not res:
                raise ValueError(f"volume_mounts source_id must be lowercase alphanumeric (hyphens allowed). First character must be alpha. Got: {v}")
        return v

    @field_validator('config_content')
    @classmethod
    def check_config_content(cls, v):
        if v is not None:
            # Limit config content to 1MB
            if len(v) > MAX_CONFIG_CONTENT_SIZE:
                raise ValueError(f"volume_mounts config_content must be less than 1MB. Got: {len(v)} bytes")
            # Ensure valid UTF-8
            try:
                v.encode('utf-8')
            except UnicodeEncodeError:
                raise ValueError("volume_mounts config_content must be valid UTF-8 text.")
        return v

    @field_validator('config_permissions')
    @classmethod
    def check_config_permissions(cls, v):
        if v:
            # Validate octal permissions format (3 or 4 digits)
            if not re.fullmatch(r'[0-7]{3,4}', v):
                raise ValueError(f"volume_mounts config_permissions must be valid octal (e.g., '0644', '755'). Got: {v}")
        return v

    @field_validator('config_update_mode')
    @classmethod
    def check_config_update_mode(cls, v):
        v = v.lower()
        valid_modes = ['always', 'once']
        if v not in valid_modes:
            raise ValueError(f"volume_mounts config_update_mode must be one of: {valid_modes}. Got: '{v}'")
        return v

    @field_validator('config_filename')
    @classmethod
    def check_config_filename(cls, v):
        if v is not None:
            # Ensure filename is safe (no path traversal)
            if '/' in v or '\\' in v or '..' in v:
                raise ValueError(f"config_filename cannot contain path separators or '..'. Got: {v}")
            if len(v) > 255:
                raise ValueError(f"config_filename must be less than 255 characters. Got: {len(v)}")
            if not re.fullmatch(r'[a-zA-Z0-9._-]+', v):
                raise ValueError(f"config_filename can only contain alphanumeric characters, dots, underscores, and hyphens. Got: {v}")
        return v

    @model_validator(mode="after")
    def validate_type_requirements(cls, values):
        """Validate fields based on type."""
        vol_type = getattr(values, 'type', None)
        source_id = getattr(values, 'source_id', None)
        config_content = getattr(values, 'config_content', None)
        read_only = getattr(values, 'read_only', None)
        
        if vol_type == 'ephemeral':
            # Ephemeral requires config_content
            if not config_content:
                raise ValueError("volume_mounts type 'ephemeral' requires config_content to be set.")
            # source_id not used for ephemeral
            if source_id:
                raise ValueError("volume_mounts type 'ephemeral' should not have source_id (config is inline).")
            # Default read_only to True for ephemeral
            if read_only is None:
                object.__setattr__(values, 'read_only', True)
        elif vol_type == 'tapisvolume':
            # tapisvolume requires source_id
            if not source_id:
                raise ValueError(f"volume_mounts type 'tapisvolume' requires source_id.")
            # tapisvolume CAN have config_content (will be written to NFS)
            # Default read_only to False for tapisvolume
            if read_only is None:
                object.__setattr__(values, 'read_only', False)
        else:
            # Other storage types (tapissnapshot, pvc) require source_id, no config_content
            if not source_id:
                raise ValueError(f"volume_mounts type '{vol_type}' requires source_id.")
            if config_content:
                raise ValueError(f"volume_mounts type '{vol_type}' does not support config_content. Use 'ephemeral' or 'tapisvolume' for configs.")
            # Default read_only based on type
            if read_only is None:
                if vol_type == 'tapissnapshot':
                    object.__setattr__(values, 'read_only', True)
                else:
                    object.__setattr__(values, 'read_only', False)
        
        return values
    
    def get_k8_name_hash(self, mount_path: str) -> str:
        """Generate a hash-based name suffix from mount_path for Kubernetes naming."""
        return hashlib.md5(mount_path.encode()).hexdigest()[:8]

    def interpolate_secrets(self, secret_map: Dict[str, str], fail_on_missing: bool = True) -> str:
        """
        Interpolate ${pods:secrets:KEY} placeholders in config_content using secret_map values.
        
        Args:
            secret_map: Dict mapping keys to their resolved secret values
            fail_on_missing: If True, raise ValueError for missing keys. If False, leave placeholder.
            
        Returns:
            Interpolated config content string
        """
        if self.config_content is None:
            raise ValueError("VolumeMount does not have config_content set.")
        
        return interpolate_config_content(self.config_content, secret_map, fail_on_missing)


def validate_mount_path(mount_path: str) -> None:
    """Validate a mount_path string."""
    if not mount_path:
        raise ValueError("mount_path cannot be empty")
    
    if not mount_path.startswith("/"):
        raise ValueError(f"mount_path must be an absolute path starting with '/': {mount_path}")
    
    # Basic validation for path characters
    if ".." in mount_path:
        raise ValueError(f"mount_path cannot contain '..': {mount_path}")


def validate_permissions_format(permissions: str) -> None:
    """Validate that permissions string is a valid octal format."""
    if not re.match(r'^[0-7]{3,4}$', permissions):
        raise ValueError(f"config_permissions must be octal format (e.g., '0644' or '644'): {permissions}")


def validate_volume_mount_entry(mount_path: str, mount_config: Any) -> Dict[str, Any]:
    """
    Validate a single volume mount entry.
    
    Args:
        mount_path: The mount path (key in volume_mounts dict)
        mount_config: The volume mount configuration (dict, VolumeMount, or None)
        
    Returns:
        Validated mount configuration dict or None
    """
    validate_mount_path(mount_path)
    
    # None is valid - it removes inherited mount
    if mount_config is None:
        return None
    
    # Convert VolumeMount object to dict if needed
    if hasattr(mount_config, 'dict'):
        mount_config = mount_config.dict()
    elif hasattr(mount_config, 'model_dump'):
        mount_config = mount_config.model_dump()
    
    if not isinstance(mount_config, dict):
        raise ValueError(f"Volume mount config must be a dict or None: {mount_path}")
    
    mount_type = mount_config.get("type")
    if not mount_type:
        raise ValueError(f"Volume mount at '{mount_path}' must specify a 'type'")
    
    if mount_type not in VALID_VOLUME_MOUNT_TYPES:
        raise ValueError(f"Invalid volume mount type '{mount_type}' at '{mount_path}'. Valid types: {VALID_VOLUME_MOUNT_TYPES}")
    
    # Type-specific validation
    if mount_type == "ephemeral":
        # Ephemeral requires config_content
        config_content = mount_config.get("config_content")
        if not config_content:
            raise ValueError(f"Ephemeral volume mount at '{mount_path}' must specify 'config_content'")
        
        # Check size limit
        if len(config_content.encode('utf-8')) > MAX_CONFIG_CONTENT_SIZE:
            raise ValueError(f"config_content at '{mount_path}' exceeds maximum size of 1MB")
        
        # source_id should not be set for ephemeral
        if mount_config.get("source_id"):
            raise ValueError(f"Ephemeral volume mount at '{mount_path}' should not have 'source_id'")
            
        # Validate permissions format if provided
        permissions = mount_config.get("config_permissions", "0644")
        validate_permissions_format(permissions)
        
    elif mount_type == "tapisvolume":
        # tapisvolume requires source_id
        source_id = mount_config.get("source_id")
        if not source_id:
            raise ValueError(f"Volume mount type 'tapisvolume' at '{mount_path}' must specify 'source_id'")
        
        # tapisvolume CAN have config_content (will be written to NFS)
        config_content = mount_config.get("config_content")
        if config_content:
            # Validate config_content size
            if len(config_content.encode('utf-8')) > MAX_CONFIG_CONTENT_SIZE:
                raise ValueError(f"config_content at '{mount_path}' exceeds maximum size of 1MB")
            # Validate permissions format if provided
            permissions = mount_config.get("config_permissions", "0644")
            validate_permissions_format(permissions)
            
    elif mount_type == "tapissnapshot":
        # tapissnapshot requires source_id, no config_content
        source_id = mount_config.get("source_id")
        if not source_id:
            raise ValueError(f"Volume mount type 'tapissnapshot' at '{mount_path}' must specify 'source_id'")
        
        # config_content should not be set for snapshots
        if mount_config.get("config_content"):
            raise ValueError(f"Volume mount type 'tapissnapshot' at '{mount_path}' should not have 'config_content'. Snapshots are read-only.")
            
    elif mount_type == "pvc":
        # PVC requires source_id (the PVC name)
        source_id = mount_config.get("source_id")
        if not source_id:
            raise ValueError(f"PVC volume mount at '{mount_path}' must specify 'source_id' (PVC name)")
        
        # config_content should not be set for PVC
        if mount_config.get("config_content"):
            raise ValueError(f"PVC volume mount at '{mount_path}' should not have 'config_content'")
    
    return mount_config


def validate_volume_mounts_dict(volume_mounts: Any) -> Dict[str, Any]:
    """
    Validate and normalize a volume_mounts dict.
    
    Args:
        volume_mounts: Dict of mount_path -> VolumeMount config
        
    Returns:
        Validated dict of volume mounts
    """
    if volume_mounts is None:
        return {}
    
    if not isinstance(volume_mounts, dict):
        raise ValueError("volume_mounts must be a dict (object keyed by mount_path)")
    
    validated = {}
    for mount_path, mount_config in volume_mounts.items():
        validated[mount_path] = validate_volume_mount_entry(mount_path, mount_config)
    
    return validated


class VolumeMountValidationResult:
    """Result object for volume mount validation."""
    def __init__(self, is_valid: bool = True, errors: List[str] = None, warnings: List[str] = None, metadata: Dict[str, Any] = None):
        self.is_valid = is_valid
        self.errors = errors or []
        self.warnings = warnings or []
        self.metadata = metadata or {}
    
    @property
    def error_message(self) -> str:
        return "; ".join(self.errors) if self.errors else ""


def validate_volume_mounts_permissions(
    volume_mounts: Dict[str, Any],
    user: str = None,
    tenant: str = None,
    site: str = None,
    roles: List[str] = None,
    template_source: str = None,
    is_blocking: bool = True
) -> VolumeMountValidationResult:
    """
    Validate that user has permission to use the volumes/snapshots in volume_mounts.
    
    Args:
        volume_mounts: Dict of mount_path -> VolumeMount config
        user: Username making the request
        tenant: The tenant ID
        site: The site ID
        roles: User's roles
        template_source: Source template for error messages
        is_blocking: If True, permission failures are errors. If False, they're warnings.
        
    Returns:
        VolumeMountValidationResult with is_valid, errors, warnings
    """
    result = VolumeMountValidationResult()
    
    if not volume_mounts:
        return result
    
    # Import here to avoid circular imports
    from models_volumes import Volume
    from models_snapshots import Snapshot
    
    for mount_path, mount_config in volume_mounts.items():
        if mount_config is None:
            continue
        
        mount_type = mount_config.get("type") if isinstance(mount_config, dict) else getattr(mount_config, "type", None)
        source_id = mount_config.get("source_id") if isinstance(mount_config, dict) else getattr(mount_config, "source_id", None)
        
        if mount_type == "tapisvolume" and source_id:
            try:
                volume = Volume.db_get_with_pk(source_id, tenant=tenant, site=site)
                if not volume:
                    msg = f"Volume '{source_id}' not found at mount path '{mount_path}'"
                    if template_source:
                        msg += f" (from template {template_source})"
                    if is_blocking:
                        result.errors.append(msg)
                        result.is_valid = False
                    else:
                        result.warnings.append(msg)
            except Exception as e:
                msg = f"Error checking volume '{source_id}': {e}"
                result.warnings.append(msg)
                
        elif mount_type == "tapissnapshot" and source_id:
            try:
                snapshot = Snapshot.db_get_with_pk(source_id, tenant=tenant, site=site)
                if not snapshot:
                    msg = f"Snapshot '{source_id}' not found at mount path '{mount_path}'"
                    if template_source:
                        msg += f" (from template {template_source})"
                    if is_blocking:
                        result.errors.append(msg)
                        result.is_valid = False
                    else:
                        result.warnings.append(msg)
            except Exception as e:
                msg = f"Error checking snapshot '{source_id}': {e}"
                result.warnings.append(msg)
        
        # ephemeral and pvc don't need permission checks on external resources
    
    return result


def get_template_merged_volume_mounts(
    template_name: str,
    tenant: str = None,
    site: str = None
) -> Dict[str, Any]:
    """
    Get the merged volume_mounts from a template and all its chained templates.
    
    This fetches the template tag and returns the volume_mounts defined in
    the pod_definition, following template chains if necessary.
    
    Args:
        template_name: Template name in format "template_id:tag" or "template_id"
        tenant: Tenant ID
        site: Site ID
        
    Returns:
        Dict of merged volume_mounts from template chain
    """
    from models_templates_utils import derive_template_info
    
    if not template_name:
        return {}
    
    try:
        # derive_template_info returns (template_name_str, template, template_tag)
        _, _, template_tag = derive_template_info(template_name, tenant=tenant, site=site)
        
        if template_tag and template_tag.pod_definition:
            pod_def = template_tag.pod_definition
            if hasattr(pod_def, 'dict'):
                pod_def = pod_def.dict() if hasattr(pod_def, 'dict') else dict(pod_def)
            elif hasattr(pod_def, 'model_dump'):
                pod_def = pod_def.model_dump()
            
            volume_mounts = pod_def.get('volume_mounts', {})
            if isinstance(volume_mounts, dict):
                return volume_mounts
            # Handle legacy list format by converting
            elif isinstance(volume_mounts, list):
                return {vm.get('mount_path', f'/mount_{i}'): vm for i, vm in enumerate(volume_mounts)}
        
        return {}
    except Exception as e:
        # Log but don't fail - template may not exist yet
        return {}


def validate_pod_volume_mounts_against_template(
    pod_mounts: Optional[Dict[str, Any]],
    template_mounts: Optional[Dict[str, Any]] = None,
    user: str = None,
    tenant: str = None,
    site: str = None,
    roles: List[str] = None
) -> VolumeMountValidationResult:
    """
    Validate pod volume mounts against template and check permissions.
    
    This validates that:
    1. Pod's volume_mounts are properly structured
    2. Pod creator has access to any referenced volumes/snapshots
    
    Args:
        pod_mounts: Pod's volume_mounts dict
        template_mounts: Template's volume_mounts dict (for reference)
        user: Username of pod creator
        tenant: Tenant ID
        site: Site ID
        roles: User's roles
        
    Returns:
        VolumeMountValidationResult with validation status and any metadata
    """
    result = VolumeMountValidationResult()
    pod_mounts = pod_mounts or {}
    
    # First do basic structure validation
    try:
        validated = validate_volume_mounts_dict(pod_mounts)
    except Exception as e:
        result.is_valid = False
        result.errors.append(str(e))
        return result
    
    # Then check permissions if user context provided
    if user and tenant and site:
        perm_result = validate_volume_mounts_permissions(
            validated,
            user=user,
            tenant=tenant,
            site=site,
            roles=roles,
            is_blocking=True  # Block on permission errors for pod creation
        )
        if not perm_result.is_valid:
            result.is_valid = False
            result.errors.extend(perm_result.errors)
        result.warnings.extend(perm_result.warnings)
        if perm_result.metadata:
            result.metadata.update(perm_result.metadata)
    
    return result


def validate_template_volume_mounts(
    volume_mounts: Optional[Dict[str, Any]],
    user: str = None,
    tenant: str = None,
    site: str = None,
    roles: List[str] = None,
    is_template_creation: bool = False
) -> VolumeMountValidationResult:
    """
    Validate volume mounts for a template, including permission checks.
    
    Templates have the same validation as pods, plus verifies the creator
    has access to any referenced volumes/snapshots.
    
    Args:
        volume_mounts: Template's volume_mounts dict
        user: Username of template creator
        tenant: Tenant ID
        site: Site ID
        roles: User's roles
        is_template_creation: If True, permission errors are blocking
        
    Returns:
        VolumeMountValidationResult with validation status and any metadata
    """
    result = VolumeMountValidationResult()
    
    # First do basic structure validation
    try:
        validated = validate_volume_mounts_dict(volume_mounts)
    except Exception as e:
        result.is_valid = False
        result.errors.append(str(e))
        return result
    
    # Then check permissions if user context provided
    if user and tenant and site:
        perm_result = validate_volume_mounts_permissions(
            validated,
            user=user,
            tenant=tenant,
            site=site,
            roles=roles,
            is_blocking=is_template_creation
        )
        if not perm_result.is_valid:
            result.is_valid = False
            result.errors.extend(perm_result.errors)
        result.warnings.extend(perm_result.warnings)
    
    return result


def merge_pod_volume_mounts_with_template(
    pod_mounts: Optional[Dict[str, Any]],
    template_mounts: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Merge pod volume mounts with template defaults.
    
    Pod mounts override template mounts. A None value in pod_mounts
    removes the mount inherited from template.
    
    Args:
        pod_mounts: Pod's volume_mounts (overrides)
        template_mounts: Template's default volume_mounts
        
    Returns:
        Final merged volume mounts for the pod
    """
    if not template_mounts and not pod_mounts:
        return {}
    
    # Start with template defaults
    merged = dict(template_mounts) if template_mounts else {}
    
    # Apply pod overrides
    if pod_mounts:
        for mount_path, mount_config in pod_mounts.items():
            if mount_config is None:
                # None removes inherited mount
                merged.pop(mount_path, None)
            else:
                # Override or add
                merged[mount_path] = mount_config
    
    return merged


class TemplateOverrides(TapisModel):
    """
    Partial overrides to apply to template-inherited values without rewriting entire configs.
    
    Use case: A template defines volume_mounts with complex config. User wants to change
    only the source_id to point to their own volume while keeping all other settings.
    
    volume_mounts: Dict[mount_path, partial_config] - merge partial_config into mount at mount_path
    secret_map: Dict[key, value] - replace secret_map[key] with value
    
    Example:
        template_overrides = {
            "volume_mounts": {
                "/data": {"source_id": "my-volume"}  # Only changes source_id, keeps type, config_content, etc.
            },
            "secret_map": {
                "DB_PASSWORD": "${secret:my-db-password}"  # Override placeholder with actual secret
            }
        }
    """
    volume_mounts: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Partial overrides for volume_mounts. Key is mount_path, value is dict of fields to override."
    )
    secret_map: Dict[str, str] = Field(
        default_factory=dict,
        description="Overrides for secret_map entries. Key is secret key, value is new secret reference or literal."
    )

    @field_validator('volume_mounts')
    @classmethod
    def check_volume_mounts(cls, v):
        """Validate volume_mounts override structure."""
        if not v:
            return v
        
        valid_fields = {'type', 'source_id', 'sub_path', 'read_only', 'config_content', 
                        'config_permissions', 'config_filename', 'config_update_mode'}
        valid_types = {'tapisvolume', 'tapissnapshot', 'ephemeral', 'pvc'}
        valid_update_modes = {'always', 'once'}
        
        for mount_path, override_config in v.items():
            # Validate mount_path format
            if not mount_path.startswith('/'):
                raise ValueError(f"template_overrides.volume_mounts key must be a mount_path starting with '/'. Got: {mount_path}")
            # Validate override config is a dict with valid fields
            if not isinstance(override_config, dict):
                raise ValueError(f"template_overrides.volume_mounts['{mount_path}'] must be a dict of fields to override")
            
            # Validate each override field
            for field, value in override_config.items():
                if field not in valid_fields:
                    raise ValueError(f"template_overrides.volume_mounts['{mount_path}'].{field} is not a valid VolumeMount field. Valid: {valid_fields}")
                
                # Field-specific validation
                if field == 'source_id' and value:
                    if not re.fullmatch(r'[a-z][a-z0-9\-]+', value):
                        raise ValueError(f"template_overrides.volume_mounts['{mount_path}'].source_id must be lowercase alphanumeric (hyphens allowed). Got: {value}")
                elif field == 'type' and value:
                    if value.lower() not in valid_types:
                        raise ValueError(f"template_overrides.volume_mounts['{mount_path}'].type must be one of {valid_types}. Got: {value}")
                elif field == 'read_only' and value is not None:
                    if not isinstance(value, bool):
                        raise ValueError(f"template_overrides.volume_mounts['{mount_path}'].read_only must be boolean. Got: {type(value).__name__}")
                elif field == 'config_permissions' and value:
                    if not re.fullmatch(r'[0-7]{3,4}', value):
                        raise ValueError(f"template_overrides.volume_mounts['{mount_path}'].config_permissions must be valid octal (e.g., '0644'). Got: {value}")
                elif field == 'config_update_mode' and value:
                    if value.lower() not in valid_update_modes:
                        raise ValueError(f"template_overrides.volume_mounts['{mount_path}'].config_update_mode must be one of {valid_update_modes}. Got: {value}")
                elif field == 'config_filename' and value:
                    if '/' in value or '\\' in value or '..' in value:
                        raise ValueError(f"template_overrides.volume_mounts['{mount_path}'].config_filename cannot contain path separators or '..'. Got: {value}")
        return v

    @field_validator('secret_map')
    @classmethod
    def check_secret_map(cls, v):
        """Validate secret_map override structure."""
        if not v:
            return v
        for key, value in v.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"template_overrides.secret_map keys must be non-empty strings")
            if not isinstance(value, str):
                raise ValueError(f"template_overrides.secret_map['{key}'] must be a string value")
        return v
