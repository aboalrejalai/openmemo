#!/usr/bin/env python3
"""Git merge drivers for Memo vault notes: timeline (D3) and note frontmatter (D7).

Usage (registered by memo_core.ensure_git_config):
    python3 scripts/memo_merge.py <timeline|note> %O %A %B
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from memo_core import (  # noqa: E402
    BLOCK_HEADER_RE,
    CHILD_RE,
    LIVING_HISTORY_HEADING,
    MONTH_HEADING_RE,
    MONTH_INDEX,
    MONTH_NAMES,
    WIKILINK_RE,
    _UPDATED_LINE_RE,
    _month_key,
)

CONFLICT_START = re.compile(r"^<<<<<<< ")
CONFLICT_SEP = re.compile(r"^=======\s*$")
CONFLICT_END = re.compile(r"^>>>>>>> ")

_DATE_OR_ISO = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)

EntryKey = Tuple


class MergeConflict(Exception):
    pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 4 or args[0] not in ("timeline", "note"):
        sys.stderr.write("usage: memo_merge.py <timeline|note> %O %A %B\n")
        return 2
    mode, base, ours, theirs = args
    try:
        if mode == "timeline":
            return merge_timeline(Path(base), Path(ours), Path(theirs))
        return merge_note(Path(base), Path(ours), Path(theirs))
    except Exception as exc:
        sys.stderr.write(f"memo_merge: {exc}\n")
        return 1


def merge_timeline(base: Path, ours: Path, theirs: Path) -> int:
    o_text = ours.read_text(encoding="utf-8")
    a_text = base.read_text(encoding="utf-8")
    b_text = theirs.read_text(encoding="utf-8")
    merged, conflicts = git_merge_file(o_text, a_text, b_text)
    if conflicts == 0:
        ours.write_text(merged, encoding="utf-8")
        return 0
    try:
        result = resolve_timeline_conflict(merged, a_text, o_text, b_text)
    except MergeConflict:
        ours.write_text(merged, encoding="utf-8")
        return 1
    ours.write_text(result, encoding="utf-8")
    return 0


def merge_note(base: Path, ours: Path, theirs: Path) -> int:
    o_text = ours.read_text(encoding="utf-8")
    a_text = base.read_text(encoding="utf-8")
    b_text = theirs.read_text(encoding="utf-8")
    merged, conflicts = git_merge_file(o_text, a_text, b_text)
    if conflicts == 0:
        ours.write_text(merged, encoding="utf-8")
        return 0
    try:
        result = resolve_note_frontmatter(merged, a_text, o_text, b_text)
    except MergeConflict:
        ours.write_text(merged, encoding="utf-8")
        return 1
    ours.write_text(result, encoding="utf-8")
    return 0


def git_merge_file(ours: str, base: str, theirs: str) -> Tuple[str, int]:
    with tempfile.TemporaryDirectory(prefix="memo-merge-") as tmp:
        t = Path(tmp)
        op, ap, bp = t / "ours", t / "base", t / "theirs"
        op.write_text(ours, encoding="utf-8")
        ap.write_text(base, encoding="utf-8")
        bp.write_text(theirs, encoding="utf-8")
        res = subprocess.run(
            ["git", "merge-file", "-p",
             "-L", "ours", "-L", "base", "-L", "theirs",
             str(op), str(ap), str(bp)],
            capture_output=True, text=True,
        )
        if res.returncode < 0:
            raise MergeConflict(f"merge-file error: {res.stderr}")
        return res.stdout, res.returncode


# ---------------------------------------------------------------------------
# Frontmatter (D7)
# ---------------------------------------------------------------------------

def parse_updated_value(raw: str) -> Optional[str]:
    val = raw.strip()
    if not val:
        return None
    if val[:1] in ("'", '"'):
        q = val[0]
        end = val.find(q, 1)
        if end == -1:
            return None
        core = val[1:end]
    else:
        core = val.split("#", 1)[0].rstrip()
    if not _DATE_OR_ISO.fullmatch(core):
        return None
    return core


def updated_line_from(text: str) -> Optional[str]:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        return None
    m = _UPDATED_LINE_RE.search(text[4:end])
    return m.group(0) if m else None


def resolve_updated_line(base: str, ours: str, theirs: str) -> Optional[str]:
    ol, tl = updated_line_from(ours), updated_line_from(theirs)
    if ol is None and tl is None:
        return None
    if ol is None:
        return tl
    if tl is None:
        return ol
    if ol == tl:
        return ol
    ov = parse_updated_value(ol.split(":", 1)[1])
    tv = parse_updated_value(tl.split(":", 1)[1])
    if ov is None or tv is None:
        raise MergeConflict("non-date updated:")
    return ol if ov >= tv else tl


def extract_sb_key(text: str, key: str) -> Optional[str]:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        return None
    block = text[4:end]
    if key == "second-brain-id":
        m = re.search(r"^second-brain-id:.*(?:\n[ \t]+-.*)*\n?", block, re.MULTILINE)
    else:
        m = re.search(r"^second-brain-synced:.*\n?", block, re.MULTILINE)
    return m.group(0) if m else None


def resolve_second_brain(base: str, ours: str, theirs: str, key: str) -> Optional[str]:
    b, o, t = extract_sb_key(base, key), extract_sb_key(ours, key), extract_sb_key(theirs, key)
    if o == t:
        return o
    if o != b and t == b:
        return o
    if t != b and o == b:
        return t
    if o != b and t != b:
        return o
    return o or t


def apply_d7_frontmatter(ours: str, base: str, theirs: str) -> str:
    """Return full file text: ours body, frontmatter with D7 keys resolved."""
    if not ours.startswith("---\n"):
        raise MergeConflict("no frontmatter")
    o_end = ours.find("\n---\n", 3)
    if o_end == -1:
        raise MergeConflict("no frontmatter end")
    block = ours[4:o_end]

    win = resolve_updated_line(base, ours, theirs)
    if win is not None:
        if _UPDATED_LINE_RE.search(block):
            block = _UPDATED_LINE_RE.sub(win, block, count=1)
        else:
            block = block.rstrip("\n") + "\n" + win + "\n"

    for key in ("second-brain-id", "second-brain-synced"):
        resolved = resolve_second_brain(base, ours, theirs, key)
        if key == "second-brain-id":
            pat = re.compile(r"^second-brain-id:.*(?:\n[ \t]+-.*)*\n?", re.MULTILINE)
        else:
            pat = re.compile(r"^second-brain-synced:.*\n?", re.MULTILINE)
        if resolved is None:
            block = pat.sub("", block)
        elif pat.search(block):
            rep = resolved if resolved.endswith("\n") else resolved + "\n"
            block = pat.sub(rep, block, count=1)
        else:
            block = block.rstrip("\n") + "\n" + (resolved if resolved.endswith("\n") else resolved + "\n")

    out = "---\n" + block
    if not out.endswith("\n"):
        out += "\n"
    out += "---\n" + ours[o_end + 5:]
    return out


def frontmatter_only_d7(hunk_lines: List[str]) -> bool:
    for line in hunk_lines:
        if CONFLICT_START.match(line) or CONFLICT_SEP.match(line) or CONFLICT_END.match(line):
            continue
        if not line.strip():
            continue
        if line.startswith("updated:"):
            continue
        if line.startswith("second-brain-id:") or line.startswith("second-brain-synced:"):
            continue
        if re.match(r"^[ \t]+-", line):
            continue
        return False
    return True


def hunk_regions(lines: List[str]) -> List[Tuple[int, int]]:
    regions = []
    i = 0
    while i < len(lines):
        if CONFLICT_START.match(lines[i]):
            j = i + 1
            while j < len(lines) and not CONFLICT_END.match(lines[j]):
                j += 1
            if j >= len(lines):
                raise MergeConflict("unclosed conflict hunk")
            regions.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    return regions


def hunk_relation(hunk: Tuple[int, int], start: int, end: int) -> str:
    hs, he = hunk
    if he <= start or hs >= end:
        return "outside"
    if hs >= start and he <= end:
        return "inside"
    return "straddle"


def fm_end_line(text: str) -> Optional[int]:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        return None
    return text[: end + 5].count("\n")


def living_bounds(lines: List[str]) -> Optional[Tuple[int, int]]:
    start = next((i for i, l in enumerate(lines) if l.startswith(LIVING_HISTORY_HEADING)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return start, end


# ---------------------------------------------------------------------------
# Living History keyed merge
# ---------------------------------------------------------------------------

def session_link(body: str) -> Optional[str]:
    for m in WIKILINK_RE.finditer(body):
        target = m.group(1).split("|", 1)[0].strip()
        if target.startswith("sessions/"):
            return target
    return None


def entry_key(line: str, date: str, siblings: List[str]) -> EntryKey:
    m = CHILD_RE.match(line)
    if not m:
        return ("exact", date, line)
    cat = (m.group("cat") or "").strip()
    body = m.group("body") or ""
    link = session_link(body)
    if link and cat:
        same = 0
        for sib in siblings:
            sm = CHILD_RE.match(sib)
            if not sm:
                continue
            if (sm.group("cat") or "").strip() == cat and session_link(sm.group("body") or "") == link:
                same += 1
        if same > 1:
            return ("exact", date, line)
        return ("session", date, cat, link)
    return ("exact", date, line)


def parse_living_body(body_lines: List[str]) -> Tuple[List[dict], List[dict]]:
    months: List[dict] = []
    opaques: List[dict] = []
    current: Optional[dict] = None
    current_date: Optional[dict] = None

    def close_date():
        nonlocal current_date
        if current is not None and current_date is not None:
            current["dates"].append(current_date)
        current_date = None

    def close_current():
        nonlocal current
        close_date()
        if current is not None:
            (opaques if current["opaque"] else months).append(current)
        current = None

    i = 0
    while i < len(body_lines):
        line = body_lines[i]
        if line.startswith("### "):
            close_current()
            m = MONTH_HEADING_RE.match(line)
            if m and m.group(1).lower() in MONTH_INDEX:
                key = _month_key(int(m.group(2)), MONTH_INDEX[m.group(1).lower()])
                current = {"heading": line, "key": key, "dates": [], "opaque": False}
            else:
                current = {"heading": line, "key": None, "dates": [], "opaque": True, "raw": [line]}
            current_date = None
            i += 1
            continue
        if current is not None and current.get("opaque"):
            current["raw"].append(line)
            i += 1
            continue
        if current is None:
            current = {"heading": None, "key": None, "dates": [], "opaque": False}
        bm = BLOCK_HEADER_RE.match(line)
        if bm:
            close_date()
            current_date = {"header": line, "date": bm.group(1), "entries": []}
            i += 1
            continue
        if current_date is not None and line.lstrip().startswith("- "):
            current_date["entries"].append(line)
            i += 1
            continue
        if line.strip() == "":
            i += 1
            continue
        if current_date is not None:
            current_date["entries"].append(line)
        i += 1
    close_current()
    return months, opaques


def index_entries(months: List[dict]) -> Dict[EntryKey, List[str]]:
    multi: Dict[EntryKey, List[str]] = {}
    for month in months:
        for d in month["dates"]:
            sibs = list(d["entries"])
            for line in d["entries"]:
                k = entry_key(line, d["date"], sibs)
                multi.setdefault(k, []).append(line)
    return multi


def date_map(months: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for month in months:
        for d in month["dates"]:
            out[d["date"]] = d
    return out


def month_map(months: List[dict]) -> Dict[Optional[Tuple[int, int]], dict]:
    return {m["key"]: m for m in months}


def extract_living_section(text: str) -> str:
    lines = text.splitlines()
    bounds = living_bounds(lines)
    if bounds is None:
        raise MergeConflict("Living History missing")
    return "\n".join(lines[bounds[0]:bounds[1]]) + "\n"



def finalize_result_multi(result_multi: Dict[EntryKey, List[str]]) -> Dict[EntryKey, List[str]]:
    """Identity hook so tests can simulate a dropped entry before fail-closed."""
    return result_multi


def keyed_merge_living(base_s: str, ours_s: str, theirs_s: str, mergefile_s: str) -> str:
    def body(section: str) -> List[str]:
        lines = section.splitlines()
        return lines[1:] if lines else []

    b_months, _ = parse_living_body(body(base_s))
    o_months, _ = parse_living_body(body(ours_s))
    t_months, _ = parse_living_body(body(theirs_s))

    mf_lines = mergefile_s.splitlines()
    mf_bounds = living_bounds(mf_lines)
    if mf_bounds is None:
        raise MergeConflict("Living History missing in merge-file result")
    mf_section_lines = mf_lines[mf_bounds[0]:mf_bounds[1]]
    _, mf_opaque = parse_living_body(mf_section_lines[1:])
    for op in mf_opaque:
        for line in op.get("raw") or []:
            if CONFLICT_START.match(line) or CONFLICT_SEP.match(line) or CONFLICT_END.match(line):
                raise MergeConflict("conflict in opaque Living History block")

    b_idx = index_entries(b_months)
    o_idx = index_entries(o_months)
    t_idx = index_entries(t_months)

    removed = set()
    for k in b_idx:
        in_o, in_t = k in o_idx, k in t_idx
        if not in_o or not in_t:
            removed.add(k)

    result_multi: Dict[EntryKey, List[str]] = {}
    for k in set(o_idx) | set(t_idx):
        if k in removed:
            o_lines, t_lines, b_lines = o_idx.get(k, []), t_idx.get(k, []), b_idx.get(k, [])
            if o_lines and o_lines != b_lines:
                raise MergeConflict(f"removed vs changed: {k}")
            if t_lines and t_lines != b_lines:
                raise MergeConflict(f"removed vs changed: {k}")
            continue
        o_lines = o_idx.get(k, [])
        t_lines = t_idx.get(k, [])
        b_lines = b_idx.get(k, [])
        if not o_lines:
            result_multi[k] = list(t_lines)
            continue
        if not t_lines:
            result_multi[k] = list(o_lines)
            continue
        if o_lines == t_lines:
            result_multi[k] = list(o_lines)
            continue
        o_changed = o_lines != b_lines
        t_changed = t_lines != b_lines
        if o_changed and t_changed and o_lines != t_lines:
            raise MergeConflict(f"both changed: {k}")
        if o_changed:
            result_multi[k] = list(o_lines)
        elif t_changed:
            result_multi[k] = list(t_lines)
        else:
            result_multi[k] = list(o_lines)

    result_multi = finalize_result_multi(result_multi)
    union_keys = (set(o_idx) | set(t_idx)) - removed
    if set(result_multi) != union_keys:
        raise MergeConflict(f"fail-closed key mismatch: {set(result_multi)} != {union_keys}")

    o_dates, t_dates, b_dates = date_map(o_months), date_map(t_months), date_map(b_months)
    o_m, t_m = month_map(o_months), month_map(t_months)

    entries_by_date: Dict[str, List[str]] = {}
    for k, lines in result_multi.items():
        date = k[1]
        entries_by_date.setdefault(date, []).extend(lines)

    def ordered_for_date(date: str) -> List[str]:
        wanted = list(entries_by_date.get(date, []))
        used = [False] * len(wanted)
        result_lines: List[str] = []

        def take(line: str) -> bool:
            for i, w in enumerate(wanted):
                if not used[i] and w == line:
                    used[i] = True
                    result_lines.append(w)
                    return True
            return False

        for line in (o_dates[date]["entries"] if date in o_dates else []):
            take(line)
        for line in (t_dates[date]["entries"] if date in t_dates else []):
            take(line)
        for i, w in enumerate(wanted):
            if not used[i]:
                result_lines.append(w)
        return result_lines

    for date in set(entries_by_date):
        got = Counter(ordered_for_date(date))
        exp = Counter(entries_by_date[date])
        if got != exp:
            raise MergeConflict(f"fail-closed mismatch on {date}")

    heading = LIVING_HISTORY_HEADING
    for src in (ours_s, theirs_s, base_s):
        ls = src.splitlines()
        if ls and ls[0].startswith(LIVING_HISTORY_HEADING):
            heading = ls[0]
            break

    def date_month_key(date: str) -> Tuple[int, int]:
        return _month_key(int(date[:4]), int(date[5:7]))

    dates_in_month: Dict[Tuple[int, int], List[str]] = {}
    for date in entries_by_date:
        if not ordered_for_date(date):
            continue
        dates_in_month.setdefault(date_month_key(date), []).append(date)

    out_lines: List[str] = [heading]
    for mk in sorted(dates_in_month.keys(), reverse=True):
        heading_line = None
        for src in (o_m, t_m):
            if mk in src and src[mk]["heading"]:
                heading_line = src[mk]["heading"]
                break
        if heading_line is None:
            heading_line = f"### {MONTH_NAMES[mk[1]]} {mk[0]}"
        out_lines.append(heading_line)
        for date in sorted(dates_in_month[mk], reverse=True):
            header = None
            for src in (o_dates, t_dates, b_dates):
                if date in src:
                    header = src[date]["header"]
                    break
            out_lines.append(header or f"- **{date}**")
            out_lines.extend(ordered_for_date(date))
        out_lines.append("")

    for op in mf_opaque:
        out_lines.extend(op.get("raw") or [])

    while out_lines and out_lines[-1] == "":
        out_lines.pop()
    return "\n".join(out_lines) + "\n"


def classify_timeline_hunks(merged: str) -> None:
    """Raise if any hunk is outside D7 frontmatter / Living History or straddles."""
    m_lines = merged.splitlines()
    hunks = hunk_regions(m_lines)
    fm_end = fm_end_line(merged)
    living = living_bounds(m_lines)
    for h in hunks:
        in_fm = False
        in_lh = False
        if fm_end is not None:
            rel = hunk_relation(h, 1, fm_end)
            if rel == "straddle":
                raise MergeConflict("hunk straddles frontmatter")
            if rel == "inside":
                if not frontmatter_only_d7(m_lines[h[0]:h[1]]):
                    raise MergeConflict("non-D7 frontmatter conflict")
                in_fm = True
        if living is not None and not in_fm:
            rel = hunk_relation(h, living[0], living[1])
            if rel == "straddle":
                raise MergeConflict("hunk straddles Living History")
            if rel == "inside":
                in_lh = True
            elif rel == "outside":
                raise MergeConflict("conflict outside Living History / D7")
        if not in_fm and not in_lh:
            raise MergeConflict("unhandled conflict region")


def resolve_timeline_conflict(merged: str, base: str, ours: str, theirs: str) -> str:
    classify_timeline_hunks(merged)

    # Mid section (between FM and Living History) must be clean in merge-file result
    m_fm = fm_end_line(merged)
    m_lines = merged.splitlines()
    living = living_bounds(m_lines)
    if m_fm is not None and living is not None:
        mid = "\n".join(m_lines[m_fm:living[0]])
        if "<<<<<<< " in mid:
            raise MergeConflict("conflict in milestone / mid section")

    with_d7 = apply_d7_frontmatter(ours, base, theirs)
    # If updated: resolution itself raised, propagate

    o_sec = extract_living_section(ours)
    t_sec = extract_living_section(theirs)
    b_sec = extract_living_section(base)
    m_sec = extract_living_section(merged)
    new_lh = keyed_merge_living(b_sec, o_sec, t_sec, merged)

    w_lines = with_d7.splitlines(keepends=True)
    w_bounds = living_bounds([l.rstrip("\n") for l in w_lines])
    if w_bounds is None:
        raise MergeConflict("Living History missing after D7")
    before = "".join(w_lines[: w_bounds[0]])
    after = "".join(w_lines[w_bounds[1]:])
    result = before + new_lh + ("" if new_lh.endswith("\n") else "\n") + after
    if "<<<<<<< " in result:
        raise MergeConflict("markers remain")
    return result


def resolve_note_frontmatter(merged: str, base: str, ours: str, theirs: str) -> str:
    m_lines = merged.splitlines()
    hunks = hunk_regions(m_lines)
    end = fm_end_line(merged)
    if end is None:
        raise MergeConflict("no frontmatter")
    for h in hunks:
        rel = hunk_relation(h, 1, end)
        if rel == "straddle":
            raise MergeConflict("straddle fm")
        if rel == "outside":
            raise MergeConflict("body conflict")
        if not frontmatter_only_d7(m_lines[h[0]:h[1]]):
            raise MergeConflict("non-D7 fm conflict")
    result = apply_d7_frontmatter(ours, base, theirs)
    if "<<<<<<< " in result:
        raise MergeConflict("markers remain")
    return result


if __name__ == "__main__":
    sys.exit(main())
