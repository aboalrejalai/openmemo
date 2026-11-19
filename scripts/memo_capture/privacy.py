"""What never reaches memory: private blocks, injected context, and secrets.

- strip_tags(): removes balanced <private>, <memo-context>, <system-reminder>,
  <system-instruction>/<system_instruction>, <persisted-output> blocks in one
  linear pass. An unclosed tag is kept (removing to end of text would eat
  real content).
- redact(): replaces recognised secrets with <redacted type='name'/>. On by
  default. Patterns: the claude-mem 15.3.0 built-ins, Supabase keys, and the
  generic password/token/secret/Bearer patterns.
- clean_text()/clean_json(): strip + redact + base64-image removal +
  head/tail truncation, used on every prompt, tool event and reply.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

TAG_NAMES = ("private", "memo-context", "system-reminder", "system-instruction",
             "system_instruction", "persisted-output")
_TAG_RE = re.compile(r"<(/?)(" + "|".join(re.escape(t) for t in TAG_NAMES) + r")(?=[\s>/])[^<>]*>")
MAX_TAG_COUNT = 100

MAX_REDACT_CHARS = 1024 * 1024
OVERSIZE_MARKER = "<redacted type='oversize'/>"
FIELD_LIMIT = 16_000
HEAD_SHARE, TAIL_SHARE = 0.6, 0.3

REDACTION_HINT = ("If you see a \"<redacted type='...'/>\" marker, that field was a recognised "
                  "secret and was removed before storage. Treat it as a placeholder; never guess "
                  "the value or copy the marker into memory.")

# ------------------------------------------------------------------ patterns

_VALUE_CLASS = r"[A-Za-z0-9_.\-+/=~@#$%^*!?]"
_GENERIC_VALUE = rf"(?={_VALUE_CLASS}{{8}}){_VALUE_CLASS}*[A-Za-z]{_VALUE_CLASS}*"
_QUOTE = r"(?:\\{0,3}[\"'])?"
_SPACE = r"\s{0,8}"


def _keyed(key: str, value: str) -> str:
    """`key = value` with the key part captured, so only the value is replaced."""
    return rf"({key}{_QUOTE}{_SPACE}[=:]{_SPACE}{_QUOTE})({value})"


# (name, regex, keyed): keyed patterns keep group 1 and replace group 2.
BUILTIN: List[Tuple[str, str, bool]] = [
    ("aws_access_key", r"(?:AKIA|ASIA)[0-9A-Z]{16}", False),
    ("aws_secret_key", _keyed(r"(?:aws_secret_access_key|\bSecretAccessKey)", r"[A-Za-z0-9/+=]{40}"), True),
    ("aws_session_token", _keyed(r"\b(?:aws_session_token|SessionToken)", r"[A-Za-z0-9/+=]{100,}"), True),
    ("github_pat", r"\bgh[oprsu]_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{82}\b", False),
    ("openai_key", r"\bsk-(?!ant-)[A-Za-z0-9_-]{20,}(?!\w)", False),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{20,}(?!\w)", False),
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}(?!\w)", False),
    ("jwt", r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?!\w)", False),
    ("private_key_pem", r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"
                        r"[\s\S]*?-----END (?:RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----", False),
    ("stripe_key", r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{24,}\b", False),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}(?!\w)", False),
    ("supabase_key", r"\bsbp_[A-Za-z0-9]{40}\b|\bsb_secret_[A-Za-z0-9_-]{20,}(?!\w)", False),
    ("claude_oauth_token", r"\bsk-ant-oat[0-9]{2}-[A-Za-z0-9_-]{20,}(?!\w)", False),
]

GENERIC: List[Tuple[str, str, bool]] = [
    ("generic_password", _keyed(r"\b(?:\w*_?(?:password|passwd)|\w+_pwd)", _GENERIC_VALUE), True),
    ("generic_token", _keyed(r"\b\w*_(?:token|secret|api_key)", _GENERIC_VALUE), True),
    ("bearer_token", rf"(\bAuthorization{_QUOTE}{_SPACE}:{_SPACE}{_QUOTE}Bearer{_SPACE})({_GENERIC_VALUE})", True),
]

_GENERIC_KEY = re.compile(r"(?:^|_)(password|passwd|secret|token|api_key)$", re.IGNORECASE)
_WHOLE_GENERIC = re.compile(rf"^{_VALUE_CLASS}{{8,}}$")

_compiled: Optional[List[Tuple[str, "re.Pattern[str]", bool]]] = None
_compiled_key: Optional[str] = None


def _config() -> Dict[str, Any]:
    try:
        from . import settings
        s = settings.load()
        return {"enabled": bool(s["redact_enabled"]), "generic": bool(s["redact_generic_enabled"]),
                "disabled": set(settings.csv("redact_disabled_builtins")),
                "custom": s.get("redact_custom_patterns") or []}
    except Exception:
        return {"enabled": True, "generic": True, "disabled": set(), "custom": []}


def _patterns(cfg: Dict[str, Any]):
    global _compiled, _compiled_key
    key = json.dumps([cfg["generic"], sorted(cfg["disabled"]), cfg["custom"]], default=str)
    if _compiled is not None and _compiled_key == key:
        return _compiled
    out = []
    # Custom patterns run first, so they win where they overlap a built-in.
    for item in cfg["custom"]:
        try:
            name = str(item["name"]).strip()
            if name:
                out.append((name, re.compile(str(item["pattern"])), False))
        except (KeyError, TypeError, re.error):
            continue
    for name, pattern, keyed in BUILTIN + (GENERIC if cfg["generic"] else []):
        if name not in cfg["disabled"]:
            out.append((name, re.compile(pattern, re.IGNORECASE if keyed else 0), keyed))
    _compiled, _compiled_key = out, key
    return out


def redact(text: str, *, force: bool = False) -> str:
    if not isinstance(text, str) or not text:
        return text
    cfg = _config()
    if not (cfg["enabled"] or force):
        return text
    if len(text) > MAX_REDACT_CHARS:
        return OVERSIZE_MARKER
    for name, regex, keyed in _patterns(cfg):
        marker = f"<redacted type='{name}'/>"
        text = regex.sub((lambda m, mk=marker: m.group(1) + mk) if keyed else marker, text)
    return text


def redact_for_log(text: str) -> str:
    """Logs are always redacted, whatever the setting says."""
    return redact(str(text)[:20_000], force=True)


def _keyed_value_marker(key: str, value: str) -> Optional[str]:
    if not _config()["generic"]:
        return None
    match = _GENERIC_KEY.search(key)
    if not match or not _WHOLE_GENERIC.match(value) or not re.search(r"[A-Za-z]", value):
        return None
    return "<redacted type='generic_password'/>" if match.group(1).lower().startswith("passw") \
        else "<redacted type='generic_token'/>"


def redact_json(value: Any, _key: str = "", *, force: bool = False) -> Any:
    """Redact every string inside a JSON value; a credential-named key hides its whole value."""
    if isinstance(value, str):
        marker = _keyed_value_marker(_key, value) if _key else None
        return marker or redact(value, force=force)
    if isinstance(value, list):
        return [redact_json(v, force=force) for v in value]
    if isinstance(value, dict):
        return {k: redact_json(v, str(k), force=force) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- tag strip

def strip_tags(text: str) -> str:
    """Remove balanced memory/private tag blocks in one pass; keep unclosed tags."""
    if not isinstance(text, str) or "<" not in text:
        return text
    opens: Dict[str, List[int]] = {}
    spans: List[Tuple[int, int]] = []
    count = 0
    for m in _TAG_RE.finditer(text):
        name, closing = m.group(2), m.group(1) == "/"
        if not closing:
            if m.group(0).endswith("/>"):
                continue
            opens.setdefault(name, []).append(m.start())
            count += 1
            if count > MAX_TAG_COUNT:
                break
        elif opens.get(name):
            start = opens[name].pop()
            spans.append((start, m.end()))
    if not spans:
        return text
    spans.sort()
    merged: List[Tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    out, pos = [], 0
    for start, end in merged:
        out.append(text[pos:start])
        pos = end
    out.append(text[pos:])
    return "".join(out)


_TASK_NOTIFICATION = re.compile(r"^\s*<task-notification>[\s\S]*</task-notification>\s*$")


def is_task_notification_only(prompt: str) -> bool:
    return bool(prompt) and bool(_TASK_NOTIFICATION.match(prompt))


def clean_prompt(prompt: Optional[str]) -> Tuple[str, bool]:
    """(cleaned prompt, is_private). A prompt with nothing left is private."""
    raw = prompt or ""
    cleaned = redact(strip_tags(raw)).strip()
    return cleaned, bool(raw.strip()) and not cleaned


# ------------------------------------------------------------ size control

_DATA_URI = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{64,}")


def _strip_images(value: Any) -> Any:
    if isinstance(value, dict):
        source = value.get("source")
        if value.get("type") == "image" and isinstance(source, dict) and source.get("type") == "base64":
            return {"type": "image", "note": "[image removed]"}
        return {k: _strip_images(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_images(v) for v in value]
    if isinstance(value, str):
        return _DATA_URI.sub("[image removed]", value)
    return value


def truncate(text: str, limit: int = FIELD_LIMIT) -> str:
    if not isinstance(text, str) or len(text) <= limit:
        return text
    head, tail = int(limit * HEAD_SHARE), int(limit * TAIL_SHARE)
    elided = len(text) - head - tail
    return f"{text[:head]}\n<elided chars=\"{elided}\"/>\n{text[-tail:]}"


def clean_text(text: Optional[str], limit: int = FIELD_LIMIT) -> str:
    if not text:
        return ""
    return truncate(redact(_strip_images(strip_tags(text))), limit)


def clean_json(value: Any, limit: int = FIELD_LIMIT) -> str:
    """A tool input/response as cleaned, size-capped JSON text."""
    value = _strip_images(value)
    if isinstance(value, str):
        value = strip_tags(value)
    value = redact_json(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return truncate(strip_tags(text), limit)
