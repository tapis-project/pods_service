"""
Unit tests for secret_utils.py - Secret parsing, validation, and placeholder handling.

These tests cover:
- parse_secret_reference() for all secret_map value formats
- get_placeholder_warnings() for detecting unresolved placeholders
- validate_template_secret_map() for template secret_map validation
- validate_environment_placeholders() for ${pods:secrets:KEY} validation
"""
import sys
import pytest

# Allows us to import pods's modules.
sys.path.append('/home/tapis/service')

from secret_utils import (
    parse_secret_reference,
    get_placeholder_warnings,
    validate_template_secret_map,
    validate_environment_placeholders,
    resolve_random_passwords,
    resolve_pod_networking,
    detect_unresolved_patterns,
    check_pod_unresolved_patterns,
    SecretReference,
    SECRET_SHORT_PATTERN,
    SECRET_EXPLICIT_PATTERN,
    PLACEHOLDER_DEFAULT_PATTERN,
    PLACEHOLDER_REQUIRED_PATTERN,
    POD_NETWORKING_PATTERN,
    POD_URL_SHORTHAND_PATTERN,
    RANDOM_PASSWORD_PATTERN,
)


# ============================================================================
# TEST: parse_secret_reference() - Short secret form ${secret:name}
# ============================================================================

class TestParseSecretReferenceShortForm:
    """Tests for parsing ${secret:name} short secret references."""
    
    def test_short_secret_basic(self):
        """${secret:mysecret} should parse as user secret with auto-filled owner."""
        ref, error = parse_secret_reference("${secret:mysecret}", actor="jsmith")
        
        assert error is None
        assert ref is not None
        assert ref.is_user_secret is True
        assert ref.secret_id == "mysecret"
        assert ref.secret_owner == "jsmith"  # Auto-filled from actor
        assert ref.is_placeholder is False
        assert ref.is_literal is False
    
    def test_short_secret_with_underscores(self):
        """${secret:my_db_secret} should parse correctly."""
        ref, error = parse_secret_reference("${secret:my_db_secret}", actor="testuser")
        
        assert error is None
        assert ref.secret_id == "my_db_secret"
        assert ref.secret_owner == "testuser"
    
    def test_short_secret_with_hyphens(self):
        """${secret:my-api-key} should parse correctly."""
        ref, error = parse_secret_reference("${secret:my-api-key}", actor="testuser")
        
        assert error is None
        assert ref.secret_id == "my-api-key"
    
    def test_short_secret_without_actor_fails(self):
        """Short form without actor should return error."""
        ref, error = parse_secret_reference("${secret:mysecret}", actor=None)
        
        assert error is not None
        assert "requires actor context" in error
        assert ref is None


# ============================================================================
# TEST: parse_secret_reference() - Explicit secret form ${secret:user:name}
# ============================================================================

class TestParseSecretReferenceExplicitForm:
    """Tests for parsing ${secret:username:secretname} explicit references."""
    
    def test_explicit_secret_basic(self):
        """${secret:jsmith:mydbsecret} should parse correctly."""
        ref, error = parse_secret_reference("${secret:jsmith:mydbsecret}", actor="jsmith")
        
        assert error is None
        assert ref.is_user_secret is True
        assert ref.secret_id == "mydbsecret"
        assert ref.secret_owner == "jsmith"
        assert ref.is_placeholder is False
    
    def test_explicit_secret_different_user(self):
        """Explicit form with different user should still parse (validation is separate)."""
        ref, error = parse_secret_reference("${secret:otheruser:theirsecret}", actor="jsmith")
        
        # Parsing succeeds; ownership validation happens elsewhere
        assert error is None
        assert ref.secret_owner == "otheruser"
        assert ref.secret_id == "theirsecret"
    
    def test_explicit_secret_alphanumeric_username(self):
        """Username should be alphanumeric with underscores and @."""
        ref, error = parse_secret_reference("${secret:user123:secret}", actor="test")
        
        assert error is None
        assert ref.secret_owner == "user123"
    
    def test_explicit_secret_with_underscore_in_username(self):
        """Username can have underscores (e.g., service accounts)."""
        ref, error = parse_secret_reference("${secret:_pods_testuser_admin:secret}", actor="test")
        
        assert error is None
        assert ref.secret_owner == "_pods_testuser_admin"
    
    def test_explicit_secret_with_at_in_username(self):
        """Username can have @ (e.g., user@domain)."""
        ref, error = parse_secret_reference("${secret:user@domain:secret}", actor="test")
        
        assert error is None
        assert ref.secret_owner == "user@domain"
    
    def test_explicit_secret_with_underscore_in_secretname(self):
        """Secret name can have underscores."""
        ref, error = parse_secret_reference("${secret:jsmith:my_secret_name}", actor="jsmith")
        
        assert error is None
        assert ref.secret_id == "my_secret_name"


# ============================================================================
# TEST: parse_secret_reference() - Required placeholder ${:?description}
# ============================================================================

