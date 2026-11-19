"""Read the last assistant turn from a Claude Code transcript (JSONL).

A turn's reply is spread over several assistant lines (text, tool_use,
text…). Memo keeps every text part since the last real user prompt, so the
summary sees the whole answer, not only its final sentence.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional, Tuple

TAIL_BYTES = 4 * 1024 * 1024


def _text_of(content) -> Optional[str]:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") in (None, "text") and isinstance(c.get("text"), str)]
        return "\n".join(p for p in parts if p) if parts else None
    return None


def _message(line: dict) -> dict:
    """{role, content, model} for Claude Code lines ({type, message: {role, content}}) and
    Cursor lines ({role, message: {content}})."""
    msg = line.get("message") if isinstance(line.get("message"), dict) else {}
    return {"role": msg.get("role") or line.get("role"), "content": msg.get("content", line.get("content")),
            "model": msg.get("model")}


def _role(line: dict) -> Optional[str]:
    role = _message(line)["role"]
    kind = line.get("type")
    if kind in ("user", "assistant") and kind != role:
        return None
    return role


def _is_real_prompt(line: dict) -> bool:
    msg = _message(line)
    if _role(line) != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return False
    return bool(_text_of(content))


def last_assistant_turn(path: Optional[str]) -> Tuple[str, Optional[str]]:
    """(text, model) of the newest assistant turn; ("", None) when unreadable."""
    if not path or not os.path.isfile(path):
        return "", None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return "", None
    lines = chunk.splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]  # the first line of a tail read may be cut
    texts, model = [], None
    for raw in reversed(lines):
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict):
            continue
        if _is_real_prompt(line):
            break
        msg = _message(line)
        if _role(line) == "assistant":
            model = model or msg.get("model")
            text = _text_of(msg.get("content"))
            if text:
                texts.append(text)
    return "\n\n".join(reversed(texts)).strip(), model


_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_STAMP = re.compile(r"<timestamp>.*?</timestamp>", re.S)


def last_user_prompt(path: Optional[str]) -> str:
    """The newest real user prompt (Cursor's <user_query> wrapper removed)."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as fh:
            size = os.path.getsize(path)
            fh.seek(max(0, size - TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if isinstance(line, dict) and _is_real_prompt(line):
            text = _text_of(_message(line)["content"]) or ""
            m = _QUERY.search(text)
            return (m.group(1) if m else _STAMP.sub("", text)).strip()
    return ""
