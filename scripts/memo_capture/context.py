"""SessionStart context: what the next session needs to know.

Layout follows claude-mem's agent formatter: a header, a legend, stats, then
a day-by-day timeline of session requests and one-line observation index
rows (ID TIME TYPE TITLE), the latest summary in full, and a footer that
points to the retrieval tools. Details stay behind memo_get_observations
(progressive disclosure).

The block must fit the host's 10,000-character limit. It is rendered,
measured, and reduced in claude-mem's order until it fits: full narratives
off, latest summary off, last reply off, half the sessions, half the
observations.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from typing import List, Optional

from . import project as projects
from . import prompts, settings, state
from .adapters import HookResult, NormalizedInput

INSTRUCTIONS = """How to use Memo's memory:
1. memo_search(query, project?) → a compact index of matching observations and sessions.
2. memo_timeline(anchor=<id or date>) → what happened around one item.
3. memo_get_observations([ids]) → full facts, narrative and files for chosen ids.
Fetch details only for what the current task needs. Past sessions are also in the
vault as Markdown under sessions/<project>/."""


@dataclass(frozen=True)
class Config:
    observations: int
    sessions: int
    full_count: int
    full_field: str
    show_last_summary: bool
    show_last_message: bool
    show_work_tokens: bool
    show_read_tokens: bool
    show_savings: bool


def _config() -> Config:
    s = settings.load()
    return Config(observations=int(s["context_observations"]), sessions=int(s["context_session_count"]),
                  full_count=int(s["context_full_count"]), full_field=str(s["context_full_field"]),
                  show_last_summary=bool(s["context_show_last_summary"]),
                  show_last_message=bool(s["context_show_last_message"]),
                  show_work_tokens=bool(s["context_show_work_tokens"]),
                  show_read_tokens=bool(s["context_show_read_tokens"]),
                  show_savings=bool(s["context_show_savings_percent"]))


def _reduce(cfg: Config, count: int):
    # The latest summary ("where we stopped, what comes next") goes last: it is
    # what a returning session needs most, so observations are trimmed first.
    if cfg.full_count > 0:
        return replace(cfg, full_count=0), count
    if cfg.show_last_message:
        return replace(cfg, show_last_message=False), count
    if count > 10:
        return cfg, count // 2
    if cfg.show_last_summary:
        return replace(cfg, show_last_summary=False), count
    if cfg.sessions > 0:
        return replace(cfg, sessions=cfg.sessions // 2), count
    if count > 1:
        return cfg, count // 2
    return None


def _project_clause(project_names: Optional[List[str]]):
    if not project_names:
        return "", []
    marks = ",".join("?" * len(project_names))
    return f" AND project IN ({marks})", list(project_names)


def load_items(conn, project_names: Optional[List[str]], limit: int, sessions: int):
    clause, params = _project_clause(project_names)
    types = settings.csv("context_observation_types")
    concepts = settings.csv("context_observation_concepts")
    where = clause
    if types:
        where += f" AND type IN ({','.join('?' * len(types))})"
        params += types
    if settings.get("context_main_agent_only"):
        where += " AND agent_id IS NULL"
    rows = conn.execute(f"SELECT * FROM observations WHERE 1=1{where} ORDER BY created_at DESC, id DESC LIMIT ?",
                        (*params, limit * 3 if concepts else limit)).fetchall()
    if concepts:
        rows = [r for r in rows if set(state.loads(r["concepts"])) & set(concepts)][:limit]
    clause, sparams = _project_clause(project_names)
    summaries = conn.execute(f"SELECT * FROM session_summaries WHERE 1=1{clause} ORDER BY created_at DESC, id DESC "
                             f"LIMIT ?", (*sparams, max(sessions, 1))).fetchall()
    return list(rows), list(summaries)


def _clip(text: str, limit: int) -> str:
    """One request's summary can run to thousands of characters; the context
    keeps the start of each field so the latest summary always fits."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _when(ms: int, fmt: str) -> str:
    return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime(fmt)