class TestParseSecretReferenceRequiredPlaceholder:
    """Tests for parsing ${:?description} required placeholders."""
    
    def test_required_placeholder_basic(self):
        """${:?API key for service} should parse as required placeholder."""
        ref, error = parse_secret_reference("${:?API key for service}")
        
        assert error is None
        assert ref.is_placeholder is True
        assert ref.is_required is True
        assert ref.description == "API key for service"
        assert ref.default_value is None
        assert ref.is_user_secret is False
        assert ref.is_literal is False
    
    def test_required_placeholder_short_description(self):
        """${:?Required} should parse correctly."""
        ref, error = parse_secret_reference("${:?Required}")
        
        assert error is None
        assert ref.is_required is True
        assert ref.description == "Required"
    
    def test_required_placeholder_with_special_chars(self):
        """Descriptions can contain special characters."""
        ref, error = parse_secret_reference("${:?Database password (production - use strong!)}")
        
        assert error is None
        assert ref.description == "Database password (production - use strong!)"


# ============================================================================
# TEST: parse_secret_reference() - Default placeholder ${pods:default:value:?desc}
# ============================================================================

class TestParseSecretReferenceDefaultPlaceholder:
    """Tests for parsing ${pods:default:value:?description} placeholders."""
    
    def test_default_placeholder_basic(self):
        """${pods:default:redis:?Redis hostname} should parse with default value."""
        ref, error = parse_secret_reference("${pods:default:redis:?Redis hostname}")
        
        assert error is None
        assert ref.is_placeholder is True
        assert ref.is_required is False  # Has default, so not required
        assert ref.default_value == "redis"
        assert ref.description == "Redis hostname"
    
    def test_default_placeholder_empty_default(self):
        """${pods:default::?description} with empty default should be marked required."""
        ref, error = parse_secret_reference("${pods:default::?Optional cache host}")
        
        assert error is None
        assert ref.is_placeholder is True
        # Empty default value means it's effectively required
        assert ref.default_value is None
        assert ref.is_required is True
    
    def test_default_placeholder_numeric_value(self):
        """Default value can be numeric."""
        ref, error = parse_secret_reference("${pods:default:5432:?PostgreSQL port}")
        
        assert error is None
        assert ref.default_value == "5432"
        assert ref.description == "PostgreSQL port"
    
    def test_default_placeholder_missing_question_mark(self):
        """Description without ? prefix should return an error."""
        ref, error = parse_secret_reference("${pods:default:value:missing question mark}")
        
        # The pattern matches, but validation fails because description doesn't start with ?
        assert error is not None
        assert "must start with '?'" in error
        assert ref is None


# ============================================================================
# TEST: parse_secret_reference() - Literal strings
# ============================================================================

class TestParseSecretReferenceLiteral:
    """Tests for parsing literal strings (no ${} or with inline placeholders)."""
    
    def test_plain_literal(self):
        """Plain text without ${} should be literal."""
        ref, error = parse_secret_reference("just a plain string")
        
        assert error is None
        assert ref.is_literal is True
        assert ref.is_placeholder is False
        assert ref.is_user_secret is False
        assert ref.inline_placeholders is None
    
    def test_literal_with_inline_required_placeholder(self):
        """Literal with inline ${:?desc} should capture the placeholder."""
        ref, error = parse_secret_reference("my question is ${:?put your answer}")
        
        assert error is None
        assert ref.is_literal is True
        assert ref.inline_placeholders is not None
        assert len(ref.inline_placeholders) == 1
        assert ref.inline_placeholders[0]['type'] == 'required'
        assert ref.inline_placeholders[0]['description'] == 'put your answer'
    
    def test_literal_with_inline_default_placeholder(self):
        """Literal with inline ${pods:default:val:?desc} should capture it."""
        ref, error = parse_secret_reference("Connect to ${pods:default:localhost:?hostname}:5432")
        
        assert error is None
        assert ref.is_literal is True
        assert len(ref.inline_placeholders) == 1
        assert ref.inline_placeholders[0]['type'] == 'default'
        assert ref.inline_placeholders[0]['default_value'] == 'localhost'
        assert ref.inline_placeholders[0]['description'] == 'hostname'
    
    def test_literal_with_multiple_placeholders(self):
        """Multiple inline placeholders should all be captured."""
        ref, error = parse_secret_reference("${:?user}:${:?password}@${pods:default:localhost:?host}")
        
        assert error is None
        assert ref.is_literal is True
        assert len(ref.inline_placeholders) == 3


# ============================================================================
# TEST: get_placeholder_warnings()
# ============================================================================

