"""
Secret utilities for parsing, resolving, and injecting secrets into pods.

All secrets must be defined in the `secret_map` and referenced in `environment_variables`
using the `${pods:secrets:KEY}` notation where KEY is a secret_map key.

Notation Reference:
    secret_map values use ${} wrapper for ALL dynamic content:
        - KEY: "${secret:mysecret}"              - User secret (auto-adds your username)
        - KEY: "${secret:username:secretname}"   - Explicit user secret
        - KEY: "${pods:default:value}"           - Placeholder with default value (no description)
        - KEY: "${pods:default:value:?desc}"     - Placeholder with default value and optional description
        - KEY: "${:?description}"                - Required placeholder (no default)
        - KEY: "${pods:random:32}"               - Generate random 32-char password
        - KEY: "literal_string"                  - Plain literal (no $ processing)
    
    environment_variables:
        - VAR: "${pods:secrets:KEY}"            - Reference to secret_map KEY
        - VAR: "prefix_${pods:secrets:KEY}"     - Inline reference (e.g. URLs)
        - VAR: "literal_value"                  - Plain value (no secret)

Format Requirements:
    - Short secret: ${secret:secretname} - Auto-expands to ${secret:username:secretname} in backend
    - Explicit secret: ${secret:username:secretname} - Username must match actor
    - Username: alphanumeric, underscores, and @ [a-zA-Z0-9_@]+
    - Secret name: alphanumeric, underscores, hyphens [a-zA-Z0-9_-]+
    - Anything without ${} is a literal string (safe)

Examples:
    # Define secrets in secret_map (all secrets MUST be defined here first)
    secret_map = {
        "DB_PASSWORD": "${secret:mydbsecret}",            # Your secret (auto-adds username)
        "DB_PASSWORD2": "${secret:jsmith:mydbsecret}",    # Explicit form (same result for user jsmith)
        "API_KEY": "${:?API key for service}",            # Required, no default
        "CACHE_HOST": "${pods:default:redis:?Redis hostname}", # With default and description
        "CACHE_PORT": "${pods:default:6379}",             # With default, no description
        "RANDOM_PASS": "${pods:random:32}",               # Generate 32-char random password
        "NOTE": "my question is ${:?put your answer}",    # Literal with placeholder
    }
    
    # Reference secrets in environment_variables via ${pods:secrets:KEY}
    environment_variables = {
        "DATABASE_PASSWORD": "${pods:secrets:DB_PASSWORD}",
        "SERVICE_API_KEY": "${pods:secrets:API_KEY}",
        "REDIS_URL": "redis://${pods:secrets:CACHE_HOST}:6379",  # Inline
        "APP_NAME": "my_app",                    # Plain value, no secret
    }
"""
import re
from typing import Dict, Tuple, Optional, List, Any
from dataclasses import dataclass

from tapisservice.tapisfastapi.utils import g
from tapisservice.logs import get_logger
from models_secrets import Secret, PODS_SERVICE_ACCOUNT, PODS_SERVICE_TENANT
from models_secret_logs import log_secret_event
from __init__ import t

logger = get_logger(__name__)


# Pattern for short secret reference: ${secret:secretname}
# Auto-expands to include current user at resolution time
SECRET_SHORT_PATTERN = re.compile(r'^\$\{secret:([a-zA-Z0-9_-]+)\}$')

# Pattern for explicit user secret reference: ${secret:username:secretname}
# Username: alphanumeric, underscores, and @ (for service accounts like _pods_testuser_admin or user@domain)
# Secret name: alphanumeric, underscores, hyphens
SECRET_EXPLICIT_PATTERN = re.compile(r'^\$\{secret:([a-zA-Z0-9_@]+):([a-zA-Z0-9_-]+)\}$')

# Pattern for placeholder with default: ${pods:default:value} or ${pods:default:value:?description}
# Description is optional and MUST start with ? if provided
# Group 1: default value (can be empty), Group 2: optional ?description (may be None)
PLACEHOLDER_DEFAULT_PATTERN = re.compile(r'^\$\{pods:default:([^:}]*)(?::([^}]+))?\}$')

# Pattern for required placeholder: ${:?description}
PLACEHOLDER_REQUIRED_PATTERN = re.compile(r'^\$\{:\?([^}]+)\}$')

# Pattern for inline placeholders within literal strings: ${:?description} or ${pods:default:val} or ${pods:default:val:?desc}
# Note: default value can be empty, e.g., ${pods:default::?description}
INLINE_PLACEHOLDER_PATTERN = re.compile(r'\$\{(pods:default:[^:}]*(?::\?[^}]+)?|:\?[^}]+)\}')

# Pattern for inline secret references in strings: ${pods:secrets:KEY} or ${pods:secrets:KEY:?description}
# Description is informational only and stripped during interpolation
INLINE_SECRET_PATTERN = re.compile(r'\$\{pods:secrets:([a-zA-Z0-9_-]+)(?::\?([^}]+))?\}')

# Pattern for pod networking references: ${pods:networking:netname:field}
# Fields: url, hostname, port, protocol, tapis_url
POD_NETWORKING_PATTERN = re.compile(r'\$\{pods:networking:([a-zA-Z0-9_-]+):([a-zA-Z_]+)\}')

# Shorthand for ${pods:networking:default:url}
POD_URL_SHORTHAND_PATTERN = re.compile(r'\$\{pods:url\}')

# Shorthand for ${pods:networking:default:tapis_url}
POD_TAPIS_URL_SHORTHAND_PATTERN = re.compile(r'\$\{pods:tapis_url\}')

# Shorthand for pod_id: ${pods:pod_id}
POD_ID_SHORTHAND_PATTERN = re.compile(r'\$\{pods:pod_id\}')

# Pattern for random password generation: ${pods:random:length}
# Length must be 8-128 characters
RANDOM_PASSWORD_PATTERN = re.compile(r'\$\{pods:random:(\d+)\}')

# Inline pattern for short secret reference (for use in strings, not just full match)
SECRET_SHORT_INLINE_PATTERN = re.compile(r'\$\{secret:([a-zA-Z0-9_-]+)\}')


def expand_short_secret_references(secret_map: Dict[str, str], actor: str) -> Dict[str, str]:
    """
    Expand short secret references ${secret:name} to explicit form ${secret:username:name}.
    
    This should be called at pod creation/update time to persist the explicit form,
    so later resolution doesn't need to know who created the pod.
    
    Args:
        secret_map: Dict mapping keys to values (may contain ${secret:name} patterns)
        actor: Username to use for expansion
        
    Returns:
        Dict with short references expanded to explicit form
        
    Example:
        secret_map = {"DB_PASS": "${secret:mydbpass}"}
        expanded = expand_short_secret_references(secret_map, "jsmith")
        # expanded = {"DB_PASS": "${secret:jsmith:mydbpass}"}
    """
    if not secret_map or not actor:
        return secret_map or {}
    
    expanded = {}
    for key, value in secret_map.items():
        if not isinstance(value, str):
            expanded[key] = value
            continue
        
        # Replace all ${secret:name} with ${secret:actor:name}
        def replace_short(match):
            secret_name = match.group(1)
            return f"${{secret:{actor}:{secret_name}}}"
        
        expanded[key] = SECRET_SHORT_INLINE_PATTERN.sub(replace_short, value)
    
    return expanded