def render(project_label: str, observations, summaries, cfg: Config, last_message: str = "") -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    legend = " ".join(f"{t['emoji']}{t['id']}" for t in prompts.load_mode()["observation_types"])
    read_tokens = sum(len((o["title"] or "") + (o["narrative"] or "") + (o["facts"] or "")) // 4 for o in observations)
    work_tokens = sum(int(o["discovery_tokens"] or 0) for o in observations) + \
        sum(int(s["discovery_tokens"] or 0) for s in summaries)
    lines = [f"# [{project_label}] recent context, {now}", "Mode: Code and project work (code)", "",
             f"Legend: 🎯session {legend}", "Format: ID TIME TYPE TITLE",
             "Fetch details: memo_get_observations([IDs]) | Search: memo_search", ""]
    stats = [f"{len(observations)} obs"]
    if cfg.show_read_tokens:
        stats[0] += f" ({read_tokens:,}t read)"
    if cfg.show_work_tokens:
        stats.append(f"{work_tokens:,}t work")
    if cfg.show_savings and work_tokens > 0:
        stats.append(f"{max(0, round(100 * (1 - read_tokens / work_tokens)))}% savings")
    lines += ["Stats: " + " | ".join(stats), ""]

    shown_summaries = summaries[: cfg.sessions]
    items = [("o", o["created_at"], o) for o in observations] + [("s", s["created_at"], s) for s in shown_summaries]
    items.sort(key=lambda it: (it[1], 0 if it[0] == "s" else 1))
    full_ids = {o["id"] for o in observations[: cfg.full_count]}
    day = None
    for kind, ts, row in items:
        this_day = _when(ts, "%Y-%m-%d")
        if this_day != day:
            day = this_day
            lines.append(f"### {day}")
        if kind == "s":
            lines.append(f"S{row['id']} 🎯 {_clip(row['request'] or 'Session started', 300)} ({_when(ts, '%H:%M')})")
            continue
        icon = prompts.type_emoji(row["type"])
        if row["id"] in full_ids:
            lines.append(f"**{row['id']}** {_when(ts, '%H:%M')} {icon} **{row['title']}**")
            detail = row["narrative"] if cfg.full_field == "narrative" else "\n".join(
                f"- {f}" for f in state.loads(row["facts"]))
            if detail:
                lines.append(detail)
        else:
            lines.append(f"{row['id']} {_when(ts, '%H:%M')} {icon} {row['title']}")
    if cfg.show_last_summary and summaries:
        last = summaries[0]
        lines += ["", f"Latest summary (S{last['id']}, {_when(last['created_at'], '%Y-%m-%d %H:%M')}):"]
        for label, key in (("Request", "request"), ("Investigated", "investigated"), ("Learned", "learned"),
                           ("Completed", "completed"), ("Next Steps", "next_steps"), ("Notes", "notes")):
            if last[key]:
                lines.append(f"**{label}**: {_clip(last[key], 900)}")
    if cfg.show_last_message and last_message:
        lines += ["", "---", "", "**Previously**", "", f"A: {last_message}"]
    lines += ["", f"Access {round(work_tokens / 1000)}k tokens of past work via memo_get_observations([IDs]) "
                  "or memo_search.", "", INSTRUCTIONS]
    return "\n".join(lines)


def build(conn, project_names: Optional[List[str]], label: str, budget: Optional[int] = None):
    """(context text, observations shown, sessions shown); ("", 0, 0) when there is no memory."""
    cfg = _config()
    budget = int(budget or settings.get("context_budget_chars"))
    observations, summaries = load_items(conn, project_names, cfg.observations, cfg.sessions)
    if not observations and not summaries:
        return "", 0, 0
    count = len(observations)
    wrap = lambda body: f"<memo-context>\n{body}\n</memo-context>"
    text = wrap(render(label, observations[:count], summaries, cfg))
    while len(text) > budget:
        nxt = _reduce(cfg, count)
        if nxt is None:
            text = text[: budget - 20] + "\n</memo-context>"
            break
        cfg, count = nxt
        text = wrap(render(label, observations[:count], summaries, cfg))
    return text, count, min(len(summaries), cfg.sessions)


def project_names_for(cwd: str) -> Optional[List[str]]:
    if settings.get("context_projects") == "all":
        return None
    proj = projects.resolve(cwd)
    names = [proj.name]
    base = proj.name.split("/")[0]
    if base != proj.name:
        names.append(base)        # a worktree also sees its main repo's memory
    return names


def repo_state(cwd: str, commits: int = 5) -> str:
    """The working repository as git sees it now: branch, uncommitted files and
    the last commits. Always current, even when memory is behind."""
    import subprocess

    def git(*args: str) -> str:
        try:
            res = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3)
            return res.stdout.strip() if res.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    if not cwd or not git("rev-parse", "--git-dir"):
        return ""
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "?"
    dirty = len([l for l in git("status", "--porcelain").splitlines() if l.strip()])
    log = git("log", f"-{commits}", "--format=%h %ad %s", "--date=format:%Y-%m-%d %H:%M")
    lines = ["## Repository now (git; newer than memory when they differ)",
             f"Branch: {branch} · {dirty} uncommitted file(s)"]
    lines += [f"- {l[:140]}" for l in log.splitlines()]
    return "\n".join(lines)


def session_start(inp: NormalizedInput) -> Optional[HookResult]:
    from . import health
    names = project_names_for(inp.cwd)
    label = names[0] if names else "all projects"
    if names and names[0] in settings.csv("excluded_projects"):
        return None
    repo = repo_state(inp.cwd)
    text, n_obs, n_sessions, db_error = "", 0, 0, ""
    try:
        conn = state.connect()
        budget = int(settings.get("context_budget_chars")) - len(repo) - 400
        text, n_obs, n_sessions = build(conn, names, label, budget=max(budget, 1000))
    except Exception as exc:          # a broken index must not hide the warning
        db_error = f"{type(exc).__name__}: {exc}"
    warn = health.warning(db_error=db_error)
    sync_warn = health.sync_warning()
    if not text and not warn and not sync_warn:
        return None
    if text and repo:
        text = text[: -len("\n</memo-context>")] + "\n\n" + repo + "\n</memo-context>"
    banner_lines = [x for x in (warn, sync_warn) if x]
    if banner_lines:
        banner = "<memo-context>\n" + "\n".join(banner_lines) + "\n</memo-context>"
        text = banner + ("\n" + text if text else "")
    message = f"Memo: {label} — {n_obs} observation(s), {n_sessions} session summary(ies) loaded."
    banner_msg = " ".join(x for x in (warn, sync_warn) if x)
    return HookResult(additional_context=text,
                      system_message=(banner_msg + " " + message) if banner_msg else message)
