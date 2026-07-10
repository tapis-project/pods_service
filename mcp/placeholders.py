"""
Placeholder extraction for template tags — the "fill only the blanks" core.

This is a dependency-free port of the placeholder *grammar* from
``service/secret_utils.py`` (patterns at lines ~78-92). It intentionally imports
nothing from the pods service: the MCP is a standalone client, so it re-declares
the small subset of regexes it needs rather than pulling in ``tapisservice``, SK,
or the DB layer.

What it does: given a template tag's ``stack_definition`` or ``pod_definition``,
enumerate the placeholders a user must (or may) fill, so a generic
``deploy_from_template`` tool can present *only those blanks* instead of asking an
LLM to author a full nested pod/stack body.

Placeholder grammar (subset we care about for "what must the user provide?"):
    ${:?description}                    -> required, no default
    ${pods:default:value}               -> optional, default = value
    ${pods:default:value:?description}  -> optional, default = value, described
These appear as full-value entries in ``secret_map`` and volume-mount
``source_id`` fields, and inline inside literal strings.
"""
import re
from typing import Any, Dict, List, Optional

# --- ported regexes (keep in sync with service/secret_utils.py) --------------

# Full-value required placeholder: ${:?description}
_REQUIRED = re.compile(r'^\$\{:\?([^}]+)\}$')

# Full-value default placeholder: ${pods:default:value} or ${pods:default:value:?description}
# group(1) = default value (may be empty), group(2) = optional ":?description" tail
_DEFAULT = re.compile(r'^\$\{pods:default:([^:}]*)(?::([^}]+))?\}$')

# Inline placeholders inside a literal string (either required or default form)
_INLINE = re.compile(r'\$\{(pods:default:[^:}]*(?::\?[^}]+)?|:\?[^}]+)\}')


def parse_placeholder(value: Any) -> Optional[Dict[str, Any]]:
    """
    Classify a single value. Returns a blank descriptor if the whole value is a
    placeholder, else None (literals, secret refs, ${stack:...}, ${pods:random}
    are not user-facing blanks and return None).

    Descriptor: {"required": bool, "default": str|None, "description": str|None}
    """
    if not isinstance(value, str):
        return None

    m = _REQUIRED.match(value)
    if m:
        return {"required": True, "default": None, "description": m.group(1)}

    m = _DEFAULT.match(value)
    if m:
        default_val = m.group(1)
        desc_tail = m.group(2)  # e.g. "?some description" or None
        description = None
        if desc_tail and desc_tail.startswith('?'):
            description = desc_tail[1:]
        # pods:default always carries a default => never required.
        return {"required": False, "default": default_val, "description": description}

    return None


def _inline_blanks(value: Any) -> List[Dict[str, Any]]:
    """Extract placeholders embedded inside a literal string (e.g. a URL)."""
    if not isinstance(value, str):
        return []
    out: List[Dict[str, Any]] = []
    for m in _INLINE.finditer(value):
        content = m.group(1)
        if content.startswith(':?'):
            out.append({"required": True, "default": None,
                        "description": content[2:], "match": m.group(0)})
        elif content.startswith('pods:default:'):
            rest = content[len('pods:default:'):]
            parts = rest.split(':?', 1)
            out.append({"required": False, "default": parts[0],
                        "description": parts[1] if len(parts) == 2 else None,
                        "match": m.group(0)})
    return out


def _blanks_from_secret_map(secret_map: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every key in a secret_map whose value is (or contains) a user blank."""
    blanks: List[Dict[str, Any]] = []
    for key, value in (secret_map or {}).items():
        ph = parse_placeholder(value)
        if ph:
            blanks.append({"key": key, **ph})
            continue
        for inline in _inline_blanks(value):
            blanks.append({"key": key, "inline": inline.pop("match"), **inline})
    return blanks


def _volume_source_blanks(member_or_pod: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Volume mounts whose ``source_id`` is a required placeholder, e.g. a
    tapisvolume the deployer must supply. Templates require the placeholder form
    for tapisvolume/tapissnapshot sources.
    """
    blanks: List[Dict[str, Any]] = []
    vmounts = member_or_pod.get("volume_mounts") or {}
    for mount_path, mount in vmounts.items():
        if not isinstance(mount, dict):
            continue
        ph = parse_placeholder(mount.get("source_id"))
        if ph:
            blanks.append({"mount_path": mount_path, "field": "source_id", **ph})
    return blanks


def extract_pod_blanks(pod_definition: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Blanks for a kind='pod' tag. ``secrets`` are the pod-level secret_map
    placeholders; ``volumes`` are volume source_ids that must be supplied.
    """
    pod = pod_definition or {}
    return {
        "secrets": _blanks_from_secret_map(pod.get("secret_map")),
        "volumes": _volume_source_blanks(pod),
    }


def extract_stack_blanks(stack_definition: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Blanks for a kind='stack' tag.

    - ``secrets``: stack-level secret_map placeholders. These are filled via the
      from-template request's ``secrets`` dict (the single source that member
      ``${stack:secrets:KEY}`` refs resolve against).
    - ``member_overrides``: per-member blanks — a member's own secret_map
      placeholders or a member volume ``source_id`` placeholder — which are
      supplied via the from-template ``overrides`` dict, keyed by member name.
    """
    stack = stack_definition or {}
    secrets = _blanks_from_secret_map(stack.get("secret_map"))

    member_overrides: List[Dict[str, Any]] = []
    for member in stack.get("members") or []:
        if not isinstance(member, dict):
            continue
        name = member.get("name", "?")
        for b in _blanks_from_secret_map(member.get("secret_map")):
            # Skip pure ${stack:secrets:KEY} references — those aren't blanks,
            # they resolve from the stack secrets above. _blanks_from_secret_map
            # already ignores them (not a placeholder form).
            member_overrides.append({"member": name, "path": ["secret_map", b["key"]], **b})
        for b in _volume_source_blanks(member):
            member_overrides.append({"member": name,
                                     "path": ["volume_mounts", b["mount_path"], "source_id"],
                                     **b})
    return {"secrets": secrets, "member_overrides": member_overrides}


def required_keys(blanks: Dict[str, Any]) -> List[str]:
    """Flat list of required stack-secret keys — the must-fill set for a quick check."""
    return [b["key"] for b in blanks.get("secrets", []) if b.get("required")]