@dataclass
class SecretReference:
    """Parsed secret reference from secret_map value."""
    raw_value: str
    is_placeholder: bool
    is_required: bool  # True if no default value provided
    secret_id: Optional[str]  # The actual secret name to look up
    default_value: Optional[str]  # Default value if placeholder
    description: Optional[str]  # Description from placeholder
    is_user_secret: bool = False  # True if ${secret:...} format
    secret_owner: Optional[str] = None  # Username (from explicit form or auto-filled)
    is_literal: bool = False  # True if literal string (no ${} at root level)
    inline_placeholders: Optional[List[Dict[str, Any]]] = None  # Inline ${} in literals


def parse_secret_reference(value: str, actor: str = None) -> Tuple[Optional[SecretReference], Optional[str]]:
    """
    Parse a secret_map value into a SecretReference.
    
    All dynamic content must use ${} wrapper:
        - ${secret:name} - Short form, auto-expands with actor's username
        - ${secret:user:name} - Explicit user (must match actor)
        - ${pods:default:value:?description} - Placeholder with default
        - ${:?description} - Required placeholder
        - Anything else is a literal string (may contain inline placeholders)
    
    Args:
        value: The secret_map value to parse
        actor: Current user (required for short secret form auto-expansion)
        
    Returns:
        Tuple of (SecretReference, error_message)
        - On success: (SecretReference, None)
        - On failure: (None, error_message)
        
    Examples:
        >>> parse_secret_reference("${secret:mydbsecret}", actor="jsmith")
        (SecretReference(raw_value='...', is_user_secret=True, 
                        secret_owner='jsmith', secret_id='mydbsecret', ...), None)
        
        >>> parse_secret_reference("${secret:jsmith:mydbsecret}", actor="jsmith")
        (SecretReference(..., is_user_secret=True, secret_owner='jsmith', ...), None)
        
        >>> parse_secret_reference("${:?API key}")
        (SecretReference(..., is_placeholder=True, is_required=True, ...), None)
        
        >>> parse_secret_reference("${pods:default:redis:?Redis URL}")
        (SecretReference(..., is_placeholder=True, is_required=False,
                        default_value='redis', ...), None)
        
        >>> parse_secret_reference("literal text ${:?answer here}")
        (SecretReference(..., is_literal=True, inline_placeholders=[...], ...), None)
    """
    # Check for short secret reference: ${secret:secretname}
    short_match = SECRET_SHORT_PATTERN.match(value)
    if short_match:
        secret_id = short_match.group(1)
        if not actor:
            return (None, "Short secret form '${secret:name}' requires actor context for auto-expansion.")
        return (SecretReference(
            raw_value=value,
            is_placeholder=False,
            is_required=True,
            secret_id=secret_id,
            default_value=None,
            description=None,
            is_user_secret=True,
            secret_owner=actor  # Auto-fill with current user
        ), None)
    
    # Check for explicit secret reference: ${secret:username:secretname}
    explicit_match = SECRET_EXPLICIT_PATTERN.match(value)
    if explicit_match:
        username = explicit_match.group(1)
        secret_id = explicit_match.group(2)
        return (SecretReference(
            raw_value=value,
            is_placeholder=False,
            is_required=True,
            secret_id=secret_id,
            default_value=None,
            description=None,
            is_user_secret=True,
            secret_owner=username
        ), None)
    
    # Check for required placeholder: ${:?description}
    required_match = PLACEHOLDER_REQUIRED_PATTERN.match(value)
    if required_match:
        description = required_match.group(1)
        return (SecretReference(
            raw_value=value,
            is_placeholder=True,
            is_required=True,
            secret_id=None,
            default_value=None,
            description=description,
            is_user_secret=False,
            secret_owner=None
        ), None)
    
    # Check for placeholder with default: ${pods:default:value} or ${pods:default:value:?description}
    default_match = PLACEHOLDER_DEFAULT_PATTERN.match(value)
    if default_match:
        default_value = default_match.group(1)
        desc_part = default_match.group(2)  # May be None if description not provided
        description = None
        if desc_part:
            # Description must start with ? for consistency
            if not desc_part.startswith('?'):
                return (None, f"Invalid placeholder format: '{value}'. Description must start with '?' (e.g., ${{pods:default:value:?description}}).")
            description = desc_part[1:]  # Strip the leading ?
        return (SecretReference(
            raw_value=value,
            is_placeholder=True,
            is_required=(default_value == ''),
            secret_id=None,
            default_value=default_value if default_value else None,
            description=description,
            is_user_secret=False,
            secret_owner=None
        ), None)
    
    # Check for literal strings (may contain inline placeholders)
    # Find all inline ${} patterns
    inline_placeholders = []
    for match in INLINE_PLACEHOLDER_PATTERN.finditer(value):
        content = match.group(1)
        if content.startswith(':?'):
            # Required placeholder: ${:?description}
            inline_placeholders.append({
                'match': match.group(0),
                'type': 'required',
                'description': content[2:],
                'default_value': None
            })
        elif content.startswith('pods:default:'):
            # Default placeholder: ${pods:default:value} or ${pods:default:value:?description}
            rest = content[13:]  # Strip 'pods:default:'
            parts = rest.split(':?', 1)
            default_val = parts[0]
            desc = parts[1] if len(parts) == 2 else None
            inline_placeholders.append({
                'match': match.group(0),
                'type': 'default',
                'default_value': default_val,
                'description': desc
            })
    
    # Return as literal with optional inline placeholders
    return (SecretReference(
        raw_value=value,
        is_placeholder=False,
        is_required=False,
        secret_id=None,
        default_value=None,
        description=None,
        is_user_secret=False,
        secret_owner=None,
        is_literal=True,
        inline_placeholders=inline_placeholders if inline_placeholders else None
    ), None)


