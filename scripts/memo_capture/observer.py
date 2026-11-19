"""The observer: a background conversation that writes memory.

Supports two providers (settings `observer_provider`):

  claude (default historically)
    claude -p --input-format stream-json --output-format stream-json --verbose
           --model <observer_model> --effort <observer_effort>
           --safe-mode --tools "" --strict-mcp-config --permission-mode dontAsk

  agy (Antigravity / Gemini)
    agy --input-format=stream-json --output-format=stream-json --print=
        --model <observer_model> --disable-slash-commands
        --dangerously-skip-permissions
    Optional resume: --conversation <id>

- MEMO_OBSERVER=1 makes Memo's own hooks return at once.
- Conversation id is stored so a restarted worker resumes instead of starting over.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import settings
from .paths import log_line, memo_home, observer_home


class ObserverError(RuntimeError):
    """The observer could not answer: quota, outage, crash or timeout."""


@dataclass
class Answer:
    text: str
    tokens: int
    session_id: Optional[str]
    model: Optional[str]


def provider() -> str:
    value = str(settings.get("observer_provider") or "claude").strip().lower()
    return value if value in ("claude", "agy") else "claude"


def claude_cli() -> str:
    configured = settings.get("claude_cli")
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"),
                      Path("/usr/local/bin/claude"), Path.home() / ".claude/local/claude"):
        if candidate.exists():
            return str(candidate)
    raise ObserverError("the claude CLI was not found; set claude_cli in <vault>/.memo/settings.json")


def agy_cli() -> str:
    configured = settings.get("observer_cli") or settings.get("agy_cli")
    if configured:
        return configured
    found = shutil.which("agy")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/agy", Path("/opt/homebrew/bin/agy"),
                      Path("/usr/local/bin/agy")):
        if candidate.exists():
            return str(candidate)
    raise ObserverError("the agy CLI was not found; set observer_cli in <vault>/.memo/settings.json")


def observer_cwd() -> Path:
    path = observer_home() / "observer-sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_command(resume: Optional[str] = None) -> List[str]:
    model = str(settings.get("observer_model"))
    if provider() == "agy":
        cmd = [agy_cli(),
               "--input-format=stream-json", "--output-format=stream-json",
               "--print=",
               f"--model={model}",
               "--disable-slash-commands",
               "--dangerously-skip-permissions"]
        if resume:
            cmd.append(f"--conversation={resume}")
        return cmd
    cmd = [claude_cli(), "-p",
           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--model", model,
           "--effort", str(settings.get("observer_effort")),
           "--safe-mode", "--tools", "", "--strict-mcp-config",
           "--permission-mode", "dontAsk"]
    if resume:
        cmd += ["--resume", resume]
    return cmd


class Observer:
    """One observer conversation. Not thread-safe: one caller at a time."""

    def __init__(self, resume: Optional[str] = None) -> None:
        self.resume = resume
        self.proc: Optional[subprocess.Popen] = None
        self.session_id: Optional[str] = resume
        self.chars_sent = 0
        self.last_used = time.time()
        self._stderr: List[str] = []
        self._provider = provider()

    # --------------------------------------------------------------- process
    def _spawn(self) -> None:
        env = dict(os.environ, MEMO_OBSERVER="1")
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_ENTRYPOINT",
                    "CLAUDECODE", "CLAUDE_PROJECT_DIR"):
            env.pop(key, None)
        self.proc = subprocess.Popen(build_command(self.resume), cwd=str(observer_cwd()), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", bufsize=1)
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-50]

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(10)
        except Exception:
            self.proc.kill()
            self.proc.wait(5)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        self.proc = None

    # ----------------------------------------------------------------- talk
    def ask(self, text: str, timeout: Optional[float] = None) -> Answer:
        """Send one user message and wait for the observer's full reply."""
        if self._provider == "agy":
            return self._ask_agy(text, timeout)
        return self._ask_claude(text, timeout)

    def _ask_claude(self, text: str, timeout: Optional[float] = None) -> Answer:
        if not self.alive():
            self._spawn()
        assert self.proc and self.proc.stdin and self.proc.stdout
        timeout = float(timeout or settings.get("observer_timeout_sec"))
        message = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise ObserverError(f"observer stdin closed: {exc}; {self._stderr_tail()}")
        self.chars_sent += len(text)
        self.last_used = time.time()

        result: dict = {}
        texts: List[str] = []
        model = None
        timer = threading.Timer(timeout, self._timeout_kill)
        timer.start()
        try:
            for line in self.proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                self.session_id = event.get("session_id") or self.session_id
                kind = event.get("type")
                if kind == "assistant":
                    msg = event.get("message") or {}
                    model = msg.get("model") or model
                    texts += [c.get("text", "") for c in msg.get("content") or []
                              if isinstance(c, dict) and c.get("type") == "text"]
                elif kind == "result":
                    result = event
                    break
        finally:
            timer.cancel()
        if not result:
            self.close()
            raise ObserverError(f"observer ended without a result; {self._stderr_tail()}")
        if result.get("is_error") or result.get("subtype") not in (None, "success"):
            detail = str(result.get("result") or result.get("subtype") or "error")[:500]
            self.close()
            raise ObserverError(f"observer error: {detail}")
        usage = result.get("usage") or {}
        tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0) \
            + int(usage.get("cache_creation_input_tokens") or 0)
        return Answer(text="".join(texts) or str(result.get("result") or ""), tokens=tokens,
                      session_id=self.session_id, model=model)

    def _ask_agy(self, text: str, timeout: Optional[float] = None) -> Answer:
        if not self.alive():
            self._spawn()
        assert self.proc and self.proc.stdin and self.proc.stdout
        timeout = float(timeout or settings.get("observer_timeout_sec"))
        # Antigravity stream-json: event/user + content blocks
        message = {"event": "user",
                   "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        try:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise ObserverError(f"observer stdin closed: {exc}; {self._stderr_tail()}")
        self.chars_sent += len(text)
        self.last_used = time.time()

        result: dict = {}
        texts: List[str] = []
        model = None
        timer = threading.Timer(timeout, self._timeout_kill)
        timer.start()
        try:
            for line in self.proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                self.session_id = (event.get("conversation_id")
                                   or (event.get("result") or {}).get("conversation_id")
                                   or (event.get("init") or {}).get("conversation_id")
                                   or self.session_id)
                kind = event.get("event") or event.get("type")
                if kind == "init":
                    init = event.get("init") or {}
                    model = init.get("model") or model
                elif kind == "step_update":
                    su = event.get("step_update") or {}
                    if su.get("step_type") == "agent_response" and su.get("text_delta"):
                        texts.append(str(su.get("text_delta") or ""))
                    usage = su.get("usage") or {}
                    if usage:
                        pass
                elif kind == "result":
                    result = event.get("result") if isinstance(event.get("result"), dict) else event
                    break
        finally:
            timer.cancel()
        if not result:
            self.close()
            raise ObserverError(f"observer ended without a result; {self._stderr_tail()}")
        status = str(result.get("status") or "").upper()
        if status and status not in ("SUCCESS", "OK"):
            detail = str(result.get("error") or result.get("response") or status)[:500]
            self.close()
            raise ObserverError(f"observer error: {detail}")
        usage = result.get("usage") or {}
        tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0) \
            + int(usage.get("thinking_tokens") or 0) + int(usage.get("cache_read_tokens") or 0)
        body = "".join(texts) or str(result.get("response") or "")
        model = model or str(settings.get("observer_model"))
        return Answer(text=body, tokens=tokens, session_id=self.session_id, model=model)

    def _timeout_kill(self) -> None:
        log_line("observer", "observer reply timed out; killing the process")
        if self.proc:
            self.proc.kill()

    def _stderr_tail(self) -> str:
        return " | ".join(self._stderr[-5:])[:800]

    def too_long(self) -> bool:
        return self.chars_sent > int(settings.get("observer_max_conversation_chars"))


class Pool:
    """Live observers by content session id, closed when idle."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.observers: dict = {}

    def get(self, content_session_id: str, resume: Optional[str]) -> Observer:
        with self.lock:
            obs = self.observers.get(content_session_id)
            if obs is None:
                obs = Observer(resume=resume)
                self.observers[content_session_id] = obs
            return obs

    def drop(self, content_session_id: str) -> None:
        with self.lock:
            obs = self.observers.pop(content_session_id, None)
        if obs:
            obs.close()

    def kill_all(self) -> None:
        """Worker shutdown: stop every observer now; unfinished items are requeued on restart."""
        with self.lock:
            observers, self.observers = list(self.observers.values()), {}
        for obs in observers:
            if obs.proc and obs.proc.poll() is None:
                obs.proc.kill()

    def close_idle(self) -> None:
        limit = int(settings.get("observer_idle_close_sec"))
        with self.lock:
            idle = [k for k, o in self.observers.items() if time.time() - o.last_used > limit]
        for key in idle:
            self.drop(key)


POOL = Pool()