class TestGetPlaceholderWarnings:
    """Tests for get_placeholder_warnings function."""
    
    def test_empty_secret_map(self):
        """Empty secret_map should return empty warnings."""
        warnings, errors = get_placeholder_warnings({})
        
        assert warnings == []
        assert errors == []
    
    def test_secret_map_with_required_placeholder(self):
        """Required placeholder should generate warning."""
        secret_map = {
            "API_KEY": "${:?Your API key for the service}"
        }
        warnings, errors = get_placeholder_warnings(secret_map, actor="testuser")
        
        assert len(warnings) == 1
        assert warnings[0]['env_var'] == "API_KEY"
        assert warnings[0]['description'] == "Your API key for the service"
        assert warnings[0]['has_default'] is False
        assert errors == []
    
    def test_secret_map_with_default_placeholder(self):
        """Default placeholder should show has_default=True."""
        secret_map = {
            "CACHE_HOST": "${pods:default:redis:?Redis hostname}"
        }
        warnings, errors = get_placeholder_warnings(secret_map, actor="testuser")
        
        assert len(warnings) == 1
        assert warnings[0]['env_var'] == "CACHE_HOST"
        assert warnings[0]['has_default'] is True
        assert warnings[0]['default_value'] == "redis"
    
    def test_secret_map_with_literal_and_inline(self):
        """Literal with inline placeholder should generate warning."""
        secret_map = {
            "GREETING": "Hello ${:?name}, welcome!"
        }
        warnings, errors = get_placeholder_warnings(secret_map, actor="testuser")
        
        assert len(warnings) == 1
        assert warnings[0]['env_var'] == "GREETING"
        assert warnings[0]['description'] == "name"
    
    def test_secret_map_with_no_placeholders(self):
        """Secret references without placeholders should not warn."""
        secret_map = {
            "DB_PASSWORD": "${secret:mydbsecret}"
        }
        warnings, errors = get_placeholder_warnings(secret_map, actor="testuser")
        
        assert len(warnings) == 0
        assert errors == []
    
    def test_multiple_placeholders_multiple_warnings(self):
        """Multiple placeholders should generate multiple warnings."""
        secret_map = {
            "API_KEY": "${:?Your API key}",
            "DB_HOST": "${pods:default:localhost:?Database host}",
            "CACHE_HOST": "${pods:default:redis:?Cache host}"
        }
        warnings, errors = get_placeholder_warnings(secret_map, actor="testuser")
        
        assert len(warnings) == 3
        env_vars = [w['env_var'] for w in warnings]
        assert "API_KEY" in env_vars
        assert "DB_HOST" in env_vars
        assert "CACHE_HOST" in env_vars


# ============================================================================
# TEST: validate_template_secret_map()
# ============================================================================

class TestValidateTemplateSecretMap:
    """Tests for validate_template_secret_map function."""
    
    def test_empty_secret_map_valid(self):
        """Empty secret_map should be valid."""
        is_valid, errors = validate_template_secret_map({})
        
        assert is_valid is True
        assert errors == []
    
    def test_none_secret_map_valid(self):
        """None secret_map should be valid."""
        is_valid, errors = validate_template_secret_map(None)
        
        assert is_valid is True
        assert errors == []
    
    def test_template_with_required_placeholder_valid(self):
        """${:?description} placeholder should be valid in template."""
        secret_map = {
            "DB_PASSWORD": "${:?Database password for PostgreSQL}"
        }
        is_valid, errors = validate_template_secret_map(secret_map, actor="testuser")
        
        assert is_valid is True
        assert errors == []
    
    def test_template_with_default_placeholder_valid(self):
        """${pods:default:value:?desc} placeholder should be valid in template."""
        secret_map = {
            "CACHE_HOST": "${pods:default:redis:?Redis hostname}"
        }
        is_valid, errors = validate_template_secret_map(secret_map, actor="testuser")
        
        assert is_valid is True
        assert errors == []
    
    def test_template_with_literal_valid(self):
        """Plain literal strings should be valid in template."""
        secret_map = {
            "APP_NAME": "my-application",
            "VERSION": "1.0.0"
        }
        is_valid, errors = validate_template_secret_map(secret_map, actor="testuser")
        
        assert is_valid is True
        assert errors == []
    
    def test_template_with_short_secret_reference_invalid(self):
        """${secret:name} direct reference should be INVALID in template."""
        secret_map = {
            "DB_PASSWORD": "${secret:mydbsecret}"
        }
        is_valid, errors = validate_template_secret_map(secret_map, actor="testuser")
        
        assert is_valid is False
        assert len(errors) == 1
        assert errors[0]['key'] == "DB_PASSWORD"
        assert "cannot contain direct secret references" in errors[0]['description']
    
    def test_template_with_explicit_secret_reference_invalid(self):
        """${secret:user:name} explicit reference should be INVALID in template."""
        secret_map = {
            "API_KEY": "${secret:jsmith:myapikey}"
        }
        is_valid, errors = validate_template_secret_map(secret_map, actor="testuser")
        
        assert is_valid is False
        assert len(errors) == 1
        assert errors[0]['key'] == "API_KEY"
        assert "cannot contain direct secret references" in errors[0]['description']
    
    def test_template_mixed_valid_and_invalid(self):
        """Mixed placeholders and secret refs should catch all invalid ones."""
        secret_map = {
            "VALID_PLACEHOLDER": "${:?Required value}",
            "VALID_DEFAULT": "${pods:default:myval:?Optional value}",
            "INVALID_SECRET": "${secret:mydbsecret}",
            "VALID_LITERAL": "just-a-string"
        }
        is_valid, errors = validate_template_secret_map(secret_map, actor="testuser")
        
        assert is_valid is False
        assert len(errors) == 1
        assert errors[0]['key'] == "INVALID_SECRET"
    
    def test_template_multiple_invalid_secrets(self):
        """Multiple invalid secret references should all be reported."""
        secret_map = {
            "SECRET1": "${secret:secret1}",
            "SECRET2": "${secret:jsmith:secret2}"
        }
        is_valid, errors = validate_template_secret_map(secret_map, actor="testuser")
        
        assert is_valid is False
        assert len(errors) == 2
        keys = [e['key'] for e in errors]
        assert "SECRET1" in keys
        assert "SECRET2" in keys


# ============================================================================
# TEST: validate_environment_placeholders()
# ============================================================================

