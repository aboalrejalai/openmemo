"""Parse the observer's XML replies: observations, a summary, or a skip.

Tolerant by design: models sometimes wrap XML in code fences, add prose, or
leave a tag unclosed. Anything that is not a recognisable block is ignored.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import List, Optional

_OBS = re.compile(r"<observation\b[^>]*>(.*?)(?:</observation>|(?=<observation\b)|\Z)", re.S | re.I)
_SUMMARY = re.compile(r"<summary\b[^>]*>(.*?)(?:</summary>|\Z)", re.S | re.I)
_SKIP = re.compile(r"<skip_summary\b([^>]*)/?>", re.I)
_REASON = re.compile(r"reason\s*=\s*[\"']([^\"']*)[\"']", re.I)


@dataclass
class Observation:
    type: str
    title: str
    subtitle: str = ""
    facts: List[str] = field(default_factory=list)
    narrative: str = ""
    concepts: List[str] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)


@dataclass
class Summary:
    request: str = ""
    investigated: str = ""
    learned: str = ""
    completed: str = ""
    next_steps: str = ""
    notes: str = ""

    def empty(self) -> bool:
        return not any([self.request, self.investigated, self.learned, self.completed,
                        self.next_steps, self.notes])


@dataclass
class Reply:
    observations: List[Observation] = field(default_factory=list)
    summary: Optional[Summary] = None
    skipped: bool = False
    skip_reason: str = ""


def _clean(text: str) -> str:
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text or "", flags=re.S)
    text = re.sub(r"</?[A-Za-z_][\w-]*[^>]*>", "", text)  # stray inner tags
    return html.unescape(text).strip()


def _tag(body: str, name: str) -> str:
    m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}\s*>", body, re.S | re.I)
    if not m:
        m = re.search(rf"<{name}\b[^>]*>(.*?)(?=<[A-Za-z_]|\Z)", body, re.S | re.I)
    return _clean(m.group(1)) if m else ""


def _items(body: str, parent: str, child: str) -> List[str]:
    m = re.search(rf"<{parent}\b[^>]*>(.*?)(?:</{parent}\s*>|\Z)", body, re.S | re.I)
    if not m:
        return []
    out, seen = [], set()
    for item in re.findall(rf"<{child}\b[^>]*>(.*?)</{child}\s*>", m.group(1), re.S | re.I):
        value = _clean(item)
        if value and value.lower() not in seen and not value.startswith("["):
            seen.add(value.lower())
            out.append(value)
    return out


def parse(text: str, valid_types: Optional[List[str]] = None,
          valid_concepts: Optional[List[str]] = None) -> Reply:
    text = text or ""
    reply = Reply()
    summary_match = _SUMMARY.search(text)
    if summary_match:
        body = summary_match.group(1)
        s = Summary(**{k: _tag(body, k) for k in
                       ("request", "investigated", "learned", "completed", "next_steps", "notes")})
        if not s.empty():
            reply.summary = s
    for m in _OBS.finditer(text):
        body = m.group(1)
        title = _tag(body, "title")
        if not title or title.startswith("["):
            continue
        obs_type = _tag(body, "type").lower().strip()
        if valid_types and obs_type not in valid_types:
            obs_type = "change" if "change" in valid_types else valid_types[0]
        concepts = [c.lower() for c in _items(body, "concepts", "concept")]
        if valid_concepts:
            concepts = [c for c in concepts if c in valid_concepts and c != obs_type]
        reply.observations.append(Observation(
            type=obs_type or "change", title=title, subtitle=_tag(body, "subtitle"),
            facts=_items(body, "facts", "fact"), narrative=_tag(body, "narrative"),
            concepts=concepts[:5], files_read=_items(body, "files_read", "file"),
            files_modified=_items(body, "files_modified", "file")))
    skip = _SKIP.search(text)
    if skip and not reply.observations and reply.summary is None:
        reply.skipped = True
        reason = _REASON.search(skip.group(1) or "")
        reply.skip_reason = reason.group(1) if reason else ""
    return reply
