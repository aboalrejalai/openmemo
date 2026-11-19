"""Which project a session belongs to, and which vault note describes it.

Name resolution follows claude-mem's order:
1. $CLAUDE_PROJECT_DIR (applied by the adapter);
2. the `project_environments` setting (absolute path prefix → name);
3. with project_name_source = git-remote, the origin's owner/repo slug;
4. the git toplevel's folder name (a worktree or submodule becomes
   parent/child);
5. a `.memo-project` marker file walking upward (stops at home and tmp);
6. the folder name of cwd ("unknown-project" at the filesystem root).
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

MARKERS = (".memo-project", ".memo.json")
UNKNOWN = "unknown-project"
_MAX_WALK = 64


@dataclass(frozen=True)
class Project:
    name: str            # the key used for folders and filters, e.g. "my-project"
    source: str          # environment | git-remote | git | worktree | submodule | marker | cwd
    root: str            # the folder the name came from


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


def parse_origin_slug(url: str) -> Optional[str]:
    url = (url or "").strip()
    if not url or url.startswith(("/", ".", "~", "file:")) or re.match(r"^[a-z]:[\\/]", url, re.I):
        return None
    cleaned = re.sub(r"\.git$", "", url.rstrip("/"), flags=re.I)
    m = re.match(r"^[a-z][a-z0-9+.-]*://[^/]+/(.+)$", cleaned, re.I) or \
        re.match(r"^(?:[^/@\s]+@)?[^:/\s]+:(?!//)(.+)$", cleaned)
    if not m:
        return None
    parts = [p for p in m.group(1).split("/") if p and p != "_git"]
    return "/".join(parts[-2:]) if parts else None


def _marker_root(cwd: str) -> Optional[str]:
    stops = {str(Path.home().resolve()), str(Path(tempfile.gettempdir()).resolve()), "/tmp", "/private/tmp"}
    current = Path(cwd).resolve()
    for _ in range(_MAX_WALK):
        if str(current) in stops or current.parent == current:
            return None
        if any((current / m).exists() for m in MARKERS):
            return str(current)
        current = current.parent
    return None


def _environment(cwd: str, mapping: Dict[str, str]) -> Optional[str]:
    resolved = str(Path(cwd).resolve())
    best = None
    for prefix, name in (mapping or {}).items():
        p = str(Path(os.path.expanduser(prefix)).resolve())
        if (resolved == p or resolved.startswith(p.rstrip("/") + "/")) and (best is None or len(p) > len(best[0])):
            best = (p, str(name))
    return best[1] if best else None


@functools.lru_cache(maxsize=256)
def resolve(cwd: str) -> Project:
    from . import settings
    cwd = cwd or os.getcwd()
    if not os.path.isdir(cwd):
        return Project(Path(cwd).name or UNKNOWN, "cwd", cwd)
    env_name = _environment(cwd, settings.get("project_environments"))
    if env_name:
        return Project(env_name, "environment", cwd)
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top:
        if settings.get("project_name_source") == "git-remote":
            slug = parse_origin_slug(_git(top, "remote", "get-url", "origin") or "")
            if slug:
                return Project(slug, "git-remote", top)
        superproject = _git(top, "rev-parse", "--show-superproject-working-tree")
        if superproject:
            return Project(f"{Path(superproject).name}/{Path(top).name}", "submodule", top)
        common = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
        git_dir = _git(top, "rev-parse", "--path-format=absolute", "--git-dir")
        if common and git_dir and Path(common).resolve() != Path(git_dir).resolve():
            main_root = Path(common).resolve().parent
            return Project(f"{main_root.name}/{Path(top).name}", "worktree", top)
        return Project(Path(top).name, "git", top)
    marker = _marker_root(cwd)
    if marker:
        return Project(Path(marker).name, "marker", marker)
    name = Path(cwd).resolve().name
    return Project(name or UNKNOWN, "cwd", cwd)


def folder_key(name: str) -> str:
    """A project name as a safe relative folder path under sessions/."""
    parts = [re.sub(r"[^\w.\-]+", "-", p, flags=re.UNICODE).strip("-.") or UNKNOWN
             for p in name.split("/") if p]
    return "/".join(parts) or UNKNOWN


def vault_note(project: Project, vault: Path) -> Optional[str]:
    """The vault note (relative path) for this project, or None.

    Match order: frontmatter `memo_project`, file name, `repo` URL ending in
    the name or slug, then `aliases`.
    """
    projects = vault / "projects"
    if not projects.is_dir():
        return None
    base = project.name.split("/")[0].lower()
    wanted = {project.name.lower(), base}
    notes = sorted(projects.glob("*.md"))
    props = {}
    for note in notes:
        try:
            head = note.read_text(encoding="utf-8")[:4000]
        except OSError:
            continue
        props[note] = _frontmatter(head)
    for note in notes:
        if str(props.get(note, {}).get("memo_project", "")).lower() in wanted:
            return f"projects/{note.name}"
    for note in notes:
        if note.stem.lower() in wanted:
            return f"projects/{note.name}"
    for note in notes:
        slug = parse_origin_slug(str(props.get(note, {}).get("repo", "")))
        if slug and (slug.lower() in wanted or slug.split("/")[-1].lower() in wanted):
            return f"projects/{note.name}"
    for note in notes:
        aliases = props.get(note, {}).get("aliases") or []
        if any(str(a).lower() in wanted or str(a).lower() == f"project-{base}" for a in aliases):
            return f"projects/{note.name}"
    return None


def _frontmatter(text: str) -> Dict[str, object]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else ""
    out: Dict[str, object] = {}
    key = None
    for line in block.splitlines():
        if re.match(r"^[A-Za-z_][\w-]*:", line):
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            out[key] = value.strip("'\"") if value else []
        elif key and line.strip().startswith("- ") and isinstance(out.get(key), list):
            out[key].append(line.strip()[2:].strip("'\""))
    return out