class TestValidateEnvironmentPlaceholders:
    """Tests for validate_environment_placeholders function."""
    
    def test_empty_env_vars_valid(self):
        """Empty environment_variables should be valid."""
        is_valid, errors = validate_environment_placeholders({}, {})
        
        assert is_valid is True
        assert errors == []
    
    def test_none_env_vars_valid(self):
        """None environment_variables should be valid."""
        is_valid, errors = validate_environment_placeholders(None, {})
        
        assert is_valid is True
        assert errors == []
    
    def test_env_var_with_valid_secret_ref(self):
        """${pods:secrets:KEY} with existing KEY should be valid."""
        env_vars = {
            "DATABASE_PASSWORD": "${pods:secrets:DB_PASSWORD}"
        }
        secret_map = {
            "DB_PASSWORD": "${:?Database password}"
        }
        is_valid, errors = validate_environment_placeholders(env_vars, secret_map)
        
        assert is_valid is True
        assert errors == []
    
    def test_env_var_with_missing_secret_ref(self):
        """${pods:secrets:MISSING} without MISSING in secret_map should fail."""
        env_vars = {
            "DATABASE_PASSWORD": "${pods:secrets:MISSING_KEY}"
        }
        secret_map = {
            "OTHER_KEY": "${:?Some other value}"
        }
        is_valid, errors = validate_environment_placeholders(env_vars, secret_map)
        
        assert is_valid is False
        assert len(errors) == 1
        assert "MISSING_KEY" in errors[0]
        assert "does not exist" in errors[0]
    
    def test_env_var_with_inline_secret_ref(self):
        """Inline ${pods:secrets:KEY} in URL should validate."""
        env_vars = {
            "DATABASE_URL": "postgres://user:${pods:secrets:DB_PASSWORD}@localhost/db"
        }
        secret_map = {
            "DB_PASSWORD": "${:?Database password}"
        }
        is_valid, errors = validate_environment_placeholders(env_vars, secret_map)
        
        assert is_valid is True
    
    def test_env_var_with_multiple_refs(self):
        """Multiple ${pods:secrets:X} refs in one value should all validate."""
        env_vars = {
            "CONN_STRING": "Server=${pods:secrets:HOST};User=${pods:secrets:USER};Pass=${pods:secrets:PASS}"
        }
        secret_map = {
            "HOST": "${pods:default:localhost:?Server host}",
            "USER": "${:?Username}",
            "PASS": "${:?Password}"
        }
        is_valid, errors = validate_environment_placeholders(env_vars, secret_map)
        
        assert is_valid is True
    
    def test_env_var_with_one_missing_of_many(self):
        """If one of multiple refs is missing, should report error."""
        env_vars = {
            "CONN_STRING": "Server=${pods:secrets:HOST};User=${pods:secrets:MISSING};Pass=${pods:secrets:PASS}"
        }
        secret_map = {
            "HOST": "${pods:default:localhost:?Server host}",
            "PASS": "${:?Password}"
        }
        is_valid, errors = validate_environment_placeholders(env_vars, secret_map)
        
        assert is_valid is False
        assert len(errors) == 1
        assert "MISSING" in errors[0]
    
    def test_env_var_plain_value_no_refs(self):
        """Plain values without ${pods:secrets:} should be valid."""
        env_vars = {
            "APP_NAME": "my-application",
            "DEBUG": "true"
        }
        secret_map = {}
        is_valid, errors = validate_environment_placeholders(env_vars, secret_map)
        
        assert is_valid is True


# ============================================================================
# TEST: Regex Pattern Tests
# ============================================================================

class TestRegexPatterns:
    """Tests for the regex patterns used in secret parsing."""
    
    def test_short_pattern_valid(self):
        """SHORT pattern should match ${secret:name}."""
        assert SECRET_SHORT_PATTERN.match("${secret:mysecret}")
        assert SECRET_SHORT_PATTERN.match("${secret:my_secret}")
        assert SECRET_SHORT_PATTERN.match("${secret:my-secret}")
    
    def test_short_pattern_invalid(self):
        """SHORT pattern should NOT match explicit form."""
        assert SECRET_SHORT_PATTERN.match("${secret:user:secret}") is None
        assert SECRET_SHORT_PATTERN.match("${secret:}") is None
    
    def test_explicit_pattern_valid(self):
        """EXPLICIT pattern should match ${secret:user:name}."""
        assert SECRET_EXPLICIT_PATTERN.match("${secret:jsmith:mysecret}")
        assert SECRET_EXPLICIT_PATTERN.match("${secret:user123:my_secret}")
        # Underscores in username (service accounts)
        assert SECRET_EXPLICIT_PATTERN.match("${secret:_pods_testuser_admin:mysecret}")
        # @ in username (user@domain)
        assert SECRET_EXPLICIT_PATTERN.match("${secret:user@domain:mysecret}")
    
    def test_explicit_pattern_invalid(self):
        """EXPLICIT pattern should NOT match short form."""
        assert SECRET_EXPLICIT_PATTERN.match("${secret:mysecret}") is None
        # Invalid characters (spaces, special chars other than _ and @) should not match
        assert SECRET_EXPLICIT_PATTERN.match("${secret:user name:secret}") is None
        assert SECRET_EXPLICIT_PATTERN.match("${secret:user!name:secret}") is None
    
    def test_required_pattern_valid(self):
        """REQUIRED pattern should match ${:?description}."""
        assert PLACEHOLDER_REQUIRED_PATTERN.match("${:?Required field}")
        assert PLACEHOLDER_REQUIRED_PATTERN.match("${:?API key for service}")
    
    def test_required_pattern_invalid(self):
        """REQUIRED pattern should NOT match default form."""
        assert PLACEHOLDER_REQUIRED_PATTERN.match("${pods:default:val:?desc}") is None
        assert PLACEHOLDER_REQUIRED_PATTERN.match("${:missing question mark}") is None
    
    def test_default_pattern_valid(self):
        """DEFAULT pattern should match ${pods:default:value:?description}."""
        assert PLACEHOLDER_DEFAULT_PATTERN.match("${pods:default:redis:?Redis host}")
        assert PLACEHOLDER_DEFAULT_PATTERN.match("${pods:default::?Empty default}")
        assert PLACEHOLDER_DEFAULT_PATTERN.match("${pods:default:5432:?Port number}")
    
    def test_default_pattern_invalid(self):
        """DEFAULT pattern should NOT match required form."""
        assert PLACEHOLDER_DEFAULT_PATTERN.match("${:?description}") is None


