from codes import ERROR, SPAWNER_SETUP, CREATING, \
    REQUESTED, DELETING
from models_templates_tags import TemplateTag, TemplateTagPodDefinition, derive_template_info
from kubernetes_utils import create_pod, create_service, create_pvc, KubernetesError
from kubernetes import client, config

import re
from typing import Dict, List, Tuple, Any
from dataclasses import dataclass

from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapisservice.errors import BaseTapisError
from __init__ import t
logger = get_logger(__name__)


@dataclass
class SecretMapValidationResult:
    """Result object for secret_map validation operations."""
    is_valid: bool
    errors: List[Dict[str, str]]  # List of {key, value, description} error dicts
    metadata: Dict[str, Any]  # Metadata to return to user (placeholder info, etc.)
    error_message: str = ""  # Pre-formatted error message for API response


def validate_template_tag_secret_map(secret_map: Dict[str, str], actor: str = None) -> SecretMapValidationResult:
    """
    Validate that a template tag's secret_map contains only placeholders, not direct secret references.
    
    Template tags should define placeholders that users override with actual secrets when creating pods.
    Direct secret references like ${secret:name} are not allowed in template tags because:
    1. Template tags are shared - they shouldn't embed user-specific secrets
    2. Template tags define structure - pods provide the actual secret bindings
    
    Valid template secret_map values:
        - ${pods:default:value:?description} - Placeholder with default value
        - ${:?description} - Required placeholder (no default)
        - Literal strings (no ${} at root)
    
    Invalid for templates:
        - ${secret:name} - Direct secret reference
        - ${secret:user:name} - Explicit user secret reference
    
    Args:
        secret_map: Dict mapping keys to values (placeholders or literals)
        actor: Current user for parsing context
        
    Returns:
        SecretMapValidationResult with is_valid, errors, metadata, and error_message
    """
    from secret_utils import validate_template_secret_map, get_placeholder_warnings
    
    if not secret_map:
        return SecretMapValidationResult(is_valid=True, errors=[], metadata={})
    
    # First validate that no direct secret references exist
    is_valid, errors = validate_template_secret_map(secret_map, actor=actor)
    
    if not is_valid:
        error_messages = [f"Key '{err['key']}': {err['description']}" for err in errors]
        return SecretMapValidationResult(
            is_valid=False,
            errors=errors,
            metadata={},
            error_message=f"Template secret_map validation failed. Templates cannot contain direct secret references "
                          f"(${{secret:name}}) - they must use placeholders that pod creators override. "
                          f"Details: {'; '.join(error_messages)}"
        )
    
    # Get placeholder information for metadata
    placeholder_warnings, parse_errors = get_placeholder_warnings(secret_map, actor=actor)
    
    if parse_errors:
        error_dicts = [{"key": "secret_map", "value": "", "description": err} for err in parse_errors]
        return SecretMapValidationResult(
            is_valid=False,
            errors=error_dicts,
            metadata={},
            error_message=f"Invalid secret_map format in pod_definition: {'; '.join(parse_errors)}"
        )
    
    # Build metadata with placeholder info
    metadata = {}
    if placeholder_warnings:
        metadata["secret_placeholders"] = placeholder_warnings
    
    return SecretMapValidationResult(is_valid=True, errors=[], metadata=metadata)


def validate_template_tag_env_vars(environment_variables: Dict[str, Any], 
                                    secret_map: Dict[str, str]) -> SecretMapValidationResult:
    """
    Validate that environment_variables only reference keys that exist in secret_map.
    
    Args:
        environment_variables: Dict of env vars (may contain ${pods:secrets:KEY} refs)
        secret_map: Dict of secret_map entries
        
    Returns:
        SecretMapValidationResult with validation status
    """
    from secret_utils import validate_environment_placeholders
    
    if not environment_variables:
        return SecretMapValidationResult(is_valid=True, errors=[], metadata={})
    
    is_valid, env_errors = validate_environment_placeholders(
        environment_variables,
        secret_map or {}
    )
    
    if not is_valid:
        error_dicts = [{"key": "environment_variables", "value": "", "description": err} for err in env_errors]
        return SecretMapValidationResult(
            is_valid=False,
            errors=error_dicts,
            metadata={},
            error_message=f"Environment variable validation failed: {'; '.join(env_errors)}"
        )
    
    return SecretMapValidationResult(is_valid=True, errors=[], metadata={})


