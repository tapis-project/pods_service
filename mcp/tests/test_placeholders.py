"""Unit tests for the placeholder parser (dependency-free, no network)."""
import placeholders as ph


def test_required_placeholder():
    assert ph.parse_placeholder("${:?API key}") == {
        "required": True, "default": None, "description": "API key"}


def test_default_placeholder_with_description():
    d = ph.parse_placeholder("${pods:default:redis:?Redis host}")
    assert d == {"required": False, "default": "redis", "description": "Redis host"}


def test_default_placeholder_no_description():
    assert ph.parse_placeholder("${pods:default:6379}") == {
        "required": False, "default": "6379", "description": None}


def test_non_blanks_return_none():
    for v in ("${pods:random:32}", "${stack:secrets:X}", "${secret:me}",
              "${pods:url}", "literal", "", 123, None):
        assert ph.parse_placeholder(v) is None


def test_extract_stack_blanks():
    sd = {
        "secret_map": {"A": "${:?a}", "P": "${pods:random:32}", "D": "${pods:default:x:?d}"},
        "members": [
            {"name": "db", "volume_mounts": {"/v": {"type": "tapisvolume",
                                                    "source_id": "${:?vol}"}}},
            {"name": "app", "secret_map": {"R": "${stack:secrets:A}"}},
        ],
    }
    b = ph.extract_stack_blanks(sd)
    assert [x["key"] for x in b["secrets"] if x["required"]] == ["A"]
    assert [x["key"] for x in b["secrets"] if not x["required"]] == ["D"]
    # db volume source_id is a required member override
    assert any(m["member"] == "db" and m["path"][-1] == "source_id"
               for m in b["member_overrides"])
    # ${stack:secrets:A} on the app member is a reference, NOT a blank
    assert not any(m.get("key") == "R" for m in b["member_overrides"])


def test_extract_pod_blanks():
    pd = {"secret_map": {"K": "${:?k}", "auto": "${pods:random:16}"},
          "volume_mounts": {"/d": {"type": "tapisvolume", "source_id": "${:?vol}"}}}
    b = ph.extract_pod_blanks(pd)
    assert [x["key"] for x in b["secrets"]] == ["K"]
    assert b["volumes"] and b["volumes"][0]["field"] == "source_id"


def test_required_keys_helper():
    b = ph.extract_stack_blanks({"secret_map": {"A": "${:?a}", "B": "${pods:default:1}"}})
    assert ph.required_keys(b) == ["A"]
