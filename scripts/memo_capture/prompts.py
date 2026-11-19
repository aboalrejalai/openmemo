"""Prompts sent to the observer, built from plugin/modes/<mode>.json.

Conversation shape (one observer conversation per session):
  init or continuation prompt  →  one prompt per tool event  →  summary prompt
"""

from __future__ import annotations

import datetime
import functools
import json
from pathlib import Path
from typing import Any, Dict, Optional

from . import privacy
from .paths import vault_dir

LANGUAGES = {
    "en": "ALWAYS write titles, facts, narratives and summaries in English — even when the user prompt is Arabic. Never use Arabic in titles or memory fields. Chat replies to the user are separate; observer memory is English only.",
    "ar": "اكتب العناوين والحقائق والسرد بالعربية الفصحى البسيطة، واترك أسماء الملفات والأوامر والمنتجات كما هي.",
}


@functools.lru_cache(maxsize=4)
def load_mode(name: str = "code") -> Dict[str, Any]:
    path = vault_dir() / "plugin" / "modes" / f"{name}.json"
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / "plugin" / "modes" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _observation_skeleton(p: Dict[str, str]) -> str:
    return (
        "<observation>\n"
        "  <type>[one type]</type>\n"
        f"  <title>{p['title_placeholder']}</title>\n"
        f"  <subtitle>{p['subtitle_placeholder']}</subtitle>\n"
        f"  <facts>\n    <fact>{p['fact_placeholder']}</fact>\n    <fact>{p['fact_placeholder']}</fact>\n  </facts>\n"
        f"  <narrative>{p['narrative_placeholder']}</narrative>\n"
        f"  <concepts>\n    <concept>{p['concept_placeholder']}</concept>\n  </concepts>\n"
        f"  <files_read>\n    <file>{p['file_placeholder']}</file>\n  </files_read>\n"
        f"  <files_modified>\n    <file>{p['file_placeholder']}</file>\n  </files_modified>\n"
        "</observation>"
    )


def _summary_skeleton(p: Dict[str, str]) -> str:
    return (
        "<summary>\n"
        f"  <request>{p['summary_request_placeholder']}</request>\n"
        f"  <investigated>{p['summary_investigated_placeholder']}</investigated>\n"
        f"  <learned>{p['summary_learned_placeholder']}</learned>\n"
        f"  <completed>{p['summary_completed_placeholder']}</completed>\n"
        f"  <next_steps>{p['summary_next_steps_placeholder']}</next_steps>\n"
        f"  <notes>{p['summary_notes_placeholder']}</notes>\n"
        "</summary>"
    )


def _request_block(user_prompt: str, when: Optional[str]) -> str:
    when = when or datetime.date.today().isoformat()
    return ("<observed_from_primary_session>\n"
            f"  <user_request>{user_prompt}</user_request>\n"
            f"  <requested_at>{when}</requested_at>\n"
            "</observed_from_primary_session>")


def _guidance(p: Dict[str, str], language: str) -> str:
    return "\n\n".join([
        p["owner"], p["role"], p["location"], p["record"], p["skip"],
        p["types"], p["concepts"], p["fields"], LANGUAGES.get(language, LANGUAGES["en"]),
        privacy.REDACTION_HINT,
        p["output_header"], _observation_skeleton(p), p["footer"],
    ])


def init_prompt(project: str, user_prompt: str, *, mode: str = "code", language: str = "en",
                requested_at: Optional[str] = None) -> str:
    p = load_mode(mode)["prompts"]
    return "\n\n".join([
        p["identity"], f"Project: {project}", _guidance(p, language),
        _request_block(user_prompt, requested_at), p["start_header"],
    ])


def continuation_prompt(project: str, user_prompt: str, prompt_number: int, *, mode: str = "code",
                        language: str = "en", requested_at: Optional[str] = None,
                        include_guidance: bool = False) -> str:
    """A new user request in a running observer conversation.

    include_guidance is used after the conversation was recycled or resumed
    elsewhere, when the observer no longer has the init prompt in view.
    """
    p = load_mode(mode)["prompts"]
    parts = [p["continue_greeting"], f"Project: {project} · request #{prompt_number}"]
    if include_guidance:
        parts.insert(0, p["identity"])
        parts.append(_guidance(p, language))
    parts += [_request_block(user_prompt, requested_at), p["continue_instruction"], p["continue_header"]]
    return "\n\n".join(parts)


def observation_prompt(tool_name: str, tool_input: str, tool_response: str, cwd: str,
                       observed_at_ms: Optional[int] = None) -> str:
    at = datetime.datetime.fromtimestamp((observed_at_ms or 0) / 1000).isoformat(timespec="seconds") \
        if observed_at_ms else datetime.datetime.now().isoformat(timespec="seconds")
    return ("<observed_from_primary_session>\n"
            f"  <what_happened>{tool_name}</what_happened>\n"
            f"  <occurred_at>{at}</occurred_at>\n"
            f"  <working_directory>{cwd}</working_directory>\n"
            f"  <parameters>{tool_input}</parameters>\n"
            f"  <outcome>{tool_response}</outcome>\n"
            "</observed_from_primary_session>\n\n"
            "Reply with one or more <observation> blocks, or <skip_summary reason=\"...\" />. No prose.")


def summary_prompt(last_assistant_message: str, *, mode: str = "code") -> str:
    p = load_mode(mode)["prompts"]
    return "\n\n".join([
        p["summary_header"], p["summary_instruction"],
        f"{p['summary_reply_label']}\n{last_assistant_message or '(no text reply)'}",
        p["summary_format"], _summary_skeleton(p), p["summary_footer"],
    ])


def type_emoji(type_id: str, mode: str = "code") -> str:
    for t in load_mode(mode)["observation_types"]:
        if t["id"] == type_id:
            return t.get("emoji", "•")
    return "•"


def valid_types(mode: str = "code"):
    return [t["id"] for t in load_mode(mode)["observation_types"]]


def valid_concepts(mode: str = "code"):
    return [c["id"] for c in load_mode(mode)["observation_concepts"]]