# ============================================================================
# TEST: Edge Cases
# ============================================================================

class TestEdgeCases:
    """Edge case tests for secret parsing."""
    
    def test_empty_string(self):
        """Empty string should be a literal."""
        ref, error = parse_secret_reference("")
        
        assert error is None
        assert ref.is_literal is True
    
    def test_only_dollar_sign(self):
        """$ alone should be a literal."""
        ref, error = parse_secret_reference("$")
        
        assert error is None
        assert ref.is_literal is True
    
    def test_unclosed_brace(self):
        """${unclosed should be a literal."""
        ref, error = parse_secret_reference("${unclosed")
        
        assert error is None
        assert ref.is_literal is True
    
    def test_nested_braces(self):
        """Nested braces should not cause issues."""
        ref, error = parse_secret_reference("${:?Value with {braces} inside}")
        
        # The regex should handle this - braces in description
        assert error is None
    
    def test_special_chars_in_description(self):
        """Special characters in description should work."""
        ref, error = parse_secret_reference("${:?Password (min 8 chars, must include !@#$%)}")
        
        assert error is None
        assert ref.is_placeholder is True


# ============================================================================
# TEST: New Pattern Matching - Networking and Random Password
# ============================================================================

class TestNewPatterns:
    """Tests for new regex patterns: networking and random password."""
    
    def test_pod_networking_pattern_valid(self):
        """POD_NETWORKING_PATTERN should match ${pods:networking:name:field}."""
        match = POD_NETWORKING_PATTERN.search("${pods:networking:default:url}")
        assert match is not None
        assert match.group(1) == "default"
        assert match.group(2) == "url"
        
        match = POD_NETWORKING_PATTERN.search("${pods:networking:api:port}")
        assert match is not None
        assert match.group(1) == "api"
        assert match.group(2) == "port"
    
    def test_pod_networking_pattern_with_hyphen(self):
        """Networking name can contain hyphens."""
        match = POD_NETWORKING_PATTERN.search("${pods:networking:my-api:hostname}")
        assert match is not None
        assert match.group(1) == "my-api"
    
    def test_pod_url_shorthand_pattern(self):
        """POD_URL_SHORTHAND_PATTERN should match ${pods:url}."""
        match = POD_URL_SHORTHAND_PATTERN.search("${pods:url}")
        assert match is not None
        
        # Should not match longer patterns
        assert POD_URL_SHORTHAND_PATTERN.search("${pods:urls}") is None
    
    def test_random_password_pattern_valid(self):
        """RANDOM_PASSWORD_PATTERN should match ${pods:random:N}."""
        match = RANDOM_PASSWORD_PATTERN.fullmatch("${pods:random:32}")
        assert match is not None
        assert match.group(1) == "32"
        
        match = RANDOM_PASSWORD_PATTERN.fullmatch("${pods:random:128}")
        assert match is not None
        assert match.group(1) == "128"
    
    def test_random_password_pattern_invalid(self):
        """RANDOM_PASSWORD_PATTERN should not match invalid formats."""
        assert RANDOM_PASSWORD_PATTERN.fullmatch("${pods:random}") is None
        assert RANDOM_PASSWORD_PATTERN.fullmatch("${pods:random:abc}") is None


# ============================================================================
# TEST: resolve_random_passwords()
# ============================================================================

class MockPod:
    """Mock pod object for testing."""
    def __init__(self, pod_id="testpod", secret_map=None, networking=None):
        self.pod_id = pod_id
        self.secret_map = secret_map or {}
        self.networking = networking or {"default": {"url": "testpod.pods.tacc.tapis.io", "port": 5000, "protocol": "http"}}
        self._db_updated = False
        self._update_msg = None
        self.action_logs = []
    
    def db_update(self, msg="", log=None):
        self._db_updated = True
        self._update_msg = log or msg
        if log:
            self.action_logs.append(log)


