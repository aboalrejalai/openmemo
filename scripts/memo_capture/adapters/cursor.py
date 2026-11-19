"""Cursor (IDE agent and cursor-agent) hook payloads ↔ Memo.

Cursor plugin hooks (and legacy ~/.cursor/hooks.json) send conversation_id,
workspace_roots, prompt, command/output (shell), tool_name/tool_input/
result_json (MCP), file_path/edits (file edits) and status (stop). They can
only answer {"continue": true}, so Memo's context for Cursor is written to a
rule file in the workspace, .cursor/rules/memo-context.mdc, which is kept out
of git through .git/info/exclude (never through the project's .gitignore).
Install: `memo install-cursor` → ~/.cursor/plugins/local/memo.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from . import HookResult, NormalizedInput

RULE_PATH = Path(".cursor") / "rules" / "memo-context.mdc"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def transcript_path(cwd: str, session_id: str) -> Optional[str]:
    if not cwd or not session_id or not _SAFE_ID.match(session_id):
        return None
    slug = cwd.lstrip("/").replace("/", "-").replace(".", "-")
    candidate = Path.home() / ".cursor" / "projects" / slug / "agent-transcripts" / session_id / f"{session_id}.jsonl"
    return str(candidate) if candidate.exists() else None


def normalize(raw: Dict[str, Any], event: str) -> NormalizedInput:
    roots = raw.get("workspace_roots") or []
    cwd = (roots[0] if roots else None) or raw.get("cwd") or os.getcwd()
    sid = str(raw.get("conversation_id") or raw.get("generation_id") or raw.get("id") or "")
    shell = bool(raw.get("command")) and not raw.get("tool_name")
    edit = bool(raw.get("file_path")) and raw.get("edits") is not None
    if shell:
        tool, tool_input, response = "Bash", {"command": raw.get("command")}, {"output": raw.get("output")}
    elif edit:
        tool, tool_input, response = "Edit", {"file_path": raw.get("file_path"), "edits": raw.get("edits")}, {}
    else:
        tool, tool_input, response = raw.get("tool_name"), raw.get("tool_input"), raw.get("result_json")
    tool_use_id = raw.get("tool_use_id") or raw.get("tool_call_id")
    prompt = raw.get("prompt") or raw.get("query") or raw.get("input") or raw.get("message")
    return NormalizedInput(
        host="cursor",
        event=event,
        session_id=sid,
        cwd=str(cwd),
        prompt=prompt if isinstance(prompt, str) else None,
        tool_name=tool if isinstance(tool, str) else None,
        tool_input=tool_input,
        tool_response=response,
        tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
        transcript_path=raw.get("transcript_path") if isinstance(raw.get("transcript_path"), str)
        else transcript_path(str(cwd), sid),
        raw=raw,
    )


def _exclude_from_git(workspace: Path) -> None:
    try:
        git_dir = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=workspace,
                                 capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if not git_dir:
        return
    exclude = Path(git_dir) / "info" / "exclude"
    line = RULE_PATH.as_posix()
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if line not in current.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(current + ("" if current.endswith("\n") or not current else "\n") + line + "\n",
                               encoding="utf-8")
    except OSError:
        pass


def write_rule(workspace: str, context: str) -> None:
    root = Path(workspace)
    if not root.is_dir():
        return
    path = root / RULE_PATH
    body = ("---\ndescription: Memo — what earlier sessions in this project did (auto-generated)\n"
            "alwaysApply: true\n---\n\n" + context.strip() + "\n")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != body:
            path.write_text(body, encoding="utf-8")
        _exclude_from_git(root)
    except OSError:
        pass


def format_output(result: HookResult, event: str, inp: Optional[NormalizedInput] = None) -> Dict[str, Any]:
    if event == "context" and result.additional_context and inp is not None:
        write_rule(inp.cwd, result.additional_context)
    return {"continue": True}