def validate_pod_secret_map_against_template(pod_secret_map: Dict[str, str], 
                                              template_secret_map: Dict[str, str],
                                              actor: str = None) -> SecretMapValidationResult:
    """
    Validate that a pod's secret_map properly overrides all required placeholders from template.
    
    When a pod uses a template with placeholders:
    - Required placeholders (${:?description}) MUST be overridden by pod
    - Optional placeholders (${pods:default:value:?description}) can use default or be overridden
    - Pod can provide actual secret references (${secret:name})
    
    Args:
        pod_secret_map: The pod's secret_map (may be empty)
        template_secret_map: The merged template secret_map with all placeholders
        actor: Current user for parsing context
        
    Returns:
        SecretMapValidationResult with is_valid, errors, metadata, and error_message
    """
    from secret_utils import parse_secret_reference
    
    errors = []
    required_placeholders = []
    optional_placeholders = []
    
    if not template_secret_map:
        return SecretMapValidationResult(is_valid=True, errors=[], metadata={})
    
    # Merge pod and template secret maps (pod overrides template)
    merged_map = dict(template_secret_map)
    merged_map.update(pod_secret_map or {})
    
    # Check each entry in the merged map
    for key, value in merged_map.items():
        ref, parse_error = parse_secret_reference(value, actor=actor)
        
        if parse_error:
            errors.append({
                "key": key,
                "value": value,
                "description": f"Invalid secret_map value: {parse_error}"
            })
            continue
        
        # Check if this is still an unresolved required placeholder
        if ref.is_placeholder and ref.is_required:
            # Required placeholder was not overridden by pod
            errors.append({
                "key": key,
                "value": value,
                "description": f"REQUIRED: '{key}' - {ref.description or 'No description'}. Add to pod's secret_map."
            })
            required_placeholders.append(
                f"REQUIRED: '{key}' - {ref.description or 'No description'}. Override in secret_map."
            )
        elif ref.is_placeholder and not ref.is_required:
            # Optional placeholder with default - add to metadata
            optional_placeholders.append(
                f"OPTIONAL: '{key}' - {ref.description or 'No description'}. Default: '{ref.default_value}'."
            )
    
    # Build metadata as simple string lists
    metadata = {}
    if required_placeholders or optional_placeholders:
        metadata["template_placeholders"] = {
            "required": required_placeholders,
            "optional": optional_placeholders
        }
    
    if errors:
        # Build simple, human-readable error messages
        error_lines = [err['description'] for err in errors]
        return SecretMapValidationResult(
            is_valid=False,
            errors=errors,
            metadata=metadata,
            error_message=f"Missing required placeholders in secret_map: {'; '.join(error_lines)}"
        )
    
    return SecretMapValidationResult(is_valid=True, errors=[], metadata=metadata)


def get_template_merged_secret_map(template_name: str, tenant: str = None, site: str = None) -> Dict[str, str]:
    """
    Get the merged secret_map from a template and all its chained templates.
    
    Priority for chained templates: closer template > deeper template
    
    Args:
        template_name: Template reference string (e.g., "template_id:tag_name")
        tenant: Tenant ID for template lookup
        site: Site ID for template lookup
        
    Returns:
        Dict of merged secret_map entries from template chain
    """
    seen_templates = set()
    merged_secret_map = {}
    
    def process_template_chain(tpl_name: str):
        nonlocal merged_secret_map
        
        if not tpl_name:
            return
        
        if tpl_name in seen_templates:
            raise ValueError(f"Infinite loop detected: template {tpl_name} is referenced more than once in template waterfall.")
        seen_templates.add(tpl_name)
        
        _, _, template_tag = derive_template_info(tpl_name, tenant=tenant, site=site)
        pod_def = template_tag.pod_definition or {}
        
        # Convert Pydantic model to dict if necessary
        if hasattr(pod_def, 'model_dump'):
            pod_def = pod_def.model_dump()
        elif hasattr(pod_def, 'dict'):
            pod_def = pod_def.dict()
        
        # First process deeper template (if this template references another)
        modified_fields = get_modified_template_fields(TemplateTagPodDefinition().dict(), pod_def)
        if modified_fields.get('template'):
            process_template_chain(modified_fields['template'])
        
        # Then apply this template's secret_map (closer template wins over deeper)
        template_secret_map = pod_def.get('secret_map', {}) or {}
        if hasattr(template_secret_map, 'dict'):
            template_secret_map = template_secret_map.dict()
        
        merged_secret_map.update(template_secret_map)
    
    process_template_chain(template_name)
    return merged_secret_map