class TestResolveRandomPasswords:
    """Tests for resolve_random_passwords() function."""
    
    def test_generates_password_correct_length(self):
        """Should generate password of requested length."""
        secret_map = {"DB_PASS": "${pods:random:32}"}
        pod = MockPod(secret_map=secret_map)
        
        resolved, errors, updated = resolve_random_passwords(secret_map, pod, "testuser")
        
        assert len(errors) == 0
        assert "DB_PASS" in resolved
        assert len(resolved["DB_PASS"]) == 32
        assert updated is True
    
    def test_generates_different_passwords(self):
        """Each call should generate different passwords (for different pods)."""
        secret_map = {"PASS": "${pods:random:16}"}
        
        pod1 = MockPod(pod_id="pod1", secret_map=secret_map.copy())
        pod2 = MockPod(pod_id="pod2", secret_map=secret_map.copy())
        
        resolved1, _, _ = resolve_random_passwords(secret_map.copy(), pod1, "user")
        resolved2, _, _ = resolve_random_passwords(secret_map.copy(), pod2, "user")
        
        # Extremely unlikely to be equal
        assert resolved1["PASS"] != resolved2["PASS"]
    
    def test_password_min_length_validation(self):
        """Should reject passwords shorter than 8 characters."""
        secret_map = {"PASS": "${pods:random:5}"}
        pod = MockPod(secret_map=secret_map)
        
        resolved, errors, updated = resolve_random_passwords(secret_map, pod, "testuser")
        
        assert len(errors) == 1
        assert "at least 8 characters" in errors[0]
        # Key remains with original pattern value on error
        assert resolved["PASS"] == "${pods:random:5}"
    
    def test_password_max_length_validation(self):
        """Should reject passwords longer than 128 characters."""
        secret_map = {"PASS": "${pods:random:200}"}
        pod = MockPod(secret_map=secret_map)
        
        resolved, errors, updated = resolve_random_passwords(secret_map, pod, "testuser")
        
        assert len(errors) == 1
        assert "not exceed 128" in errors[0]
        # Key remains with original pattern value on error
        assert resolved["PASS"] == "${pods:random:200}"
    
    def test_persists_to_database(self):
        """Should call pod.db_update() to persist generated password."""
        secret_map = {"SECRET": "${pods:random:16}"}
        pod = MockPod(secret_map=secret_map)
        
        resolved, errors, updated = resolve_random_passwords(secret_map, pod, "testuser")
        
        assert pod._db_updated is True
        assert "SECRET" in str(pod._update_msg)
    
    def test_passes_through_non_random_values(self):
        """Non-random values should pass through unchanged."""
        secret_map = {
            "RANDOM": "${pods:random:16}",
            "LITERAL": "plain_value",
            "SECRET_REF": "${secret:mysecret}"
        }
        pod = MockPod(secret_map=secret_map)
        
        resolved, errors, _ = resolve_random_passwords(secret_map, pod, "testuser")
        
        assert len(resolved["RANDOM"]) == 16
        assert resolved["LITERAL"] == "plain_value"
        assert resolved["SECRET_REF"] == "${secret:mysecret}"
    
    def test_multiple_random_passwords(self):
        """Should handle multiple random password fields."""
        secret_map = {
            "PASS1": "${pods:random:16}",
            "PASS2": "${pods:random:32}",
            "PASS3": "${pods:random:8}"
        }
        pod = MockPod(secret_map=secret_map)
        
        resolved, errors, _ = resolve_random_passwords(secret_map, pod, "testuser")
        
        assert len(errors) == 0
        assert len(resolved["PASS1"]) == 16
        assert len(resolved["PASS2"]) == 32
        assert len(resolved["PASS3"]) == 8
    
    def test_password_character_set(self):
        """Generated password should contain expected character types."""
        secret_map = {"PASS": "${pods:random:64}"}
        pod = MockPod(secret_map=secret_map)
        
        resolved, _, _ = resolve_random_passwords(secret_map, pod, "testuser")
        password = resolved["PASS"]
        
        # Should only contain allowed characters
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*")
        assert all(c in allowed for c in password)


# ============================================================================
# TEST: resolve_pod_networking()
# ============================================================================

