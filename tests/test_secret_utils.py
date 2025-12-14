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
    SecretReference,
    SECRET_SHORT_PATTERN,
    SECRET_EXPLICIT_PATTERN,
    PLACEHOLDER_DEFAULT_PATTERN,
    PLACEHOLDER_REQUIRED_PATTERN,
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
        """Username should be alphanumeric only."""
        ref, error = parse_secret_reference("${secret:user123:secret}", actor="test")
        
        assert error is None
        assert ref.secret_owner == "user123"
    
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
# TEST: parse_secret_reference() - Default placeholder ${default:value:?desc}
# ============================================================================

class TestParseSecretReferenceDefaultPlaceholder:
    """Tests for parsing ${default:value:?description} placeholders."""
    
    def test_default_placeholder_basic(self):
        """${default:redis:?Redis hostname} should parse with default value."""
        ref, error = parse_secret_reference("${default:redis:?Redis hostname}")
        
        assert error is None
        assert ref.is_placeholder is True
        assert ref.is_required is False  # Has default, so not required
        assert ref.default_value == "redis"
        assert ref.description == "Redis hostname"
    
    def test_default_placeholder_empty_default(self):
        """${default::?description} with empty default should be marked required."""
        ref, error = parse_secret_reference("${default::?Optional cache host}")
        
        assert error is None
        assert ref.is_placeholder is True
        # Empty default value means it's effectively required
        assert ref.default_value is None
        assert ref.is_required is True
    
    def test_default_placeholder_numeric_value(self):
        """Default value can be numeric."""
        ref, error = parse_secret_reference("${default:5432:?PostgreSQL port}")
        
        assert error is None
        assert ref.default_value == "5432"
        assert ref.description == "PostgreSQL port"
    
    def test_default_placeholder_missing_question_mark(self):
        """Description without ? prefix should return an error."""
        ref, error = parse_secret_reference("${default:value:missing question mark}")
        
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
        """Literal with inline ${default:val:?desc} should capture it."""
        ref, error = parse_secret_reference("Connect to ${default:localhost:?hostname}:5432")
        
        assert error is None
        assert ref.is_literal is True
        assert len(ref.inline_placeholders) == 1
        assert ref.inline_placeholders[0]['type'] == 'default'
        assert ref.inline_placeholders[0]['default_value'] == 'localhost'
        assert ref.inline_placeholders[0]['description'] == 'hostname'
    
    def test_literal_with_multiple_placeholders(self):
        """Multiple inline placeholders should all be captured."""
        ref, error = parse_secret_reference("${:?user}:${:?password}@${default:localhost:?host}")
        
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
            "CACHE_HOST": "${default:redis:?Redis hostname}"
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
            "DB_HOST": "${default:localhost:?Database host}",
            "CACHE_HOST": "${default:redis:?Cache host}"
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
        """${default:value:?desc} placeholder should be valid in template."""
        secret_map = {
            "CACHE_HOST": "${default:redis:?Redis hostname}"
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
            "VALID_DEFAULT": "${default:myval:?Optional value}",
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
            "HOST": "${default:localhost:?Server host}",
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
            "HOST": "${default:localhost:?Server host}",
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
    
    def test_explicit_pattern_invalid(self):
        """EXPLICIT pattern should NOT match short form."""
        assert SECRET_EXPLICIT_PATTERN.match("${secret:mysecret}") is None
        # Username with underscore should not match (alphanumeric only)
        assert SECRET_EXPLICIT_PATTERN.match("${secret:user_name:secret}") is None
    
    def test_required_pattern_valid(self):
        """REQUIRED pattern should match ${:?description}."""
        assert PLACEHOLDER_REQUIRED_PATTERN.match("${:?Required field}")
        assert PLACEHOLDER_REQUIRED_PATTERN.match("${:?API key for service}")
    
    def test_required_pattern_invalid(self):
        """REQUIRED pattern should NOT match default form."""
        assert PLACEHOLDER_REQUIRED_PATTERN.match("${default:val:?desc}") is None
        assert PLACEHOLDER_REQUIRED_PATTERN.match("${:missing question mark}") is None
    
    def test_default_pattern_valid(self):
        """DEFAULT pattern should match ${default:value:?description}."""
        assert PLACEHOLDER_DEFAULT_PATTERN.match("${default:redis:?Redis host}")
        assert PLACEHOLDER_DEFAULT_PATTERN.match("${default::?Empty default}")
        assert PLACEHOLDER_DEFAULT_PATTERN.match("${default:5432:?Port number}")
    
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
