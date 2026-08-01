"""
Volume mount validation utilities for object-based volume_mounts structure.

The volume_mounts field is a Dict[str, VolumeMount | None] where:
- Keys are mount_path strings
- Values are VolumeMount objects or None (to remove inherited mounts)

Volume Mount Placeholder System:
    Templates MUST use placeholders for source_id in tapisvolume/tapissnapshot mounts:
    - "${:?description}" - Required placeholder that pod creator must override
    
    Pods override placeholders with actual volume IDs:
    - "my-volume-id" - Literal volume ID (user must have READ permission)
    
    This ensures templates don't assume access to specific volumes, and the pod
    creator (who provides the volume ID) must have permission to use it.

Permission Model:
    - On pod create/update: The user making the change must have READ on any
      volumes they're adding to volume_mounts. This user is recorded as "mounted_by".
    - On pod start/restart: At least one pod ADMIN must have READ on each volume.
      This allows team workflows where volume-admin adds mount, then others start pod.
    - Snapshots follow same rules but are always read-only.
"""
from typing import Dict, Optional, Any, List, Tuple, Literal
from dataclasses import dataclass
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

# Type alias for volume mount type field (for OpenAPI schema generation)
VolumeMountType = Literal["tapisvolume", "tapissnapshot", "ephemeral", "pvc"]

# =============================================================================
# Volume Placeholder Patterns and Parsing
# =============================================================================

# Pattern for required placeholder: ${:?description}
# Used in templates to indicate pod creator must provide a volume ID
VOLUME_PLACEHOLDER_PATTERN = re.compile(r'^\$\{:\?([^}]+)\}$')

# Pattern to detect any unresolved placeholder in source_id
VOLUME_UNRESOLVED_PATTERN = re.compile(r'\$\{[^}]+\}')


@dataclass
class VolumeSourceReference:
    """Parsed source_id reference from volume mount."""
    raw_value: str
    is_placeholder: bool
    description: Optional[str]  # Description from placeholder (for user guidance)
    volume_id: Optional[str]  # The actual volume ID if literal value


def _parse_volume_source_id(value: str) -> Tuple[VolumeSourceReference, Optional[str]]:
    """
    Parse a volume_mounts source_id value. Internal use only.
    
    Templates should use placeholders:
        - "${:?description}" - Required placeholder (pod must override)
    
    Pods should use literal volume IDs:
        - "my-volume-id" - Actual volume ID
    
    Args:
        value: The source_id value to parse
        
    Returns:
        Tuple of (VolumeSourceReference, error_message)
        - On success: (VolumeSourceReference, None)
        - On failure: (None, error_message)
    """
    if not value:
        return (None, "source_id cannot be empty")
    
    # Check for required placeholder: ${:?description}
    placeholder_match = VOLUME_PLACEHOLDER_PATTERN.match(value)
    if placeholder_match:
        description = placeholder_match.group(1)
        return (VolumeSourceReference(
            raw_value=value,
            is_placeholder=True,
            description=description,
            volume_id=None
        ), None)
    
    # Check for any unresolved placeholder pattern (error case)
    if VOLUME_UNRESOLVED_PATTERN.search(value):
        return (None, f"Invalid source_id format: '{value}'. Use '${{:?description}}' for placeholders or a literal volume ID.")
    
    # It's a literal volume ID - validate format
    if not re.fullmatch(r'[a-z][a-z0-9\-]+', value):
        return (None, f"source_id must be lowercase alphanumeric (hyphens allowed). First character must be alpha. Got: {value}")
    
    return (VolumeSourceReference(
        raw_value=value,
        is_placeholder=False,
        description=None,
        volume_id=value
    ), None)


def is_volume_placeholder(source_id: str) -> bool:
    """Check if a source_id is a placeholder pattern."""
    if not source_id:
        return False
    return bool(VOLUME_PLACEHOLDER_PATTERN.match(source_id))