def get_modified_template_fields(original_template, modified_template_def):
    """
    Returns a dictionary of fields that have been modified from a base template
    Meaning, returns fields that user defined in template.
    """
    # Convert Pydantic model to dict if necessary
    if hasattr(modified_template_def, 'model_dump'):
        modified_template_def = modified_template_def.model_dump()
    elif hasattr(modified_template_def, 'dict'):
        modified_template_def = modified_template_def.dict()
    
    changed_fields = {}
    for key, value in original_template.items():
        if key not in modified_template_def or value != modified_template_def[key]:
            changed_fields[key] = modified_template_def.get(key)
    if changed_fields.get('resources'):
        ### resources.gpus, resources.mem_Limit, etc exists.
        # Only return resources in dict if subfield not null, so we delete null subfields
        for resource_key, resource_val in changed_fields['resources'].copy().items():
            if resource_val is None:
                del changed_fields['resources'][resource_key]
    return changed_fields


def apply_template_overrides(
    volume_mounts: Dict[str, Any],
    secret_map: Dict[str, str],
    template_overrides: Dict[str, Any] = None
) -> Tuple[Dict[str, Any], Dict[str, str], List[str]]:
    """
    Apply template_overrides to merged volume_mounts and secret_map.
    
    For volume_mounts: merges override fields into existing mount config at mount_path.
    For secret_map: replaces value at key.
    
    Args:
        volume_mounts: Merged volume_mounts dict (from template + pod)
        secret_map: Merged secret_map dict (from template + pod)
        template_overrides: TemplateOverrides dict or object with volume_mounts and secret_map
        
    Returns:
        Tuple of (updated_volume_mounts, updated_secret_map, warnings)
    """
    warnings = []
    
    if not template_overrides:
        return volume_mounts, secret_map, warnings
    
    # Convert to dict if needed
    if hasattr(template_overrides, 'model_dump'):
        template_overrides = template_overrides.model_dump()
    elif hasattr(template_overrides, 'dict'):
        template_overrides = template_overrides.dict()
    
    # Apply volume_mounts overrides
    vm_overrides = template_overrides.get('volume_mounts', {})
    if vm_overrides and volume_mounts:
        volume_mounts = dict(volume_mounts)  # Copy to avoid mutation
        for mount_path, override_fields in vm_overrides.items():
            if mount_path in volume_mounts and volume_mounts[mount_path] is not None:
                # Merge override fields into existing config
                existing = volume_mounts[mount_path]
                if isinstance(existing, dict):
                    merged = dict(existing)
                    merged.update(override_fields)
                    volume_mounts[mount_path] = merged
            else:
                # Mount path not found in merged mounts - warn but don't error
                warnings.append(f"template_overrides.volume_mounts['{mount_path}'] not found in merged volume_mounts")
    
    # Apply secret_map overrides
    sm_overrides = template_overrides.get('secret_map', {})
    if sm_overrides:
        secret_map = dict(secret_map) if secret_map else {}  # Copy to avoid mutation
        for key, value in sm_overrides.items():
            if key in secret_map:
                secret_map[key] = value
            else:
                # Key not found - warn but still apply (user may be adding new key)
                warnings.append(f"template_overrides.secret_map['{key}'] not found in merged secret_map, adding as new entry")
                secret_map[key] = value
    
    return volume_mounts, secret_map, warnings