def get_placeholder_warnings(secret_map: Dict[str, str], actor: str = None) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Check secret_map for unresolved placeholders and return warnings.
    
    Args:
        secret_map: Dict mapping env var names to secret references
        actor: Current user for short secret form validation
        
    Returns:
        Tuple of (warnings list, parsing errors list)
        - warnings: List of warning dicts with env_var, placeholder, and description
        - errors: List of parsing error messages for invalid formats
    """
    warnings = []
    errors = []
    for env_var, value in secret_map.items():
        ref, error = parse_secret_reference(value, actor=actor)
        if error:
            errors.append(f"Key '{env_var}': {error}")
            continue
        
        # Handle standalone placeholders
        if ref.is_placeholder:
            warnings.append({
                "env_var": env_var,
                "placeholder": ref.raw_value,
                "description": ref.description or "No description provided",
                "has_default": ref.default_value is not None,
                "default_value": ref.default_value
            })
        
        # Handle literals with inline placeholders
        elif ref.is_literal and ref.inline_placeholders:
            for ph in ref.inline_placeholders:
                warnings.append({
                    "env_var": env_var,
                    "placeholder": ph['match'],
                    "description": ph['description'],
                    "has_default": ph['default_value'] is not None,
                    "default_value": ph['default_value']
                })
    return (warnings, errors)


def validate_template_secret_map(
    secret_map: Dict[str, str],
    actor: str = None
) -> Tuple[bool, List[Dict[str, str]]]:
    """
    Validate that a template's secret_map contains only placeholders, not direct secret references.
    
    Templates should define placeholders that users override with actual secrets when creating pods.
    Direct secret references like ${secret:name} are not allowed in templates because:
    1. Templates are shared - they shouldn't embed user-specific secrets
    2. Templates define structure - pods provide the actual secret bindings
    
    Valid template secret_map values:
        - ${pods:default:value:?description} - Placeholder with default value
        - ${:?description} - Required placeholder (no default)
        - Literal strings with inline placeholders
    
    Invalid for templates:
        - ${secret:name} - Direct secret reference
        - ${secret:user:name} - Explicit user secret reference
    
    Args:
        secret_map: Dict mapping keys to values (placeholders or literals)
        actor: Current user for parsing context
        
    Returns:
        Tuple of (is_valid, list of error dicts with key, value, and description)
        Each error dict contains:
        - key: The secret_map key with the invalid reference
        - value: The invalid value
        - description: User-friendly description of the issue
    """
    errors = []
    
    if not secret_map:
        return (True, [])
    
    for key, value in secret_map.items():
        if not isinstance(value, str):
            continue
        
        ref, parse_error = parse_secret_reference(value, actor=actor)
        
        if parse_error:
            errors.append({
                "key": key,
                "value": value,
                "description": parse_error
            })
            continue
        
        # Check for direct secret references - NOT allowed in templates
        if ref.is_user_secret:
            # Check if it has description syntax: ${secret:name:?description}
            # Try to extract description from the value
            desc_match = re.search(r':(\?[^}]+)\}$', value)
            if desc_match:
                description = desc_match.group(1)[1:]  # Strip leading ?
                errors.append({
                    "key": key,
                    "value": value,
                    "description": f"Templates cannot contain direct secret references. "
                                   f"User provided description: '{description}'. "
                                   f"Use '${{:?{description}}}' for a required placeholder or "
                                   f"'${{default:value:?{description}}}' for an optional placeholder."
                })
            else:
                errors.append({
                    "key": key,
                    "value": value,
                    "description": f"Templates cannot contain direct secret references like '{value}'. "
                                   f"Templates define placeholders that pod creators override with their secrets. "
                                   f"Use '${{:?Describe the secret needed}}' for a required placeholder or "
                                   f"'${{default:fallback_value:?Describe the secret}}' for an optional placeholder."
                })
    
    return (len(errors) == 0, errors)


def validate_environment_placeholders(
    environment_variables: Dict[str, Any],
    secret_map: Dict[str, str]
) -> Tuple[bool, List[str]]:
    """
    Validate that all ${pods:secrets:KEY} references in environment_variables
    have corresponding entries in secret_map.
    
    This should be called at pod creation/update time to catch missing references
    early rather than at pod start time.
    
    Args:
        environment_variables: Dict of environment variables (may contain placeholders)
        secret_map: Dict mapping keys to secret references
        
    Returns:
        Tuple of (is_valid, list of error messages)
        
    Examples:
        >>> env = {"DB_URL": "postgres://${pods:secrets:DB_PASSWORD}@localhost"}
        >>> secret_map = {"DB_PASSWORD": "my_secret"}
        >>> validate_environment_placeholders(env, secret_map)
        (True, [])
        
        >>> env = {"DB_URL": "postgres://${pods:secrets:MISSING}@localhost"}
        >>> secret_map = {}
        >>> validate_environment_placeholders(env, secret_map)
        (False, ["Environment variable 'DB_URL' references secret_map key 'MISSING' which does not exist."])
    """
    errors = []
    secret_map_keys = set(secret_map.keys()) if secret_map else set()
    
    if not environment_variables:
        return (True, [])
    
    for var_name, value in environment_variables.items():
        if not isinstance(value, str):
            continue
            
        # Find all ${pods:secrets:KEY} or ${pods:secrets:KEY:?description} references
        # INLINE_SECRET_PATTERN returns tuples of (key, description) where description may be empty
        matches = INLINE_SECRET_PATTERN.findall(value)
        for match in matches:
            # match is a tuple: (key, description) - we only need the key
            secret_key = match[0] if isinstance(match, tuple) else match
            if secret_key not in secret_map_keys:
                errors.append(
                    f"Environment variable '{var_name}' references secret_map key "
                    f"'{secret_key}' which does not exist in secret_map."
                )
    
    return (len(errors) == 0, errors)


# Patterns that are only valid in secret_map, not in environment_variables or config
SECRET_MAP_ONLY_PATTERNS = [
    (re.compile(r'\$\{pods:default:[^}]*\}'), 'pods:default', 'default value placeholders'),
    (re.compile(r'\$\{pods:networking:[^}]+\}'), 'pods:networking', 'pod networking references'),
    (re.compile(r'\$\{pods:url\}'), 'pods:url', 'pod URL shorthand'),
    (re.compile(r'\$\{pods:tapis_url\}'), 'pods:tapis_url', 'Tapis base URL shorthand'),
    (re.compile(r'\$\{pods:random:\d+\}'), 'pods:random', 'random password generation'),
    (re.compile(r'\$\{pods:pod_id\}'), 'pods:pod_id', 'pod ID reference'),
]


def get_config_secret_map_warnings(
    environment_variables: Dict[str, Any]
) -> List[Dict[str, str]]:
    """
    Check environment_variables (config values) for patterns that are only valid in secret_map.
    
    These patterns are resolved in secret_map and should be referenced via ${pods:secrets:KEY}
    in environment_variables. Using them directly in environment_variables won't work.
    
    Args:
        environment_variables: Dict of environment variables to check
        
    Returns:
        List of warning dicts with env_var, pattern, and suggestion
        
    Patterns that generate warnings:
        - ${pods:default:...} - Use in secret_map, reference via ${pods:secrets:KEY}
        - ${pods:networking:...} - Use in secret_map, reference via ${pods:secrets:KEY}
        - ${pods:url} - Use in secret_map, reference via ${pods:secrets:KEY}
        - ${pods:random:N} - Use in secret_map, reference via ${pods:secrets:KEY}
    """
    warnings = []
    
    if not environment_variables:
        return warnings
    
    for var_name, value in environment_variables.items():
        if not isinstance(value, str):
            continue
        
        for pattern, pattern_name, description in SECRET_MAP_ONLY_PATTERNS:
            if pattern.search(value):
                warnings.append({
                    "env_var": var_name,
                    "pattern": pattern_name,
                    "value": value,
                    "message": f"'{pattern_name}' syntax ({description}) is only valid in secret_map, "
                               f"not environment_variables. Define the value in secret_map and reference "
                               f"it here using ${{pods:secrets:KEY}}."
                })
    
    return warnings


def validate_secret_map(
    secret_map: Dict[str, str],
    site_id: str,
    actor: str,
    tenant_id: str = None,
    pod_id: str = None
) -> Tuple[bool, List[str]]:
    """
    Validate that all secret references in secret_map use valid format and
    reference secrets owned by the actor.
    
    Secret notation:
        - ${secret:name} - Auto-expands with actor's username
        - ${secret:user:name} - Explicit user (must match actor)
    
    Args:
        secret_map: Dict mapping env var names to secret references
        site_id: Site ID for secret lookup
        actor: Username performing the action (must match username in reference)
        tenant_id: Tenant ID for logging (optional)
        pod_id: Pod ID for logging (optional)
        
    Returns:
        Tuple of (is_valid, list of error messages)
    """
    errors = []
    
    for env_var, value in secret_map.items():
        ref, parse_error = parse_secret_reference(value, actor=actor)
        
        # Check for parsing errors first - invalid format
        if parse_error:
            errors.append(f"Key '{env_var}': {parse_error}")
            log_secret_event(
                event_type="SECRET_VALIDATION_FAILED",
                secret_id=value,
                actor=actor,
                pod_id=pod_id,
                tenant_id=tenant_id or g.request_tenant_id,
                site_id=site_id,
                details={"reason": "Invalid format", "env_var": env_var, "error": parse_error}
            )
            continue
        
        # Skip placeholders - they'll use defaults or fail at start time
        if ref.is_placeholder:
            continue
        
        # Skip literals - they're just plain text
        if ref.is_literal:
            continue
        
        # For user secrets, validate ownership from the notation itself
        if ref.is_user_secret:
            if ref.secret_owner != actor:
                error_msg = (
                    f"Key '{env_var}': Secret reference '{value}' specifies user '{ref.secret_owner}' "
                    f"but you are '{actor}'. You can only reference your own secrets."
                )
                errors.append(error_msg)
                log_secret_event(
                    event_type="SECRET_VALIDATION_FAILED",
                    secret_id=ref.secret_id,
                    actor=actor,
                    pod_id=pod_id,
                    tenant_id=tenant_id or g.request_tenant_id,
                    site_id=site_id,
                    details={
                        "reason": "Ownership mismatch in reference",
                        "env_var": env_var,
                        "reference_owner": ref.secret_owner,
                        "requesting_user": actor
                    }
                )
                continue
            
            # Check if secret exists in database
            secret = Secret.db_get_with_pk(ref.secret_id, tenant="siteadmintable", site=site_id)
            if not secret:
                error_msg = f"Key '{env_var}': Secret '{ref.secret_id}' not found."
                errors.append(error_msg)
                log_secret_event(
                    event_type="SECRET_VALIDATION_FAILED",
                    secret_id=ref.secret_id,
                    actor=actor,
                    pod_id=pod_id,
                    tenant_id=tenant_id or g.request_tenant_id,
                    site_id=site_id,
                    details={"reason": "Secret not found", "env_var": env_var}
                )
                continue
            
            # Double-check DB ownership matches (defense-in-depth)
            if secret.added_by != actor:
                error_msg = (
                    f"Key '{env_var}': Secret '{ref.secret_id}' is owned by '{secret.added_by}' "
                    f"in the database, not '{actor}'."
                )
                errors.append(error_msg)
                log_secret_event(
                    event_type="SECRET_VALIDATION_FAILED",
                    secret_id=ref.secret_id,
                    sk_secret_name=secret.sk_secret_name,
                    actor=actor,
                    pod_id=pod_id,
                    tenant_id=tenant_id or g.request_tenant_id,
                    site_id=site_id,
                    details={
                        "reason": "DB ownership mismatch",
                        "env_var": env_var,
                        "secret_owner": secret.added_by,
                        "requesting_user": actor
                    }
                )
                continue
                
            logger.debug(f"Validated secret '{ref.secret_id}' for env var '{env_var}' - owned by '{actor}'")
    
    return (len(errors) == 0, errors)


def resolve_secret_map(
    secret_map: Dict[str, str],
    site_id: str,
    tenant_id: str,
    actor: str = None,
    pod_id: str = None,
    pod: Any = None
) -> Tuple[Dict[str, str], List[str]]:
    """
    Resolve all secret references in secret_map to their actual values.
    
    This is called at pod start time to fetch actual secret values from SK.
    Results are passed to the spawner for injection into the pod.
    
    Resolution order:
    1. Random passwords (${pods:random:N}) - generates and persists to DB
    2. Pod networking (${pods:networking:name:field}, ${pods:url}) - resolves pod URLs
    3. SK secrets (${secret:name}) - fetches from Security Kernel
    
    Args:
        secret_map: Dict mapping env var names to secret references
        site_id: Site ID for secret lookup
        tenant_id: Tenant ID of the pod owner
        actor: Username performing the action. If None, uses owner from secret notation.
               When provided, secret owner must match actor (API mode).
               When None, trusts the owner embedded in secret notation (health mode).
        pod_id: Pod ID for logging
        pod: Optional Pod object for networking resolution and random password persistence
        
    Returns:
        Tuple of (resolved_secrets dict, list of error messages)
        resolved_secrets maps env var names to actual secret values
    """
    resolved = {}
    errors = []
    
    # Working copy of secret_map that gets progressively resolved
    working_map = dict(secret_map)
    
    # Step 1: Resolve random passwords first (persists to DB)
    if pod:
        working_map, random_errors, _ = resolve_random_passwords(working_map, pod, actor)
        errors.extend(random_errors)
    
    # Step 2: Resolve pod networking references
    if pod:
        working_map, networking_errors = resolve_pod_networking(working_map, pod)
        errors.extend(networking_errors)
    
    # Step 3: Resolve SK secrets and other patterns
    for env_var, value in working_map.items():
        ref, parse_error = parse_secret_reference(value, actor=actor)
        
        if parse_error:
            errors.append(f"Key '{env_var}': {parse_error}")
            log_secret_event(
                event_type="SECRET_RESOLUTION_FAILED",
                secret_id=value,
                actor=actor or "unknown",
                pod_id=pod_id,
                tenant_id=tenant_id,
                site_id=site_id,
                details={"reason": "Invalid format", "error": parse_error}
            )
            continue
        
        if ref.is_placeholder:
            if ref.is_required:
                # Required placeholder with no default - fail
                errors.append(
                    f"Required secret for env var '{env_var}' not provided. "
                    f"Description: {ref.description}"
                )
                # Log failure
                log_secret_event(
                    event_type="SECRET_RESOLUTION_FAILED",
                    secret_id=env_var,
                    actor=actor or "unknown",
                    pod_id=pod_id,
                    tenant_id=tenant_id,
                    site_id=site_id,
                    details={"reason": "Required placeholder not overridden", 
                            "description": ref.description}
                )
            else:
                # Use default value
                resolved[env_var] = ref.default_value
                logger.debug(f"Using default value for env var '{env_var}'")
            continue
        
        # Literal value - use as-is
        if ref.is_literal:
            resolved[env_var] = ref.raw_value
            logger.debug(f"Using literal value for env var '{env_var}'")
            continue
        
        # User secret reference - fetch from SK
        if ref.is_user_secret:
            # The effective actor is always the secret owner from notation
            effective_actor = ref.secret_owner
            
            # If actor provided (API mode), verify it matches the secret owner
            if actor and effective_actor != actor:
                errors.append(
                    f"Key '{env_var}': Secret reference specifies user '{effective_actor}' "
                    f"but requesting user is '{actor}'."
                )
                log_secret_event(
                    event_type="SECRET_RESOLUTION_FAILED",
                    secret_id=ref.secret_id,
                    actor=actor,
                    pod_id=pod_id,
                    tenant_id=tenant_id,
                    site_id=site_id,
                    details={
                        "reason": "Ownership mismatch at resolution",
                        "reference_owner": effective_actor,
                        "requesting_user": actor
                    }
                )
                continue
            
            try:
                secret = Secret.db_get_with_pk(ref.secret_id, tenant="siteadmintable", site=site_id)
                if not secret:
                    errors.append(f"Key '{env_var}': Secret '{ref.secret_id}' not found.")
                    log_secret_event(
                        event_type="SECRET_RESOLUTION_FAILED",
                        secret_id=ref.secret_id,
                        actor=effective_actor,
                        pod_id=pod_id,
                        tenant_id=tenant_id,
                        site_id=site_id,
                        details={"reason": "Secret not found in database"}
                    )
                    continue
                
                # Double-check DB ownership (defense-in-depth)
                if secret.added_by != effective_actor:
                    errors.append(
                        f"Key '{env_var}': Secret '{ref.secret_id}' is owned by "
                        f"'{secret.added_by}' in database, not '{effective_actor}'."
                    )
                    log_secret_event(
                        event_type="SECRET_RESOLUTION_FAILED",
                        secret_id=ref.secret_id,
                        sk_secret_name=secret.sk_secret_name,
                        actor=effective_actor,
                        pod_id=pod_id,
                        tenant_id=tenant_id,
                        site_id=site_id,
                        details={
                            "reason": "DB ownership mismatch at resolution",
                            "secret_owner": secret.added_by,
                            "requesting_user": effective_actor
                        }
                    )
                    continue
                
                # Fetch actual value from SK using service account
                result = t.sk.readSecret(
                    secretType='user',
                    secretName=secret.sk_secret_name,
                    tenant=PODS_SERVICE_TENANT,
                    user=PODS_SERVICE_ACCOUNT,
                    _tapis_set_x_headers_from_service=True
                )
                secret_value = result.secretMap.get('secret_value', '')
                resolved[env_var] = secret_value
                
                # Log successful injection
                log_secret_event(
                    event_type="SECRET_INJECTED",
                    secret_id=ref.secret_id,
                    sk_secret_name=secret.sk_secret_name,
                    actor=effective_actor,
                    pod_id=pod_id,
                    tenant_id=tenant_id,
                    site_id=site_id,
                    details={"env_var": env_var}
                )
                logger.debug(f"Resolved secret '{ref.secret_id}' for env var '{env_var}'")
                
            except Exception as e:
                error_msg = f"Key '{env_var}': Failed to resolve secret '{ref.secret_id}': {str(e)}"
                errors.append(error_msg)
                log_secret_event(
                    event_type="SECRET_RESOLUTION_FAILED",
                    secret_id=ref.secret_id,
                    actor=effective_actor,
                    pod_id=pod_id,
                    tenant_id=tenant_id,
                    site_id=site_id,
                    details={"reason": "Exception during resolution", "error": str(e)}
                )
    
    return (resolved, errors)


def resolve_random_passwords(
    secret_map: Dict[str, str],
    pod: Any,
    actor: str
) -> Tuple[Dict[str, str], List[str], bool]:
    """
    Resolve ${pods:random:N} patterns in secret_map by generating random passwords.
    
    Random passwords are a ONE-TIME resolution. Once generated and persisted to
    pod.secret_map, subsequent calls will return the persisted value rather than
    generating new passwords.
    
    Args:
        secret_map: Dict mapping keys to values (may contain ${pods:random:N})
        pod: Pod object for DB persistence and checking existing resolved values
        actor: Username for logging
        
    Returns:
        Tuple of (resolved dict, list of errors, bool indicating if DB was updated)
        
    Example:
        secret_map = {"DB_PASS": "${pods:random:32}"}
        resolved, errors, updated = resolve_random_passwords(secret_map, pod, "user")
        # resolved = {"DB_PASS": "aB3xK9..."}  (32 char random string)
        # pod.secret_map is now {"DB_PASS": "aB3xK9..."} in DB
        # Subsequent calls return the same password, not a new one
    """
    import secrets
    import string
    
    resolved = dict(secret_map)
    errors = []
    db_updates_needed = False
    generated_keys = []  # List of (key, length) tuples for logging
    
    # Get already-resolved values from pod.secret_map if available
    # This prevents regenerating passwords on subsequent calls
    existing_resolved = {}
    if pod and pod.secret_map:
        existing_resolved = dict(pod.secret_map)
    
    for key, value in secret_map.items():
        if not isinstance(value, str):
            continue
            
        match = RANDOM_PASSWORD_PATTERN.fullmatch(value)
        if match:
            length = int(match.group(1))
            
            # Check if this key was already resolved in pod.secret_map
            # If the stored value doesn't match the random pattern, it's already resolved
            if key in existing_resolved:
                existing_value = existing_resolved[key]
                if not RANDOM_PASSWORD_PATTERN.fullmatch(str(existing_value)):
                    # Already resolved - use the existing value
                    resolved[key] = existing_value
                    logger.debug(f"Using existing resolved random password for key '{key}' in pod '{pod.pod_id}'")
                    continue
            
            # Validate length
            if length < 8:
                errors.append(f"Key '{key}': Random password length must be at least 8 characters, got {length}")
                continue
            if length > 128:
                errors.append(f"Key '{key}': Random password length must not exceed 128 characters, got {length}")
                continue
            
            # Generate secure random password
            alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
            password = ''.join(secrets.choice(alphabet) for _ in range(length))
            
            resolved[key] = password
            generated_keys.append((key, length))
            db_updates_needed = True
            
            pod_id = pod.pod_id if pod else 'unknown'
            logger.info(f"Generated random password for key '{key}' (length={length}) in pod '{pod_id}'")
    
    # Bulk update pod.secret_map in DB if randomized password(s) generated
    if db_updates_needed and pod:
        try:
            new_secret_map = dict(pod.secret_map) if pod.secret_map else {}
            for key, length in generated_keys:
                new_secret_map[key] = resolved[key]
            
            pod.secret_map = new_secret_map
            # Create detailed action log with key names and lengths
            log_entries = [f"{key} (length: {length})" for key, length in generated_keys]
            pod.db_update(log=f"Generated random password(s): {', '.join(log_entries)}")
            logger.debug(f"Persisted random passwords to pod '{pod.pod_id}' secret_map")
        except Exception as e:
            errors.append(f"Failed to persist random passwords to database: {str(e)}")
            logger.error(f"Failed to persist random passwords for pod '{pod.pod_id}': {e}")
    
    return (resolved, errors, db_updates_needed)


def resolve_pod_networking(
    secret_map: Dict[str, str],
    pod: Any
) -> Tuple[Dict[str, str], List[str]]:
    """
    Resolve ${pods:networking:name:field}, ${pods:url}, ${pods:tapis_url}, and ${pods:pod_id} patterns in secret_map.
    
    Replaces networking references and pod identity with actual values from the pod's config.
    
    Args:
        secret_map: Dict mapping keys to values (may contain networking patterns)
        pod: Pod object with networking configuration
        
    Returns:
        Tuple of (resolved dict, list of errors)
        
    Supported patterns:
        ${pods:pod_id}                       -> "mypod" (the pod's ID)
        ${pods:networking:default:url}       -> "mypod.pods.tenant.tapis.io"
        ${pods:networking:default:hostname}  -> "mypod.pods.tenant.tapis.io"
        ${pods:networking:default:port}      -> "5000"
        ${pods:networking:default:protocol}  -> "http"
        ${pods:networking:default:tapis_url} -> "tenant.tapis.io" (base Tapis URL)
        ${pods:url}                          -> shorthand for ${pods:networking:default:url}
        ${pods:tapis_url}                    -> shorthand for ${pods:networking:default:tapis_url}
        
    Example:
        secret_map = {"CALLBACK": "https://${pods:url}/callback"}
        resolved, errors = resolve_pod_networking(secret_map, pod)
        # resolved = {"CALLBACK": "https://mypod.pods.tacc.tapis.io/callback"}
        
        secret_map = {"TAPIS_BASE": "${pods:tapis_url}"}
        resolved, errors = resolve_pod_networking(secret_map, pod)
        # resolved = {"TAPIS_BASE": "tacc.tapis.io"}
    """
    resolved = dict(secret_map)
    errors = []
    
    if not pod or not hasattr(pod, 'networking'):
        return (resolved, errors)
    
    # Get networking config (handle both dict and object)
    networking = pod.networking
    if hasattr(networking, 'dict'):
        networking = networking.dict()
    elif hasattr(networking, 'model_dump'):
        networking = networking.model_dump()
    
    # Get pod_id for ${pods:pod_id} resolution
    pod_id = getattr(pod, 'pod_id', None) if pod else None

    for key, value in secret_map.items():
        if not isinstance(value, str):
            continue
        
        new_value = value
        
        # Replace ${pods:pod_id} shorthand
        if POD_ID_SHORTHAND_PATTERN.search(new_value):
            if pod_id:
                new_value = POD_ID_SHORTHAND_PATTERN.sub(pod_id, new_value)
            else:
                errors.append(f"Key '{key}': Pod has no pod_id available for ${{pods:pod_id}} resolution")
        
        # Replace ${pods:url} shorthand first
        if POD_URL_SHORTHAND_PATTERN.search(new_value):
            default_net = networking.get('default', {})
            if hasattr(default_net, 'dict'):
                default_net = default_net.dict()
            elif hasattr(default_net, 'model_dump'):
                default_net = default_net.model_dump()
            elif not isinstance(default_net, dict):
                default_net = dict(default_net) if default_net else {}
                
            url = default_net.get('url', '')
            if not url:
                errors.append(f"Key '{key}': Pod has no default networking URL configured")
            else:
                new_value = POD_URL_SHORTHAND_PATTERN.sub(url, new_value)
        
        # Replace ${pods:tapis_url} shorthand
        if POD_TAPIS_URL_SHORTHAND_PATTERN.search(new_value):
            default_net = networking.get('default', {})
            if hasattr(default_net, 'dict'):
                default_net = default_net.dict()
            elif hasattr(default_net, 'model_dump'):
                default_net = default_net.model_dump()
            elif not isinstance(default_net, dict):
                default_net = dict(default_net) if default_net else {}
                
            url = default_net.get('url', '')
            if not url:
                errors.append(f"Key '{key}': Pod has no default networking URL configured")
            elif '.pods.' not in url:
                errors.append(f"Key '{key}': Cannot extract tapis_url from '{url}' - expected format: <pod>.pods.<tapis_base_url>")
            else:
                # Extract base Tapis URL (everything after "pods.")
                tapis_url = url.split('.pods.', 1)[1]
                new_value = POD_TAPIS_URL_SHORTHAND_PATTERN.sub(tapis_url, new_value)
        
        # Replace ${pods:networking:name:field} patterns
        for match in POD_NETWORKING_PATTERN.finditer(new_value):
            net_name = match.group(1)
            field = match.group(2)
            
            net_config = networking.get(net_name)
            if not net_config:
                errors.append(f"Key '{key}': Networking '{net_name}' not found in pod")
                continue
            
            # Convert to dict if needed
            if hasattr(net_config, 'dict'):
                net_config = net_config.dict()
            elif hasattr(net_config, 'model_dump'):
                net_config = net_config.model_dump()
            elif not isinstance(net_config, dict):
                net_config = dict(net_config)
            
            # Map field names - most map directly to net_config keys
            # Special case: tapis_url extracts base URL from full pod URL
            field_mapping = {
                'url': 'url',
                'hostname': 'url',  # hostname is same as url (no protocol prefix)
                'port': 'port',
                'protocol': 'protocol',
                'tapis_url': 'url'  # Will be post-processed to extract base URL
            }
            
            if field not in field_mapping:
                errors.append(f"Key '{key}': Unknown networking field '{field}'. Valid fields: url, hostname, port, protocol, tapis_url")
                continue
            
            field_value = net_config.get(field_mapping[field])
            if field_value is None:
                errors.append(f"Key '{key}': Networking '{net_name}' has no '{field}' configured")
                continue
            
            # Convert to string
            field_value = str(field_value)
            
            # Special handling for tapis_url: extract base URL after "pods."
            # e.g., "mypod.pods.tacc.tapis.io" -> "tacc.tapis.io"
            if field == 'tapis_url':
                if '.pods.' in field_value:
                    # Extract everything after "pods."
                    field_value = field_value.split('.pods.', 1)[1]
                else:
                    errors.append(f"Key '{key}': Cannot extract tapis_url from '{field_value}' - expected format: <pod>.pods.<tapis_base_url>")
                    continue
            
            # Replace the match
            new_value = new_value.replace(match.group(0), field_value)
        
        resolved[key] = new_value
    
    return (resolved, errors)


def inject_secrets_into_env_vars(
    environment_variables: Dict[str, Any],
    resolved_secrets: Dict[str, str],
    fail_on_missing: bool = True
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Process inline secret references in environment variables.
    
    Secrets must be defined in secret_map first, then referenced in
    environment_variables using ${pods:secrets:KEY} or ${pods:secrets:KEY:?description} notation.
    
    The :?description suffix is optional and purely informational - it allows environment
    variable definitions to be self-documenting. Descriptions are stripped during interpolation.
    
    NOTE: This function only processes ${pods:secrets:KEY} references.
    It does NOT automatically add all secret_map keys to environment_variables.
    Users must explicitly reference secrets they want as env vars.
    
    Args:
        environment_variables: Existing environment variables dict
        resolved_secrets: Resolved secret values from resolve_secret_map()
        fail_on_missing: If True, collect errors for unresolved references
        
    Returns:
        Tuple of (processed environment variables dict, list of errors)
        
    Examples:
        # Basic usage
        env_vars = {"DB_URL": "postgres://user:${pods:secrets:DB_PASSWORD}@localhost/db"}
        secrets = {"DB_PASSWORD": "secret123"}
        result, errors = inject_secrets_into_env_vars(env_vars, secrets)
        # result = {"DB_URL": "postgres://user:secret123@localhost/db"}
        
        # With description (description is stripped)
        env_vars = {"DB_URL": "postgres://user:${pods:secrets:DB_PASSWORD:?Database password}@localhost/db"}
        result, errors = inject_secrets_into_env_vars(env_vars, secrets)
        # result = {"DB_URL": "postgres://user:secret123@localhost/db"}
    """
    result = dict(environment_variables) if environment_variables else {}
    errors = []
    
    # NOTE: We intentionally do NOT auto-add all resolved_secrets to result.
    # Secrets are only injected when explicitly referenced via ${pods:secrets:KEY}.
    
    # Replace inline ${pods:secrets:KEY} or ${pods:secrets:KEY:?description} references
    for key, value in list(result.items()):
        if isinstance(value, str):
            missing_refs = []
            
            def replace_inline_secret(match):
                secret_key = match.group(1)
                # description = match.group(2)  # Available if needed for logging/debugging
                if secret_key in resolved_secrets:
                    return resolved_secrets[secret_key]
                # Track missing reference
                missing_refs.append(secret_key)
                if fail_on_missing:
                    logger.error(f"Inline secret reference '{secret_key}' not found in resolved secrets")
                    return match.group(0)  # Leave unreplaced for error reporting
                else:
                    logger.warning(f"Inline secret reference '{secret_key}' not found in resolved secrets")
                    return match.group(0)
            
            result[key] = INLINE_SECRET_PATTERN.sub(replace_inline_secret, value)
            
            # Collect errors for missing references
            for ref in missing_refs:
                errors.append(
                    f"Environment variable '{key}' references secret_map key '{ref}' "
                    f"which was not resolved. Ensure '{ref}' exists in secret_map."
                )
    
    return (result, errors)


def get_secret_map_summary(secret_map: Dict[str, str], actor: str = None) -> Dict[str, Any]:
    """
    Get a summary of the secret_map for display (without exposing values).
    
    Args:
        secret_map: Dict mapping env var names to secret references
        actor: Current user for short secret form auto-expansion
        
    Returns:
        Summary dict with counts and details (no secret values)
    """
    user_secret_refs = []
    placeholders_with_defaults = []
    required_placeholders = []
    literals = []
    literals_with_placeholders = []
    parse_errors = []
    
    for env_var, value in secret_map.items():
        ref, error = parse_secret_reference(value, actor=actor)
        if error:
            parse_errors.append({"env_var": env_var, "error": error})
            continue
        if ref.is_placeholder:
            if ref.is_required:
                required_placeholders.append({
                    "env_var": env_var,
                    "description": ref.description
                })
            else:
                placeholders_with_defaults.append({
                    "env_var": env_var,
                    "description": ref.description,
                    "has_default": True
                })
        elif ref.is_user_secret:
            user_secret_refs.append({
                "env_var": env_var,
                "secret_id": ref.secret_id,
                "secret_owner": ref.secret_owner
            })
        elif ref.is_literal:
            if ref.inline_placeholders:
                literals_with_placeholders.append({
                    "env_var": env_var,
                    "inline_placeholders": len(ref.inline_placeholders)
                })
            else:
                literals.append({"env_var": env_var})
    
    return {
        "total_entries": len(secret_map),
        "user_secret_refs": user_secret_refs,
        "placeholders_with_defaults": placeholders_with_defaults,
        "required_placeholders": required_placeholders,
        "literals": literals,
        "literals_with_placeholders": literals_with_placeholders,
        "parse_errors": parse_errors
    }


def detect_ownership_transfers(
    old_secret_map: Dict[str, str],
    new_secret_map: Dict[str, str],
    new_owner: str
) -> List[Dict[str, Any]]:
    """
    Detect when secret references are being transferred from one owner to another.
    
    This happens when:
    1. A key exists in both old and new secret_map
    2. The old reference specified a different username than the new reference
    3. OR the new reference specifies a different user than new_owner (the current actor)
    
    This function is informational - the transfer is allowed if new_owner matches
    the username in the new reference, but we log it for debugging purposes.
    
    Args:
        old_secret_map: Previous secret_map (may be None or empty)
        new_secret_map: New secret_map being set
        new_owner: The username of the actor setting the new secret_map
        
    Returns:
        List of transfer records with details for logging
    """
    transfers = []
    
    if not old_secret_map or not new_secret_map:
        return transfers
    
    for key, new_value in new_secret_map.items():
        if key not in old_secret_map:
            continue  # New key, not a transfer
            
        old_value = old_secret_map[key]
        if old_value == new_value:
            continue  # No change
        
        old_ref, old_error = parse_secret_reference(old_value, actor=new_owner)
        new_ref, new_error = parse_secret_reference(new_value, actor=new_owner)
        
        # Skip if either has parse errors
        if old_error or new_error:
            continue
        
        # Only track transfers for user secret references
        if not old_ref.is_user_secret or not new_ref.is_user_secret:
            continue
        
        # Detect ownership change
        if old_ref.secret_owner != new_ref.secret_owner:
            transfers.append({
                "key": key,
                "old_reference": old_value,
                "new_reference": new_value,
                "old_owner": old_ref.secret_owner,
                "new_owner": new_ref.secret_owner,
                "old_secret": old_ref.secret_id,
                "new_secret": new_ref.secret_id,
                "actor": new_owner
            })
    
    return transfers


# Generic pattern to detect ANY remaining ${...} syntax after resolution
UNRESOLVED_PATTERN = re.compile(r'\$\{([^}]+)\}')


def detect_unresolved_patterns(
    secret_map: Dict[str, str] = None,
    environment_variables: Dict[str, Any] = None,
    config_contents: List[str] = None
) -> Dict[str, Any]:
    """
    Detect any remaining unresolved ${...} patterns across pod configuration.
    
    This function scans secret_map, environment_variables, and config_content
    for any ${...} patterns that weren't resolved. These indicate:
    - Placeholders that still need values
    - Secret references that failed to resolve  
    - Syntax errors in pattern usage
    
    Args:
        secret_map: Dict of secret_map entries (already "resolved" or with placeholders)
        environment_variables: Dict of environment variables
        config_contents: List of config_content strings from volume_mounts
        
    Returns:
        Dict with:
        - has_unresolved: bool - True if any unresolved patterns found
        - secret_map_unresolved: List of {key, patterns: [{pattern, type}]}
        - env_vars_unresolved: List of {key, patterns: [{pattern, type}]}
        - config_unresolved: List of {index, patterns: [{pattern, type}]}
        - syntax_warnings: List of syntax issue warnings (e.g., ? instead of :?)
        - summary: Human-readable summary string
    """
    result = {
        "has_unresolved": False,
        "secret_map_unresolved": [],
        "env_vars_unresolved": [],
        "config_unresolved": [],
        "syntax_warnings": [],
        "summary": ""
    }
    
    # Pattern to detect common mistake: ${pods:secrets:KEY?desc} instead of ${pods:secrets:KEY:?desc}
    # This matches ${pods:secrets:KEY?...} where ? is NOT preceded by :
    MISSING_COLON_PATTERN = re.compile(r'\$\{pods:secrets:([a-zA-Z0-9_-]+)\?([^}]+)\}')
    
    def classify_pattern(pattern_content: str) -> str:
        """Classify what type of unresolved pattern this is."""
        if pattern_content.startswith('secret:'):
            return 'user_secret'
        elif pattern_content.startswith('pods:secrets:'):
            return 'secret_map_reference'
        elif pattern_content.startswith('pods:default:'):
            return 'default_placeholder'
        elif pattern_content.startswith(':?'):
            return 'required_placeholder'
        elif pattern_content.startswith('pods:networking:'):
            return 'networking_reference'
        elif pattern_content == 'pods:url':
            return 'pod_url'
        elif pattern_content == 'pods:tapis_url':
            return 'tapis_url'
        elif pattern_content == 'pods:pod_id':
            return 'pod_id'
        elif pattern_content.startswith('pods:random:'):
            return 'random_password'
        else:
            return 'unknown'
    
    def find_patterns(value: str) -> List[Dict[str, str]]:
        """Find all ${...} patterns in a string."""
        if not isinstance(value, str):
            return []
        patterns = []
        for match in UNRESOLVED_PATTERN.finditer(value):
            content = match.group(1)
            patterns.append({
                "pattern": match.group(0),
                "type": classify_pattern(content)
            })
        return patterns
    
    def check_syntax_warnings(value: str, location: str) -> None:
        """Check for common syntax mistakes and add warnings."""
        if not isinstance(value, str):
            return
        # Check for ${pods:secrets:KEY?desc} - missing colon before ?
        for match in MISSING_COLON_PATTERN.finditer(value):
            key = match.group(1)
            desc = match.group(2)
            result["syntax_warnings"].append({
                "location": location,
                "issue": "Missing colon before description",
                "found": match.group(0),
                "suggestion": f"Use '${{pods:secrets:{key}:?{desc}}}' (note the ':?' not just '?')",
                "explanation": "Descriptions require ':?' syntax, not just '?'. The pattern ${pods:secrets:KEY?desc} won't match - use ${pods:secrets:KEY:?desc} instead."
            })
    
    # Check secret_map
    if secret_map:
        for key, value in secret_map.items():
            check_syntax_warnings(value, f"secret_map['{key}']")
            patterns = find_patterns(value)
            if patterns:
                result["secret_map_unresolved"].append({
                    "key": key,
                    "patterns": patterns
                })
    
    # Check environment_variables
    if environment_variables:
        for key, value in environment_variables.items():
            check_syntax_warnings(value, f"environment_variables['{key}']")
            patterns = find_patterns(value)
            if patterns:
                result["env_vars_unresolved"].append({
                    "key": key,
                    "patterns": patterns
                })
    
    # Check config_contents
    if config_contents:
        for idx, content in enumerate(config_contents):
            check_syntax_warnings(content, f"config_content[{idx}]")
            patterns = find_patterns(content)
            if patterns:
                result["config_unresolved"].append({
                    "index": idx,
                    "patterns": patterns
                })
    
    # Set has_unresolved flag
    total_unresolved = (
        len(result["secret_map_unresolved"]) + 
        len(result["env_vars_unresolved"]) + 
        len(result["config_unresolved"])
    )
    result["has_unresolved"] = total_unresolved > 0 or len(result["syntax_warnings"]) > 0
    
    # Build summary
    parts = []
    if result["secret_map_unresolved"]:
        keys = [item["key"] for item in result["secret_map_unresolved"]]
        parts.append(f"secret_map keys with unresolved patterns: {', '.join(keys)}")
    if result["env_vars_unresolved"]:
        keys = [item["key"] for item in result["env_vars_unresolved"]]
        parts.append(f"environment_variables with unresolved patterns: {', '.join(keys)}")
    if result["config_unresolved"]:
        parts.append(f"{len(result['config_unresolved'])} config_content(s) with unresolved patterns")
    if result["syntax_warnings"]:
        parts.append(f"{len(result['syntax_warnings'])} syntax warning(s) - check 'syntax_warnings' for details")
    
    if parts:
        result["summary"] = "; ".join(parts)
    else:
        result["summary"] = "All patterns resolved successfully"
    
    return result


def check_pod_unresolved_patterns(
    secret_map: Dict[str, str] = None,
    environment_variables: Dict[str, Any] = None,
    volume_mounts: Dict[str, Any] = None
) -> Optional[Dict[str, Any]]:
    """
    Convenience function to check a pod's configuration for unresolved patterns.
    
    Extracts config_content from volume_mounts and calls detect_unresolved_patterns.
    Returns the unresolved info dict if any patterns found, otherwise None.
    
    Args:
        secret_map: Pod's secret_map dict
        environment_variables: Pod's environment_variables dict
        volume_mounts: Pod's volume_mounts dict (keyed by mount_path)
        
    Returns:
        Dict with unresolved pattern info if any found, None if all resolved
    """
    # Extract config_contents from volume_mounts
    config_contents = []
    if volume_mounts:
        for mount_path, vol_mount in volume_mounts.items():
            if vol_mount is None:
                continue
            config = None
            if hasattr(vol_mount, 'config_content'):
                config = vol_mount.config_content
            elif isinstance(vol_mount, dict):
                config = vol_mount.get('config_content')
            if config:
                config_contents.append(config)
    
    unresolved = detect_unresolved_patterns(
        secret_map=dict(secret_map) if secret_map else None,
        environment_variables=dict(environment_variables) if environment_variables else None,
        config_contents=config_contents if config_contents else None
    )
    
    return unresolved if unresolved["has_unresolved"] else None