def get_volume_placeholder_warnings(
    volume_mounts: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Check volume_mounts for unresolved placeholders and return warnings.
    
    Args:
        volume_mounts: Dict mapping mount paths to VolumeMount configs
        
    Returns:
        Tuple of (warnings list, parsing errors list)
        - warnings: List of dicts with mount_path, placeholder, description, type
        - errors: List of parsing error messages for invalid formats
    """
    warnings = []
    errors = []
    
    if not volume_mounts:
        return (warnings, errors)
    
    for mount_path, mount_config in volume_mounts.items():
        if mount_config is None:
            continue
        
        # Get source_id from config
        if isinstance(mount_config, dict):
            source_id = mount_config.get("source_id")
            mount_type = mount_config.get("type")
        else:
            source_id = getattr(mount_config, "source_id", None)
            mount_type = getattr(mount_config, "type", None)
        
        if not source_id:
            continue
        
        # Check if it's a placeholder
        ref, error = _parse_volume_source_id(source_id)
        if error:
            errors.append(f"Mount '{mount_path}': {error}")
            continue
        
        if ref.is_placeholder:
            warnings.append({
                "mount_path": mount_path,
                "mount_type": mount_type,
                "placeholder": ref.raw_value,
                "description": ref.description or "No description provided"
            })
    
    return (warnings, errors)


def validate_template_volume_mounts_placeholders(
    volume_mounts: Dict[str, Any]
) -> Tuple[bool, List[Dict[str, str]]]:
    """
    Validate that a template's volume_mounts uses placeholders for source_id,
    not literal volume IDs.
    
    Templates should define placeholders that users override with actual volumes.
    Literal volume IDs are not allowed because:
    1. Templates are shared - they shouldn't assume access to specific volumes
    2. Templates define structure - pods provide the actual volume bindings
    3. Permission model requires the pod creator to have access to volumes
    
    Valid template source_id values:
        - "${:?description}" - Required placeholder
    
    Invalid for templates (tapisvolume/tapissnapshot only):
        - "my-volume-id" - Literal volume ID
    
    Note: ephemeral and pvc types don't use this validation:
        - ephemeral has no source_id (uses config_content)
        - pvc is admin-only and may use literal PVC names
    
    Args:
        volume_mounts: Dict mapping mount paths to VolumeMount configs
        
    Returns:
        Tuple of (is_valid, list of error dicts)
        Each error dict contains:
        - mount_path: The mount path with the invalid source_id
        - source_id: The invalid value
        - mount_type: The type of mount
        - description: User-friendly description of the issue
    """
    errors = []
    
    if not volume_mounts:
        return (True, [])
    
    for mount_path, mount_config in volume_mounts.items():
        if mount_config is None:
            continue
        
        # Get fields from config
        if isinstance(mount_config, dict):
            source_id = mount_config.get("source_id")
            mount_type = mount_config.get("type")
        else:
            source_id = getattr(mount_config, "source_id", None)
            mount_type = getattr(mount_config, "type", None)
        
        # Only validate tapisvolume and tapissnapshot - these reference user resources
        if mount_type not in ("tapisvolume", "tapissnapshot"):
            continue
        
        if not source_id:
            # source_id is required for these types, but that's validated elsewhere
            continue
        
        # Parse the source_id
        ref, parse_error = _parse_volume_source_id(source_id)
        
        if parse_error:
            errors.append({
                "mount_path": mount_path,
                "source_id": source_id,
                "mount_type": mount_type,
                "description": parse_error
            })
            continue
        
        # Check for literal volume IDs - NOT allowed in templates
        if not ref.is_placeholder:
            errors.append({
                "mount_path": mount_path,
                "source_id": source_id,
                "mount_type": mount_type,
                "description": (
                    f"Templates cannot contain literal volume IDs for {mount_type} mounts. "
                    f"Templates define placeholders that pod creators override with their volumes. "
                    f"Use '${{:?Describe the volume needed}}' as a required placeholder. "
                    f"Example: '${{:?User data volume for persistent storage}}'"
                )
            })
    
    return (len(errors) == 0, errors)


def resolve_volume_placeholders(
    pod_mounts: Optional[Dict[str, Any]],
    template_mounts: Optional[Dict[str, Any]],
    template_overrides_mounts: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    """
    Resolve volume mount placeholders by applying pod values over template placeholders.
    
    Resolution order:
    1. Start with template_mounts as base
    2. Apply template_overrides_mounts (partial field overrides)
    3. Apply pod_mounts (full mount replacement)
    
    Args:
        pod_mounts: Pod's volume_mounts (full mount replacements)
        template_mounts: Template's volume_mounts (may contain placeholders)
        template_overrides_mounts: Partial field overrides from template_overrides
        
    Returns:
        Tuple of (resolved_mounts, errors, metadata)
        - resolved_mounts: Final merged volume_mounts dict
        - errors: List of error messages for unresolved required placeholders
        - metadata: Dict with placeholder resolution info
    """
    errors = []
    metadata = {"volume_placeholders": {"resolved": [], "unresolved": []}}
    
    # Start with template
    merged = dict(template_mounts) if template_mounts else {}
    
    # Apply template_overrides (partial merges)
    if template_overrides_mounts:
        for mount_path, override_config in template_overrides_mounts.items():
            if mount_path in merged and merged[mount_path] is not None:
                # Merge override fields into existing mount
                if isinstance(merged[mount_path], dict):
                    merged[mount_path] = {**merged[mount_path], **override_config}
                else:
                    # Convert to dict if needed
                    base = merged[mount_path].model_dump() if hasattr(merged[mount_path], 'model_dump') else dict(merged[mount_path])
                    merged[mount_path] = {**base, **override_config}
    
    # Apply pod_mounts (full replacements)
    if pod_mounts:
        for mount_path, mount_config in pod_mounts.items():
            if mount_config is None:
                # None removes inherited mount
                merged.pop(mount_path, None)
            else:
                # Full replacement
                merged[mount_path] = mount_config
    
    # Check for unresolved placeholders
    for mount_path, mount_config in merged.items():
        if mount_config is None:
            continue
        
        if isinstance(mount_config, dict):
            source_id = mount_config.get("source_id")
            mount_type = mount_config.get("type")
        else:
            source_id = getattr(mount_config, "source_id", None)
            mount_type = getattr(mount_config, "type", None)
        
        if not source_id:
            continue
        
        ref, _ = _parse_volume_source_id(source_id)
        if ref and ref.is_placeholder:
            # Still a placeholder - not resolved
            metadata["volume_placeholders"]["unresolved"].append({
                "mount_path": mount_path,
                "mount_type": mount_type,
                "placeholder": source_id,
                "description": ref.description
            })
            errors.append(
                f"Required volume placeholder at '{mount_path}' not overridden. "
                f"Description: {ref.description}. "
                f"Provide a volume ID in volume_mounts or template_overrides.volume_mounts."
            )
        elif ref and ref.volume_id:
            # Track resolved placeholders (for audit/logging)
            # Check if this was originally a placeholder in template
            template_source_id = None
            if template_mounts and mount_path in template_mounts:
                t_mount = template_mounts[mount_path]
                if isinstance(t_mount, dict):
                    template_source_id = t_mount.get("source_id")
                else:
                    template_source_id = getattr(t_mount, "source_id", None)
            
            if template_source_id and is_volume_placeholder(template_source_id):
                metadata["volume_placeholders"]["resolved"].append({
                    "mount_path": mount_path,
                    "mount_type": mount_type,
                    "original_placeholder": template_source_id,
                    "resolved_to": ref.volume_id
                })
    
    return (merged, errors, metadata)

# Maximum config content size (1MB)
MAX_CONFIG_CONTENT_SIZE = 1024 * 1024  # 1MB


def interpolate_config_content(content: str, secret_map: Dict[str, str], fail_on_missing: bool = True) -> str:
    """
    Interpolate ${pods:secrets:KEY} or ${pods:secrets:KEY:?description} placeholders in config content.
    
    The :?description suffix is optional and purely informational - it allows config files
    to be self-documenting. Descriptions are stripped during interpolation.
    
    Args:
        content: Config content with placeholders
        secret_map: Dict mapping keys to their resolved secret values
        fail_on_missing: If True, raise ValueError for missing keys. If False, leave placeholder.
        
    Returns:
        Interpolated config content string
        
    Examples:
        # Basic usage
        interpolate_config_content("pass=${pods:secrets:DB_PASS}", {"DB_PASS": "secret"})
        # Returns: "pass=secret"
        
        # With description (description is stripped)
        interpolate_config_content("pass=${pods:secrets:DB_PASS:?Database password}", {"DB_PASS": "secret"})
        # Returns: "pass=secret"
    """
    if content is None:
        return ""
    
    result = content
    # Pattern captures KEY and optional :?description
    # Group 1: key (required), Group 2: description (optional, after :?)
    pattern = r'\$\{pods:secrets:([a-zA-Z0-9_-]+)(?::\?([^}]+))?\}'
    
    def replace_match(match):
        key = match.group(1)
        # description = match.group(2)  # Available if needed for logging/debugging
        if key in secret_map:
            return str(secret_map[key])
        elif fail_on_missing:
            raise ValueError(f"Config interpolation failed: secret_map key '{key}' not found.")
        else:
            return match.group(0)  # Leave placeholder unchanged
    
    result = re.sub(pattern, replace_match, result)
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
                # Extract clean error message without Pydantic URLs and type info
                error_msg = str(e)
                # Remove Pydantic error URL references
                if 'For further information visit' in error_msg:
                    error_msg = error_msg.split('For further information visit')[0].strip()
                # Remove [type=..., input_value=..., input_type=...] suffix
                if '[type=' in error_msg:
                    error_msg = error_msg.split('[type=')[0].strip()
                # Extract the actual validation error message
                if 'Value error,' in error_msg:
                    # Get just the error description after 'Value error,'
                    parts = error_msg.split('Value error,')
                    if len(parts) > 1:
                        error_msg = parts[-1].strip().rstrip(']').strip()
                raise ValueError(f"Invalid volume mount at '{mount_path}': {error_msg}")
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
    - mounted_by: Service-managed field tracking which user mounted this volume (set by API, not user)
    """
    
    # Required field - using Literal for proper OpenAPI enum generation
    type: VolumeMountType = Field(..., description="Type of mount: 'tapisvolume', 'tapissnapshot', 'ephemeral', or 'pvc'.")
    
    # Required for storage types, not used for ephemeral
    source_id: Optional[str] = Field(None, description="ID of the volume, snapshot, or PVC to mount. Required for tapisvolume/tapissnapshot/pvc.")
    
    # Service-managed field (not user-settable)
    mounted_by: Optional[str] = Field(None, description="Service-managed: Username who mounted this volume. Set automatically when volume_mounts are created/updated.")
    
    # Optional fields for storage types
    sub_path: str = Field("", description="Sub-path within the source volume/snapshot to mount. Not used for ephemeral.")
    read_only: Optional[bool] = Field(None, description="If true, mount will be read-only. Default: False for volumes/pvc, True for snapshots/ephemeral.")
    
    # Config fields (used for ephemeral and tapisvolume with inline config)
    config_content: Optional[str] = Field(None, description="Config file content. For ephemeral: mounted as ConfigMap. For tapisvolume: written to NFS. Supports ${pods:secrets:KEY} interpolation. Max 1MB.")
    config_permissions: str = Field("0644", description="Unix file permissions for config file (e.g., '0644', '0600').")
    config_filename: Optional[str] = Field(None, description="Filename for config file when using tapisvolume with config_content. Defaults to basename of mount_path.")
    config_update_mode: str = Field("always", description="Config update behavior: 'always' recreates config on each pod start, 'once' only creates if file/ConfigMap doesn't exist.")

    @field_validator('type', mode='before')
    @classmethod
    def check_type(cls, v):
        # Normalize to lowercase before Literal validation
        if isinstance(v, str):
            v = v.lower()
        return v

    @field_validator('source_id')
    @classmethod
    def check_source_id(cls, v):
        if v is not None and v != "":
            # Allow placeholder pattern ${:?description} for templates
            if VOLUME_PLACEHOLDER_PATTERN.match(v):
                return v
            # Otherwise validate as literal volume ID (a-z0-9 with hyphens)
            res = re.fullmatch(r'[a-z][a-z0-9\-]+', v)
            if not res:
                raise ValueError(f"volume_mounts source_id must be lowercase alphanumeric (hyphens allowed), first character must be alpha, or a placeholder '${{:?description}}'. Got: {v}")
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

    @field_validator('sub_path')
    @classmethod
    def check_sub_path(cls, v):
        # sub_path is joined into the on-disk config write path at spawn time; a
        # '..' would let config_content escape the volume/tenant base (arbitrary
        # file write as the service user). Same guard mount_path/config_filename
        # already carry — sub_path was the gap.
        if v:
            if '..' in v or v.startswith('/') or '\\' in v:
                raise ValueError(f"sub_path cannot contain '..', backslashes, or be absolute. Got: {v}")
            if len(v) > 255:
                raise ValueError(f"sub_path must be less than 255 characters. Got: {len(v)}")
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
            # When config_content is provided, config_filename is REQUIRED to avoid ambiguity
            # (mount_path basename could be a directory name like 'headscale' vs a file 'config.yaml')
            config_filename = getattr(values, 'config_filename', None)
            if config_content and not config_filename:
                raise ValueError(
                    f"volume_mounts type 'tapisvolume' with config_content requires config_filename to be specified. "
                    f"This ensures the config file is written with an explicit filename rather than deriving from mount_path."
                )
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


def validate_volume_mounts_on_start(
    volume_mounts: Dict[str, Any],
    pod_permissions: Dict[str, str],
    tenant: str,
    site: str
) -> List[str]:
    """
    Validate volume mounts before starting/restarting a pod.
    
    For each tapisvolume/tapissnapshot mount WITH a `mounted_by` user set:
    1. Check that the mounted_by user still has ADMIN or USER permission on the pod
    2. Check that the mounted_by user still has READ permission on the volume/snapshot
    
    Note: Mounts without `mounted_by` are allowed (backward compatibility for older pods).
    
    Args:
        volume_mounts: Dict of mount_path -> VolumeMount config (can be dict or VolumeMount object)
        pod_permissions: Dict from pod.get_permissions() - {username: level}
        tenant: Tenant ID
        site: Site ID
        
    Returns:
        List of error messages (empty if all valid)
    """
    if not volume_mounts:
        return []
    
    # Import here to avoid circular imports
    from models_volumes import Volume
    from models_snapshots import Snapshot
    from utils import check_permissions
    import codes
    
    errors = []
    
    for mount_path, mount_config in volume_mounts.items():
        if mount_config is None:
            continue
        
        # Handle both dict and VolumeMount object
        def get_field(field):
            return mount_config.get(field) if isinstance(mount_config, dict) else getattr(mount_config, field, None)
        
        mount_type = get_field("type")
        source_id = get_field("source_id")
        mounted_by = get_field("mounted_by")
        
        # Only check tapisvolume/tapissnapshot with mounted_by set
        if mount_type not in ("tapisvolume", "tapissnapshot") or not mounted_by or not source_id:
            continue
        
        # Skip placeholders (shouldn't happen at start time)
        if is_volume_placeholder(source_id):
            continue
        
        # Check 1: mounted_by user must exist in pod_permissions
        current_permission = pod_permissions.get(mounted_by)
        if current_permission is None:
            errors.append(
                f"Volume at '{mount_path}' was mounted by '{mounted_by}' who is no longer in this pod's "
                f"permissions list. Remove the mount or re-add with a permitted user."
            )
            continue
        
        # Check 2: mounted_by user must have ADMIN or USER permission on the pod
        # Note: pod_permissions values are strings, so compare against string literals
        if current_permission not in ("ADMIN", "USER"):
            errors.append(
                f"Volume at '{mount_path}' was mounted by '{mounted_by}' who has '{current_permission}' permission "
                f"but requires ADMIN or USER. Remove the mount or re-add with a permitted user."
            )
            continue
        
        # Check 3: mounted_by user must still have READ permission on the volume/snapshot
        try:
            obj_type = "volume" if mount_type == "tapisvolume" else "snapshot"
            Model = Volume if mount_type == "tapisvolume" else Snapshot
            
            resource = Model.db_get_with_pk(source_id, tenant=tenant, site=site)
            if not resource:
                errors.append(f"{obj_type.capitalize()} '{source_id}' at '{mount_path}' not found.")
                continue
            
            if not check_permissions(user=mounted_by, level=codes.READ, object=resource, 
                                     object_type=obj_type, roles=None, tenant=tenant):
                errors.append(
                    f"User '{mounted_by}' (who has '{current_permission}' on pod) no longer has READ permission on "
                    f"{obj_type} '{source_id}' at '{mount_path}'. Remove the mount or have a user with both "
                    f"pod ADMIN/USER permission AND {obj_type} READ permission re-add it."
                )
        except Exception as e:
            errors.append(f"Error checking {mount_type} '{source_id}' at '{mount_path}': {e}")
    
    return errors


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
    is_blocking: bool = True,
    pod_admins: List[str] = None,
    check_any_admin: bool = False
) -> VolumeMountValidationResult:
    """
    Validate that user has permission to use the volumes/snapshots in volume_mounts.
    
    Permission Model:
    - If check_any_admin=False (default, for create/update): 
      The requesting user must have READ permission on volumes they're adding.
    - If check_any_admin=True (for start/restart):
      At least one pod admin must have READ permission on each volume.
    
    Args:
        volume_mounts: Dict of mount_path -> VolumeMount config
        user: Username making the request (required for permission checks)
        tenant: The tenant ID
        site: The site ID
        roles: User's roles (for admin bypass)
        template_source: Source template for error messages
        is_blocking: If True, permission failures are errors. If False, they're warnings.
        pod_admins: List of usernames with ADMIN permission on the pod (for check_any_admin)
        check_any_admin: If True, check if ANY pod admin has permission (for start/restart)
        
    Returns:
        VolumeMountValidationResult with is_valid, errors, warnings, and metadata
        metadata includes:
        - mounted_by: Dict of mount_path -> username who has permission (for audit)
        - volume_placeholders: Any placeholder info
    """
    result = VolumeMountValidationResult()
    result.metadata["mounted_by"] = {}
    
    if not volume_mounts:
        return result
    
    # Import here to avoid circular imports
    from models_volumes import Volume
    from models_snapshots import Snapshot
    from utils import check_permissions
    import codes
    
    for mount_path, mount_config in volume_mounts.items():
        if mount_config is None:
            continue
        
        mount_type = mount_config.get("type") if isinstance(mount_config, dict) else getattr(mount_config, "type", None)
        source_id = mount_config.get("source_id") if isinstance(mount_config, dict) else getattr(mount_config, "source_id", None)
        
        # Skip if source_id is a placeholder (templates only)
        if source_id and is_volume_placeholder(source_id):
            continue
        
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
                    continue
                
                # Check permissions
                if check_any_admin and pod_admins:
                    # For start/restart: check if ANY pod admin has permission
                    found_admin_with_access = None
                    for admin_user in pod_admins:
                        has_perm = check_permissions(
                            user=admin_user,
                            level=codes.READ,
                            object=volume,
                            object_type="volume",
                            roles=None,  # Don't pass roles for other users
                            tenant=tenant
                        )
                        if has_perm:
                            found_admin_with_access = admin_user
                            break
                    
                    if found_admin_with_access:
                        result.metadata["mounted_by"][mount_path] = found_admin_with_access
                    else:
                        msg = f"No pod admin has READ permission on volume '{source_id}' at mount path '{mount_path}'"
                        if is_blocking:
                            result.errors.append(msg)
                            result.is_valid = False
                        else:
                            result.warnings.append(msg)
                elif user:
                    # For create/update: requesting user must have permission
                    has_perm = check_permissions(
                        user=user,
                        level=codes.READ,
                        object=volume,
                        object_type="volume",
                        roles=roles,
                        tenant=tenant
                    )
                    if has_perm:
                        result.metadata["mounted_by"][mount_path] = user
                    else:
                        msg = f"User '{user}' does not have READ permission on volume '{source_id}' at mount path '{mount_path}'"
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
                    continue
                
                # Check permissions (same logic as volumes)
                if check_any_admin and pod_admins:
                    found_admin_with_access = None
                    for admin_user in pod_admins:
                        has_perm = check_permissions(
                            user=admin_user,
                            level=codes.READ,
                            object=snapshot,
                            object_type="snapshot",
                            roles=None,
                            tenant=tenant
                        )
                        if has_perm:
                            found_admin_with_access = admin_user
                            break
                    
                    if found_admin_with_access:
                        result.metadata["mounted_by"][mount_path] = found_admin_with_access
                    else:
                        msg = f"No pod admin has READ permission on snapshot '{source_id}' at mount path '{mount_path}'"
                        if is_blocking:
                            result.errors.append(msg)
                            result.is_valid = False
                        else:
                            result.warnings.append(msg)
                elif user:
                    has_perm = check_permissions(
                        user=user,
                        level=codes.READ,
                        object=snapshot,
                        object_type="snapshot",
                        roles=roles,
                        tenant=tenant
                    )
                    if has_perm:
                        result.metadata["mounted_by"][mount_path] = user
                    else:
                        msg = f"User '{user}' does not have READ permission on snapshot '{source_id}' at mount path '{mount_path}'"
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
