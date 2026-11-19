"""Remote Memo MCP surface policy (plan 004 D7).

Installed only when MEMO_REMOTE is set. mcp_server imports this module; this
module never imports mcp_server.

Default profile: all read tools + memo_save_memory. Hidden paths and observation
types are filtered before results leave the process.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

# Set by mcp_server.main when remote background sync starts.
SYNC_WAKE = None

# Fixed hide-list for sensitive paths that must not leave a remote vault.
# Dot-folder copies are already refused by the path rule.
REMOTE_HIDDEN: frozenset[str] = frozenset({
    "SECOND_BRAIN.md",
})

REMOTE_HIDDEN_TYPES: frozenset[str] = frozenset({
    "sensitive",
    "security_alert",
})

# Read tools currently shipped in mcp_server + save. memo_project_brief lands in
# TASK-003 and should be added here when that tool exists. memo_lint stays local.
REMOTE_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "memo_search",
    "memo_get_entity",
    "memo_get_timeline",
    "memo_get_dossier",
    "memo_list_files",
    "memo_list_projects",
    "memo_status",
    "memo_query_props",
    "memo_session_timeline",
    "memo_get_observations",
    "memo_save_memory",
})

# VPS allow-list when MEMO_REMOTE_WRITERS=curated (plan 003 D8 / 004 D7).
CURATED_WRITE_PREFIXES: tuple[str, ...] = (
    "sessions/",
    "timeline.md",
    "decisions.md",
    "projects/",
    "clients/",
    "stack/",
    "subscriptions/",
    "repo-library/",
    "life/",
    "hackathons/",
    "graph/entities.json",
    "agent/learned-heuristics.md",
)

_before: Optional[Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]] = None
_after: Optional[Callable[[str, Dict[str, Any], Optional[Dict[str, Any]]], Optional[Dict[str, Any]]]] = None
_installed = False


def remote_enabled() -> bool:
    return (os.environ.get("MEMO_REMOTE") or "").strip() in {"1", "true", "TRUE", "yes", "YES"}


def writers_mode() -> str:
    return (os.environ.get("MEMO_REMOTE_WRITERS") or "save").strip().lower() or "save"


def normalize_rel(path: str) -> str:
    rel = (path or "").replace("\\", "/").lstrip("/")
    while "//" in rel:
        rel = rel.replace("//", "/")
    if rel.endswith(".md"):
        pass
    return rel


def is_hidden_rel(rel: str, *, all_dirs: bool = False) -> bool:
    """True if any path segment is in SKIP_DIRS or starts with '.' .

    all_dirs=True treats every segment as a directory (for memo_list_files.subdir).
    """
    import memo_core as core

    rel = normalize_rel(rel)
    if not rel:
        return False
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return False
    check = parts if all_dirs else parts[:-1]
    # For a bare file at root, also refuse if the basename itself is a skip dir name.
    if not check and parts[0] in core.SKIP_DIRS:
        return True
    for seg in check:
        if seg in core.SKIP_DIRS or seg.startswith("."):
            return True
    return False


def is_remote_hidden_path(rel: str) -> bool:
    rel = normalize_rel(rel)
    if not rel:
        return False
    if rel in REMOTE_HIDDEN or rel.lower() in {h.lower() for h in REMOTE_HIDDEN}:
        return True
    base = rel.rsplit("/", 1)[-1]
    return base in REMOTE_HIDDEN or base.lower() in {h.lower() for h in REMOTE_HIDDEN}


def path_refused_reason(rel: str, *, all_dirs: bool = False) -> Optional[str]:
    rel = normalize_rel(rel)
    if is_hidden_rel(rel, all_dirs=all_dirs):
        return f"Refused: path '{rel}' is outside the remote-readable vault surface."
    if is_remote_hidden_path(rel):
        return f"Refused: path '{rel}' is hidden in remote mode."
    return None


def filter_path_hits(items: Sequence[Dict[str, Any]], path_key: str = "path") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        p = it.get(path_key) or it.get("file") or it.get("markdown") or ""
        if isinstance(p, str) and (is_remote_hidden_path(p) or is_hidden_rel(p)):
            continue
        out.append(it)
    return out


def observation_type(item: Dict[str, Any]) -> str:
    return str(item.get("type") or item.get("obs_type") or "").strip().lower()


def filter_observation_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("kind") == "session":
            # Drop session summaries that only carry hidden-type content later;
            # keep the session shell but strip markdown lines of hidden types.
            cleaned = dict(it)
            if cleaned.get("markdown"):
                pass
            out.append(cleaned)
            continue
        if observation_type(it) in REMOTE_HIDDEN_TYPES:
            continue
        out.append(it)
    return out


def strip_hidden_type_lines(text: str) -> str:
    """Remove session-note observation lines whose type is REMOTE_HIDDEN_TYPES.

    Matches writer._obs_line style: `- obs-<id> · <emoji> <type> · <title> ...`
    Also matches a simpler `- **type:**` / `· type ·` forms used in notes.
    """
    if not text:
        return text
    kept: List[str] = []
    for line in text.splitlines():
        drop = False
        lower = line.lower()
        for t in REMOTE_HIDDEN_TYPES:
            # common shapes: "· sensitive ·" / " sensitive ·" / "**sensitive:**"
            if f"· {t} ·" in lower or f" {t} ·" in lower or f"**{t}:**" in lower:
                drop = True
                break
            # "· ⚠ sensitive ·" style with emoji before type
            if f" {t} ·" in f" {lower} ":
                drop = True
                break
        if not drop:
            kept.append(line)
    return "\n".join(kept)


def curated_target_allowed(rel: str) -> bool:
    import memo_core as core

    rel = normalize_rel(rel)
    base = rel.rsplit("/", 1)[-1].lower()
    if base in core.INSTRUCTION_FILE_NAMES or base[:-3] in {n[:-3] for n in core.INSTRUCTION_FILE_NAMES if n.endswith(".md")}:
        return False
    if rel in CURATED_WRITE_PREFIXES:
        return True
    for prefix in CURATED_WRITE_PREFIXES:
        if prefix.endswith("/"):
            if rel.startswith(prefix):
                return True
        elif rel == prefix:
            return True
    return False


def _audit(method: str, tool: str = "-", source: str = "-") -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(f"memo-audit {now} {method} {tool} source={source}\n")
    sys.stderr.flush()


def _tool_error_result(msg_id: Any, text: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": True,
        },
    }


def _method_not_found(msg_id: Any, method: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not supported: '{method}'"},
    }


def before(method: str, params: Dict[str, Any], *, msg_id: Any = None) -> Optional[Dict[str, Any]]:
    """Pre-dispatch hook. Returns a full JSON-RPC response to short-circuit, or None."""
    if method == "initialize":
        return None  # patched in after()

    if method.startswith("resources/") or method.startswith("prompts/"):
        _audit(method)
        return _method_not_found(msg_id, method)

    if method == "tools/list":
        _audit(method)
        return None

    if method == "tools/call":
        tool = (params.get("name") or "").strip()
        args = params.get("arguments") or {}
        source = str(args.get("source") or "-")
        _audit(method, tool or "-", source)
        if tool not in REMOTE_ALLOWED_TOOLS:
            return _tool_error_result(
                msg_id,
                f"Unknown tool '{tool}'. Available: " + ", ".join(sorted(REMOTE_ALLOWED_TOOLS)),
            )
        if tool == "memo_get_dossier":
            reason = path_refused_reason(str(args.get("path") or ""), all_dirs=False)
            if reason:
                return _tool_error_result(msg_id, reason)
        if tool == "memo_list_files":
            sub = str(args.get("subdir") or "")
            if sub:
                reason = path_refused_reason(sub, all_dirs=True)
                if reason:
                    return _tool_error_result(msg_id, reason)
        if writers_mode() == "curated" and tool in {
            "memo_append_fact", "memo_append_section", "memo_create_note",
            "memo_update_fact", "memo_add_entity",
        }:
            # curated writers are not in REMOTE_ALLOWED_TOOLS by default; this
            # branch is for a future expanded allow-list.
            target = str(args.get("path") or "")
            if tool == "memo_append_fact":
                target = "timeline.md" if (args.get("target") or "timeline") == "timeline" else "sessions/current.md"
            if tool == "memo_add_entity":
                target = "graph/entities.json"
            if target and not curated_target_allowed(target):
                return _tool_error_result(msg_id, f"Refused: write target '{target}' is outside the VPS allow-list.")
        return None

    _audit(method)
    return None


def _redact_payload(value: Any) -> Any:
    try:
        from memo_capture import privacy
        if hasattr(privacy, "redact_json"):
            # force=True when supported
            try:
                return privacy.redact_json(value, force=True)  # type: ignore[call-arg]
            except TypeError:
                return privacy.redact_json(value)
    except Exception:
        return value
    return value


def after(method: str, params: Dict[str, Any], response: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Post-dispatch hook. Mutates/filters the JSON-RPC response."""
    if response is None:
        return None

    if method == "initialize" and "result" in response:
        result = response["result"]
        caps = dict(result.get("capabilities") or {})
        # CUJs need neither resources nor prompts remotely.
        caps.pop("resources", None)
        caps.pop("prompts", None)
        result["capabilities"] = caps
        if "instructions" in result and isinstance(result["instructions"], str):
            try:
                from memo_capture import privacy
                result["instructions"] = privacy.redact(result["instructions"], force=True)
            except Exception:
                pass
        return response

    if method == "tools/list" and "result" in response:
        tools = response["result"].get("tools") or []
        response["result"]["tools"] = [
            t for t in tools if isinstance(t, dict) and t.get("name") in REMOTE_ALLOWED_TOOLS
        ]
        return response

    if method != "tools/call" or "result" not in response:
        return response

    tool = (params.get("name") or "").strip()
    result = response["result"]
    if result.get("isError"):
        return response

    structured = result.get("structuredContent")
    text_parts = result.get("content") or []

    if tool in {"memo_search", "memo_list_files", "memo_query_props"} and isinstance(structured, dict):
        for key in ("hits", "files", "results", "items", "matches"):
            if isinstance(structured.get(key), list):
                structured[key] = filter_path_hits(structured[key])
                structured["count"] = len(structured[key])
        # rebuild text lightly for list_files
        if tool == "memo_list_files" and isinstance(structured.get("files"), list):
            files = structured["files"]
            lines = [f"Memo vault contains {len(files)} markdown file(s):", ""]
            lines += [f"- {f.get('path')}  —  {f.get('title')}" for f in files]
            result["content"] = [{"type": "text", "text": "\n".join(lines)}]

    if tool in {"memo_get_observations", "memo_session_timeline"} and isinstance(structured, dict):
        if isinstance(structured.get("items"), list):
            structured["items"] = filter_observation_items(structured["items"])
            structured["count"] = len(structured["items"])

    if tool == "memo_get_dossier" and isinstance(structured, dict):
        content = structured.get("content")
        if isinstance(content, str):
            path = str(structured.get("path") or "")
            if path.startswith("sessions/") or "/sessions/" in f"/{path}":
                content = strip_hidden_type_lines(content)
            structured["content"] = content
            result["content"] = [{"type": "text", "text": f"# Dossier: {path}\n\n{content}"}]

    # Always redact structured + text leaving the process.
    if structured is not None:
        result["structuredContent"] = _redact_payload(structured)
    new_content = []
    for part in text_parts:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            try:
                from memo_capture import privacy
                part = dict(part)
                part["text"] = privacy.redact(part["text"], force=True)
            except Exception:
                pass
        new_content.append(part)
    if new_content:
        result["content"] = new_content
    return response


def filter_tools_definition(tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [t for t in tools if isinstance(t, dict) and t.get("name") in REMOTE_ALLOWED_TOOLS]


def install() -> None:
    """Mark remote policy active. Hooks are invoked from mcp_server.handle_request."""
    global _installed
    _installed = True
    sys.stderr.write("memo-remote: surface policy installed (reads + memo_save_memory)\n")
    sys.stderr.flush()


def is_installed() -> bool:
    return _installed