class TestResolvePodNetworking:
    """Tests for resolve_pod_networking() function."""
    
    def test_resolves_pods_url_shorthand(self):
        """${pods:url} should resolve to default networking URL."""
        secret_map = {"CALLBACK": "https://${pods:url}/callback"}
        pod = MockPod(networking={"default": {"url": "mypod.pods.tacc.tapis.io", "port": 5000}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 0
        assert resolved["CALLBACK"] == "https://mypod.pods.tacc.tapis.io/callback"
    
    def test_resolves_networking_url(self):
        """${pods:networking:default:url} should resolve to URL."""
        secret_map = {"URL": "${pods:networking:default:url}"}
        pod = MockPod(networking={"default": {"url": "test.pods.tapis.io"}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 0
        assert resolved["URL"] == "test.pods.tapis.io"
    
    def test_resolves_networking_hostname(self):
        """${pods:networking:default:hostname} should resolve same as url."""
        secret_map = {"HOST": "${pods:networking:default:hostname}"}
        pod = MockPod(networking={"default": {"url": "test.pods.tapis.io"}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 0
        assert resolved["HOST"] == "test.pods.tapis.io"
    
    def test_resolves_networking_port(self):
        """${pods:networking:default:port} should resolve to port number."""
        secret_map = {"PORT": "${pods:networking:default:port}"}
        pod = MockPod(networking={"default": {"url": "test.tapis.io", "port": 8080}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 0
        assert resolved["PORT"] == "8080"
    
    def test_resolves_networking_protocol(self):
        """${pods:networking:default:protocol} should resolve to protocol."""
        secret_map = {"PROTO": "${pods:networking:default:protocol}"}
        pod = MockPod(networking={"default": {"url": "test.tapis.io", "protocol": "tcp"}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 0
        assert resolved["PROTO"] == "tcp"
    
    def test_resolves_named_network(self):
        """Should resolve non-default network names."""
        secret_map = {"API_URL": "${pods:networking:api:url}"}
        pod = MockPod(networking={
            "default": {"url": "main.tapis.io"},
            "api": {"url": "api.tapis.io", "port": 8000}
        })
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 0
        assert resolved["API_URL"] == "api.tapis.io"
    
    def test_error_on_missing_network(self):
        """Should return error for non-existent network name."""
        secret_map = {"URL": "${pods:networking:nonexistent:url}"}
        pod = MockPod(networking={"default": {"url": "test.tapis.io"}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 1
        assert "nonexistent" in errors[0]
        assert "not found" in errors[0]
    
    def test_error_on_invalid_field(self):
        """Should return error for invalid field name."""
        secret_map = {"X": "${pods:networking:default:invalid_field}"}
        pod = MockPod(networking={"default": {"url": "test.tapis.io"}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 1
        assert "invalid_field" in errors[0]
        assert "Unknown networking field" in errors[0]
    
    def test_error_on_missing_default_url(self):
        """Should return error when ${pods:url} used but no default URL."""
        secret_map = {"URL": "${pods:url}"}
        pod = MockPod(networking={"default": {}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 1
        assert "no default networking URL" in errors[0]
    
    def test_multiple_networking_refs_in_one_value(self):
        """Should resolve multiple networking refs in single value."""
        secret_map = {"CONN": "${pods:networking:default:protocol}://${pods:networking:default:url}:${pods:networking:default:port}"}
        pod = MockPod(networking={"default": {"url": "db.tapis.io", "port": 5432, "protocol": "postgres"}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert len(errors) == 0
        assert resolved["CONN"] == "postgres://db.tapis.io:5432"
    
    def test_passes_through_non_networking_values(self):
        """Non-networking values should pass through unchanged."""
        secret_map = {
            "NET": "${pods:url}",
            "LITERAL": "plain_value",
            "SECRET_REF": "${secret:mysecret}"
        }
        pod = MockPod(networking={"default": {"url": "test.tapis.io"}})
        
        resolved, errors = resolve_pod_networking(secret_map, pod)
        
        assert resolved["NET"] == "test.tapis.io"
        assert resolved["LITERAL"] == "plain_value"
        assert resolved["SECRET_REF"] == "${secret:mysecret}"
    
    def test_handles_none_pod(self):
        """Should handle None pod gracefully."""
        secret_map = {"URL": "${pods:url}"}
        
        resolved, errors = resolve_pod_networking(secret_map, None)
        
        # Should pass through unchanged when no pod
        assert resolved["URL"] == "${pods:url}"
        assert len(errors) == 0


# ============================================================================
# TEST: Integration - Combined Random + Networking
# ============================================================================

class TestCombinedResolution:
    """Tests for combined random password and networking resolution."""
    
    def test_both_random_and_networking(self):
        """Should resolve both random passwords and networking refs."""
        secret_map = {
            "SESSION_SECRET": "${pods:random:32}",
            "CALLBACK_URL": "https://${pods:url}/auth/callback"
        }
        pod = MockPod(
            secret_map=secret_map,
            networking={"default": {"url": "myapp.pods.tapis.io"}}
        )
        
        # Resolve random first
        resolved, rand_errors, _ = resolve_random_passwords(secret_map, pod, "testuser")
        # Then networking
        resolved, net_errors = resolve_pod_networking(resolved, pod)
        
        assert len(rand_errors) == 0
        assert len(net_errors) == 0
        assert len(resolved["SESSION_SECRET"]) == 32
        assert resolved["CALLBACK_URL"] == "https://myapp.pods.tapis.io/auth/callback"


# ============================================================================
# TEST: detect_unresolved_patterns()
# ============================================================================

class TestDetectUnresolvedPatterns:
    """Tests for detect_unresolved_patterns function."""
    
    def test_empty_inputs_no_unresolved(self):
        """Empty inputs should report no unresolved patterns."""
        result = detect_unresolved_patterns()
        
        assert result["has_unresolved"] is False
        assert result["secret_map_unresolved"] == []
        assert result["env_vars_unresolved"] == []
        assert result["config_unresolved"] == []
    
    def test_resolved_values_no_unresolved(self):
        """Fully resolved values should report no unresolved patterns."""
        result = detect_unresolved_patterns(
            secret_map={"DB_PASS": "actualpassword123"},
            environment_variables={"APP_NAME": "myapp"},
            config_contents=["host=localhost\nport=5432"]
        )
        
        assert result["has_unresolved"] is False
    
    def test_detects_secret_map_placeholders(self):
        """Should detect unresolved placeholders in secret_map."""
        result = detect_unresolved_patterns(
            secret_map={
                "API_KEY": "${:?API key required}",
                "DB_HOST": "${pods:default:localhost:?Database host}"
            }
        )
        
        assert result["has_unresolved"] is True
        assert len(result["secret_map_unresolved"]) == 2
        
        # Check types are classified correctly
        api_key_entry = next(e for e in result["secret_map_unresolved"] if e["key"] == "API_KEY")
        assert api_key_entry["patterns"][0]["type"] == "required_placeholder"
        
        db_host_entry = next(e for e in result["secret_map_unresolved"] if e["key"] == "DB_HOST")
        assert db_host_entry["patterns"][0]["type"] == "default_placeholder"
    
    def test_detects_env_var_secret_refs(self):
        """Should detect unresolved ${pods:secrets:KEY} in environment_variables."""
        result = detect_unresolved_patterns(
            environment_variables={
                "DATABASE_URL": "postgres://user:${pods:secrets:DB_PASS}@localhost/db"
            }
        )
        
        assert result["has_unresolved"] is True
        assert len(result["env_vars_unresolved"]) == 1
        assert result["env_vars_unresolved"][0]["patterns"][0]["type"] == "secret_map_reference"
    
    def test_detects_config_unresolved(self):
        """Should detect unresolved patterns in config_content."""
        result = detect_unresolved_patterns(
            config_contents=[
                "api_key=${pods:secrets:API_KEY}\nhost=localhost"
            ]
        )
        
        assert result["has_unresolved"] is True
        assert len(result["config_unresolved"]) == 1
    
    def test_detects_user_secret_patterns(self):
        """Should detect unresolved user secret references."""
        result = detect_unresolved_patterns(
            secret_map={"PASSWORD": "${secret:jsmith:mydbsecret}"}
        )
        
        assert result["has_unresolved"] is True
        assert result["secret_map_unresolved"][0]["patterns"][0]["type"] == "user_secret"
    
    def test_detects_networking_patterns(self):
        """Should detect unresolved networking references."""
        result = detect_unresolved_patterns(
            secret_map={
                "URL": "${pods:url}",
                "TAPIS": "${pods:tapis_url}",
                "PORT": "${pods:networking:default:port}"
            }
        )
        
        assert result["has_unresolved"] is True
        assert len(result["secret_map_unresolved"]) == 3
        
        types = [entry["patterns"][0]["type"] for entry in result["secret_map_unresolved"]]
        assert "pod_url" in types
        assert "tapis_url" in types
        assert "networking_reference" in types
    
    def test_detects_random_password_pattern(self):
        """Should detect unresolved random password patterns."""
        result = detect_unresolved_patterns(
            secret_map={"SESSION_KEY": "${pods:random:32}"}
        )
        
        assert result["has_unresolved"] is True
        assert result["secret_map_unresolved"][0]["patterns"][0]["type"] == "random_password"
    
    def test_summary_message(self):
        """Should provide human-readable summary."""
        result = detect_unresolved_patterns(
            secret_map={"KEY1": "${:?required}"},
            environment_variables={"VAR1": "${pods:secrets:MISSING}"},
            config_contents=["value=${pods:secrets:ALSO_MISSING}"]
        )
        
        assert result["has_unresolved"] is True
        assert "secret_map keys" in result["summary"]
        assert "environment_variables" in result["summary"]
        assert "config_content" in result["summary"]
    
    def test_multiple_patterns_in_one_value(self):
        """Should detect multiple patterns in a single value."""
        result = detect_unresolved_patterns(
            environment_variables={
                "CONN_STR": "host=${pods:secrets:HOST};pass=${pods:secrets:PASS}"
            }
        )
        
        assert result["has_unresolved"] is True
        assert len(result["env_vars_unresolved"]) == 1
        assert len(result["env_vars_unresolved"][0]["patterns"]) == 2


# ============================================================================
# TEST: check_pod_unresolved_patterns()
# ============================================================================

class TestCheckPodUnresolvedPatterns:
    """Tests for check_pod_unresolved_patterns helper function."""
    
    def test_returns_none_when_all_resolved(self):
        """Should return None when no unresolved patterns."""
        result = check_pod_unresolved_patterns(
            secret_map={"DB_PASS": "actualpassword"},
            environment_variables={"APP_NAME": "myapp"},
            volume_mounts={}
        )
        
        assert result is None
    
    def test_returns_dict_when_unresolved(self):
        """Should return unresolved dict when patterns found."""
        result = check_pod_unresolved_patterns(
            secret_map={"API_KEY": "${:?required}"},
            environment_variables={},
            volume_mounts={}
        )
        
        assert result is not None
        assert result["has_unresolved"] is True
    
    def test_extracts_config_from_volume_mounts_dict(self):
        """Should extract config_content from dict-style volume_mounts."""
        result = check_pod_unresolved_patterns(
            secret_map={},
            environment_variables={},
            volume_mounts={
                "/etc/config": {
                    "type": "ephemeral",
                    "config_content": "password=${pods:secrets:MISSING}"
                }
            }
        )
        
        assert result is not None
        assert len(result["config_unresolved"]) == 1
    
    def test_handles_none_volume_mount_entries(self):
        """Should handle None entries in volume_mounts (removed mounts)."""
        result = check_pod_unresolved_patterns(
            secret_map={"RESOLVED": "value"},
            environment_variables={},
            volume_mounts={
                "/removed": None,
                "/valid": {"type": "ephemeral", "config_content": "clean=true"}
            }
        )
        
        assert result is None
    
    def test_handles_all_none_inputs(self):
        """Should handle all None inputs gracefully."""
        result = check_pod_unresolved_patterns(
            secret_map=None,
            environment_variables=None,
            volume_mounts=None
        )
        
        assert result is None