def combine_pod_and_template_recursively(input_obj, template_name, seen_templates=None, tenant: str = None, site: str = None, original_pod_envs=None, original_pod_secret_map=None):
    """
    --- run with
    pod = Pod.db_get_with_pk(pk_id='testingfastapi', tenant='dev', site='tacc')
    d = combine_pod_and_template_recursively(pod, "template21:car@2024-06-11-18:09:39")
    d.description
    """
    logger.debug(f"Top of combine_pod_and_template_recursively for template: {template_name}, tenant: {tenant}, site: {site}")
    if seen_templates is None:
        seen_templates = set()
    
    # Store original pod env vars on first call (before any template processing)
    if original_pod_envs is None and 'environment_variables' in (input_obj.modified_fields or []):
        original_pod_envs = input_obj.environment_variables.copy() if input_obj.environment_variables else {}
    
    # Store original pod secret_map on first call (before any template processing)
    if original_pod_secret_map is None and 'secret_map' in (input_obj.modified_fields or []):
        pod_sm = getattr(input_obj, 'secret_map', {}) or {}
        if hasattr(pod_sm, 'dict'):
            pod_sm = pod_sm.dict()
        original_pod_secret_map = dict(pod_sm)

    if template_name:
        if template_name in seen_templates:
            raise ValueError(f"Infinite loop detected: template {template_name} is referenced more than once in template waterfall.")
        seen_templates.add(template_name)

        template_name_str, template, template_tag = derive_template_info(template_name, tenant=tenant, site=site)
        
        # Convert pod_definition to dict if it's a Pydantic model
        template_pod_def = template_tag.pod_definition
        if hasattr(template_pod_def, 'model_dump'):
            template_pod_def = template_pod_def.model_dump()
        elif hasattr(template_pod_def, 'dict'):
            template_pod_def = template_pod_def.dict()
        elif template_pod_def is None:
            template_pod_def = {}
        
        modified_fields = get_modified_template_fields(TemplateTagPodDefinition().dict(), template_pod_def)

        # First, recursively combine the input_obj with the next template in the chain
        input_obj = combine_pod_and_template_recursively(input_obj, modified_fields.get('template'), seen_templates, tenant, site, original_pod_envs, original_pod_secret_map)

        # Then, apply the current template to the input_obj
        try:
            logger.debug("Attempting to combine pod and template recursively22")
            for mod_key, mod_val in modified_fields.items():
                input_obj_modified_fields = input_obj.modified_fields or []
                logger.debug(f"mod_key: {mod_key}; mod_val: {mod_val}")
                if mod_key == "resources":
                    # Merge template resources with pod resources
                    # Priority: user-modified pod values > closer template > deeper template > pod defaults
                    pod_resources = getattr(input_obj, "resources", {})
                    if hasattr(pod_resources, 'dict'):
                        pod_resources = pod_resources.dict()
                    
                    # mod_val contains only the resources explicitly set in this template
                    template_resources = mod_val
                    
                    # Start with current pod_resources (which may have values from deeper templates)
                    merged_resources = pod_resources.copy() if pod_resources else {}
                    
                    # Apply this template's resources on top (closer template wins over deeper)
                    if template_resources:
                        for resource_key, resource_val in template_resources.items():
                            # Only apply if user didn't explicitly modify this field via modified_fields (resources.cpu_request)
                            resource_field = f"resources.{resource_key}"
                            if resource_field not in input_obj_modified_fields:
                                # Template didn't set this - use pod default
                                merged_resources[resource_key] = resource_val
                    
                    setattr(input_obj, mod_key, merged_resources)
                    logger.critical(f'DEBUG: end of resources merge {getattr(input_obj, mod_key, {})}')
                elif mod_key.startswith("resources."):
                    logger.critical('hey')
                    outer_arg, inner_arg = resources.split('.') # resources.gpus
                    outer_obj = getattr(input_obj, outer_arg) # resources
                    logger.critical('oh no!')
                    new_obj_value = template_pod_def[outer_arg][inner_arg]
                    setattr(outer_obj, inner_arg, new_obj_value)
                elif mod_key == "networking":
                    # must take template3, update with template2, template,1 and then pod, in that order
                    # Preserving order of objs, pod being the most important.
                    # Merge template networking with pod networking, pod values take precedence
                    final_network_obj = getattr(input_obj, mod_key)
                    template_networks = template_pod_def[mod_key]
                    for network_name, network_def in template_networks.items():
                        # Start with template's network definition
                        merged_network = network_def.copy()
                        # If pod modified this field, overwrite template
                        logger.critical(f"network_name: {network_name}, input_obj_modified_fields: {input_obj_modified_fields}")
                        if network_name in final_network_obj and "networking" in input_obj_modified_fields:
                            merged_network.update(final_network_obj[network_name])
                        # Get tenant_id from input_obj or use default from context
                        tenant_id = getattr(input_obj, 'tenant_id')
                        # Fetch base_url from tenant_cache
                        logger.debug(f"Fetching base_url for pod {input_obj.pod_id} network {network_name}")
                        base_url = t.tenant_cache.get_tenant_config(tenant_id=tenant_id).base_url
                        # Generate URL based on network name
                        if network_name == 'default':
                            url = base_url.replace("https://", f"{input_obj.pod_id}.pods.")
                        else:
                            url = base_url.replace("https://", f"{input_obj.pod_id}-{network_name}.pods.")
                        # Set the URL in network definition
                        merged_network['url'] = url
                        final_network_obj[network_name] = merged_network
                    setattr(input_obj, mod_key, final_network_obj)
                elif mod_key == "environment_variables":
                    logger.debug(f"environment_variables----")
                    # Self-documenting method of either overwriting alls envs or appending to them
                    use_template_envs_flag = input_obj.environment_variables.get("_TAPIS_INTERNAL_USE_TEMPLATE_ENVS", "True")
                    if use_template_envs_flag.lower() == "true":
                        # Priority order: pod modified > closer template > deeper template
                        # 1. Start with current input_obj env vars (has deeper template values)
                        # 2. Apply this template's env vars on top (closer template wins over deeper)
                        # 3. Re-apply pod's original env vars if user modified them (pod wins over all templates)
                        input_envs = input_obj.environment_variables.copy()
                        template_envs = template_pod_def[mod_key]
                        if template_envs:
                            # Closer template overrides deeper template values
                            final_envs = input_envs.copy()
                            final_envs.update(template_envs)
                            # But pod's original env vars should override all template values
                            if original_pod_envs:
                                final_envs.update(original_pod_envs)
                        else:
                            final_envs = input_envs
                        setattr(input_obj, mod_key, final_envs)
                        logger.debug(f"_TAPIS_INTERNAL_USE_TEMPLATE_ENVS is True - input_obj.environment_variables: {input_obj.environment_variables}")
                    else:
                        # We're not using templateobj envs, so we only use inputobj envs
                        logger.debug(f"_TAPIS_INTERNAL_USE_TEMPLATE_ENVS is False - input_obj.environment_variables: {input_obj.environment_variables}")
                elif mod_key == "secret_map":
                    # Merge secret_maps with proper priority: pod > closer template > deeper template
                    # Template defines placeholders (${pods:default:...} or ${:?...}) that users can override
                    # with actual secret references (${secret:...}) at pod creation
                    # 
                    # At this point:
                    # - input_obj.secret_map contains merged values from deeper templates + original pod values
                    # - template_secret_map is the current (closer) template's values
                    # 
                    # Order: Pod was already applied first. We recursively went deep, now coming back up.
                    # So we need: start with deeper values (input_obj), apply current template on top,
                    # but pod's original values must override all templates.
                    template_secret_map = template_pod_def.get(mod_key, {}) or {}
                    current_secret_map = getattr(input_obj, "secret_map", {}) or {}
                    
                    if hasattr(template_secret_map, 'dict'):
                        template_secret_map = template_secret_map.dict() if hasattr(template_secret_map, 'dict') else dict(template_secret_map)
                    if hasattr(current_secret_map, 'dict'):
                        current_secret_map = current_secret_map.dict() if hasattr(current_secret_map, 'dict') else dict(current_secret_map)
                    
                    # Priority: closer template > deeper template (current input_obj has deeper values)
                    # Then pod's original secret_map (from modified_fields) wins over all
                    final_secret_map = dict(current_secret_map)  # Start with deeper template values
                    final_secret_map.update(template_secret_map)  # Closer template wins over deeper
                    
                    # Re-apply pod's original secret_map if user modified it (pod wins over all templates)
                    if original_pod_secret_map:
                        final_secret_map.update(original_pod_secret_map)
                    
                    setattr(input_obj, mod_key, final_secret_map)
                    logger.debug(f"Merged secret_map: template had {len(template_secret_map)} entries, current has {len(current_secret_map)}, final has {len(final_secret_map)}")
                elif mod_key.startswith("volume_mount."):
                    pass  # Reserved for future per-mount overrides
                elif mod_key == "volume_mounts":
                    # Skip template merge if user explicitly modified volume_mounts at pod creation
                    # The merge was already done at creation time and stored in pod.volume_mounts
                    if "volume_mounts" in input_obj_modified_fields:
                        logger.debug(f"volume_mounts in modified_fields - skipping template merge, using pod's stored value")
                        continue
                    
                    # Use only user's volume_mounts by default, or merge if _TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES is True
                    # Dict-based structure: keys are mount_paths, values are VolumeMount configs or None
                    from models_volume_mounts_utils import merge_pod_volume_mounts_with_template
                    
                    pod_volume_mounts = getattr(input_obj, "volume_mounts", {})
                    if hasattr(pod_volume_mounts, 'dict'):
                        pod_volume_mounts = pod_volume_mounts.dict() if hasattr(pod_volume_mounts, 'dict') else dict(pod_volume_mounts)
                    elif not isinstance(pod_volume_mounts, dict):
                        # Fallback for any non-dict format
                        pod_volume_mounts = {}
                    
                    template_volume_mounts = template_pod_def.get(mod_key, {})
                    if hasattr(template_volume_mounts, 'dict'):
                        template_volume_mounts = template_volume_mounts.dict() if hasattr(template_volume_mounts, 'dict') else dict(template_volume_mounts)
                    elif not isinstance(template_volume_mounts, dict):
                        # Fallback for any non-dict format
                        template_volume_mounts = {}
                    
                    env_vars = getattr(input_obj, "environment_variables", {})
                    use_template_vols_flag = env_vars.get('_TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES', "True")
                    
                    if use_template_vols_flag.lower() == "true":
                        # Merge: template provides base mounts, pod can override or add
                        # None values in pod_volume_mounts remove inherited mounts
                        merged_volume_mounts = merge_pod_volume_mounts_with_template(
                            pod_mounts=pod_volume_mounts,
                            template_mounts=template_volume_mounts
                        )
                        
                        setattr(input_obj, mod_key, merged_volume_mounts)
                    else:
                        setattr(input_obj, mod_key, pod_volume_mounts or {})
                elif mod_key.startswith("template"):
                    pass ## Don't need this one
                elif mod_key in input_obj.modified_fields:
                    pass ## Don't modify user-modified fields, sans the above as they're dict updates and not overwrites
                else:
                    setattr(input_obj, mod_key, mod_val)

            logger.debug(f"End of combine_pod_and_template_recursively for template: {template_name}, tenant: {tenant}, site: {site}")
            try:
                if input_obj.resources and not type(input_obj.resources) == dict:
                    input_obj.resources = input_obj.resources.dict()
            except Exception as e:
                logger.debug(f'this resources part: Got exception when attempting to combine pod and templates: {e}')
                pass

            try:
                if input_obj.networking and not type(input_obj.networking) == dict:
                    input_obj.networking = input_obj.networking.dict()
            except Exception as e:
                logger.debug(f'this networking part: Got exception when attempting to combine pod and templates: {e}')
                pass

        except Exception as e:
            logger.debug(f'Got exception when attempting to combine pod and templates: {e}')

    # Apply template_overrides if present (partial overrides for volume_mounts and secret_map)
    if hasattr(input_obj, 'template_overrides') and input_obj.template_overrides:
        vol_mounts = getattr(input_obj, 'volume_mounts', {}) or {}
        sec_map = getattr(input_obj, 'secret_map', {}) or {}
        updated_vol, updated_sec, warnings = apply_template_overrides(vol_mounts, sec_map, input_obj.template_overrides)
        setattr(input_obj, 'volume_mounts', updated_vol)
        setattr(input_obj, 'secret_map', updated_sec)
        # Log warnings for missing paths/keys (non-blocking)
        for warn in warnings:
            logger.warning(f"template_overrides: {warn}")

    return input_obj