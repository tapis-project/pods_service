"""
Unit tests for the write-time secret-ownership gate (secret_utils.validate_secret_map_ownership).

Guards the cross-user secret-theft vector on the stack path: a stack's shared secret_map is
later resolved with actor=None (owner-in-notation trusted blindly), so caller-supplied values
must reject ${secret:other_user:id} at write time. Pods get this implicitly via
resolve_secret_map(actor=...); stacks call this helper directly.
"""
import sys

sys.path.append('/home/tapis/service')

from secret_utils import validate_secret_map_ownership, expand_short_secret_references


def test_own_explicit_ref_is_allowed():
    m = {"DB": "${secret:alice:dbpass}"}
    assert validate_secret_map_ownership(m, "alice") == []


def test_cross_user_explicit_ref_is_rejected():
    m = {"DB": "${secret:victim:dbpass}"}
    errs = validate_secret_map_ownership(m, "attacker")
    assert len(errs) == 1
    assert "victim" in errs[0] and "DB" in errs[0]


def test_inline_cross_user_ref_is_caught():
    # ref embedded in surrounding text (anchored parser would miss it)
    m = {"URL": "postgres://u:${secret:victim:pw}@host/db"}
    errs = validate_secret_map_ownership(m, "attacker")
    assert len(errs) == 1
    assert "victim" in errs[0]


def test_expanded_short_ref_passes_the_gate():
    # short refs are pinned to the caller first, then must pass ownership
    expanded = expand_short_secret_references({"K": "${secret:mypass}"}, "alice")
    assert expanded == {"K": "${secret:alice:mypass}"}
    assert validate_secret_map_ownership(expanded, "alice") == []


def test_literal_values_are_ignored():
    assert validate_secret_map_ownership({"K": "just-a-literal"}, "alice") == []


def test_non_string_values_are_ignored():
    assert validate_secret_map_ownership({"K": 123, "J": None}, "alice") == []


def test_empty_map():
    assert validate_secret_map_ownership({}, "alice") == []
    assert validate_secret_map_ownership(None, "alice") == []


def test_multiple_keys_each_reported():
    m = {"A": "${secret:v1:s}", "B": "${secret:alice:s}", "C": "${secret:v2:s}"}
    errs = validate_secret_map_ownership(m, "alice")
    assert len(errs) == 2  # A and C, not B
