#!/usr/bin/env python3
"""
Regression suite for the Memo MCP server.

Every test below is tied to a defect that was reproduced against the previous
implementation. The suite runs against a throwaway copy of the vault (via
MEMO_VAULT) so it can never touch real memory.

Run: python3 scripts/test_mcp.py
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import shutil
import sqlite3
import datetime
import tempfile
import subprocess
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import oslock  # noqa: E402
SERVER = REPO / "scripts" / "mcp_server.py"
ENGINE = REPO / "scripts" / "memory_engine.py"
TODAY = datetime.date.today().isoformat()

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


class Client:
    """Minimal stdio JSON-RPC client that spawns the server as a subprocess."""

    def __init__(self, vault: Path):
        env = dict(os.environ, MEMO_VAULT=str(vault), MEMO_LOG_LEVEL="ERROR",
                   MEMO_HOME=str(vault.parent / "memo-home"),
                   MEMO_SYNC_INTERVAL_MINUTES="0")
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self._n = 0

    def send(self, method: str, params: dict | None = None, *, notify: bool = False):
        self._n += 1
        msg: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            msg["id"] = self._n
        return self.raw(msg, expect_reply=not notify)

    def raw(self, payload, *, expect_reply: bool = True):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        if not expect_reply:
            return None
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"server died. stderr:\n{self.proc.stderr.read()}")
        return json.loads(line)

    def call(self, tool: str, args: dict | None = None) -> dict:
        return self.send("tools/call", {"name": tool, "arguments": args or {}})

    def text(self, tool: str, args: dict | None = None) -> str:
        return self.call(tool, args)["result"]["content"][0]["text"]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def make_vault() -> Path:
    """Throwaway git-backed copy of the repo with synthetic test fixtures in /tmp."""
    tmp = Path(tempfile.mkdtemp(prefix="memo-test-"))
    vault = tmp / "vault"
    shutil.copytree(
        REPO, vault,
        ignore=shutil.ignore_patterns(".git", ".cache", ".obsidian", "__pycache__"),
    )
    # Seed synthetic fixtures inside the /tmp test vault only (not in the openmemo repo)
    (vault / "clients").mkdir(parents=True, exist_ok=True)
    (vault / "projects").mkdir(parents=True, exist_ok=True)
    (vault / "hackathons").mkdir(parents=True, exist_ok=True)
    (vault / "stack").mkdir(parents=True, exist_ok=True)
    (vault / "sessions").mkdir(parents=True, exist_ok=True)
    (vault / "clients" / "demo-org.md").write_text(
        "---\ntitle: Demo Org\ntype: client\nstatus: active\ntags:\n  - type/client\n  - status/active\n"
        "entity_id: demo-org\ncreated: 2026-11-16\nupdated: 2026-11-16\n---\n# Demo Org\n\nSynthetic org.\n",
        encoding="utf-8",
    )
    (vault / "clients" / "acme-corp.md").write_text(
        "---\ntitle: Acme Corp\ntype: client\nstatus: active\ntags:\n  - type/client\n  - status/active\n"
        "entity_id: acme-corp\ncreated: 2026-11-16\nupdated: 2026-11-16\n---\n# Acme Corp (شركة تجريبية)\n\n"
        "Arabic name خالد appears here for Arabic FTS coverage.\n\n"
        "## System Architecture & Deliverables\n\n1. Sample.\n\n## Verified Technical Stack\n\n- Example.\n",
        encoding="utf-8",
    )
    (vault / "clients" / "globex.md").write_text(
        "---\ntitle: Globex Studio\ntype: client\nstatus: active\ntags:\n  - type/client\n  - status/active\n"
        "entity_id: globex\ncreated: 2026-11-16\nupdated: 2026-11-16\n---\n# Globex Studio\n\n## Technical Notes\n\n- Demo note.\n",
        encoding="utf-8",
    )
    (vault / "projects" / "demo-project.md").write_text(
        "---\ntitle: Demo Project\ntype: project\nstatus: active\ntags:\n  - type/project\n  - status/active\n"
        "created: 2026-11-16\nupdated: 2026-11-16\n---\n# Demo Project\n\nSynthetic project.\n",
        encoding="utf-8",
    )
    (vault / "hackathons" / "demo-hackathon.md").write_text(
        "---\ntitle: Demo Hackathon\ntype: hackathon\nstatus: completed\ntags:\n  - type/hackathon\n  - status/completed\n"
        "created: 2026-11-16\nupdated: 2026-11-16\n---\n# Demo Hackathon\n\nPhrase \"Demo Hackathon\" for phrase search.\n",
        encoding="utf-8",
    )
    (vault / "stack" / "salla.md").write_text(
        "---\ntitle: Salla\ntype: stack\nstatus: active\ntags:\n  - type/stack\n  - status/active\n  - tech/salla\n---\n"
        "# Salla\n\nSalla Supabase multi-word search fixture.\n",
        encoding="utf-8",
    )
    (vault / "stack" / "supabase.md").write_text(
        "---\ntitle: Supabase\ntype: stack\nstatus: active\ntags:\n  - type/stack\n  - status/active\n  - tech/supabase\n---\n"
        "# Supabase\n\nSalla Supabase fixture.\n",
        encoding="utf-8",
    )
    (vault / "sessions" / "current.md").write_text(
        "---\ntitle: Active Session\ntype: session\nstatus: active\ntags:\n  - type/session\n  - status/active\n---\n"
        "# Active Session\n",
        encoding="utf-8",
    )
    (vault / "graph" / "entities.json").write_text(
        json.dumps({
            "$schema": "./schema.md",
            "version": "2.2.0",
            "entities": [
                {"id": "demo-org", "type": "Organization", "name": "Demo Org", "file": "clients/demo-org.md"},
                {"id": "acme-corp", "type": "Client", "name": "Acme Corp", "file": "clients/acme-corp.md"},
                {"id": "globex", "type": "Client", "name": "Globex Studio", "file": "clients/globex.md"},
                {"id": "demo-project", "type": "Project", "name": "Demo Project", "file": "projects/demo-project.md"},
            ],
            "relationships": [],
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.email", "test@memo.local"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.name", "Memo Test"], cwd=vault, check=True)
    # Hooks come only from this throwaway repo, never from a global hooksPath.
    (vault / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "config", "core.hooksPath", ".git/hooks"], cwd=vault, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=vault, check=True)
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=vault, check=True)
    return vault


def git_log(vault: Path) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%s"], cwd=vault,
                         capture_output=True, text=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def git_out(vault: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=vault, capture_output=True, text=True).stdout


def head(vault: Path) -> str:
    return git_out(vault, "rev-parse", "HEAD").strip()


def changed_since(vault: Path, rev: str) -> set[str]:
    return {l for l in git_out(vault, "diff", "--name-only", rev, "HEAD").splitlines() if l}


def subjects_since(vault: Path, rev: str) -> list[str]:
    return [l for l in git_out(vault, "log", "--format=%s", f"{rev}..HEAD").splitlines() if l]


def cli_env(vault: Path, log_level: str = "ERROR") -> dict:
    return dict(os.environ, MEMO_VAULT=str(vault), MEMO_LOG_LEVEL=log_level,
                MEMO_SYNC_INTERVAL_MINUTES="0")


def cli(vault: Path, *args: str, log_level: str = "ERROR") -> subprocess.CompletedProcess:
    """Run memory_engine.py against the throwaway vault."""
    return subprocess.run([sys.executable, str(ENGINE), *args], cwd=vault,
                          env=cli_env(vault, log_level), capture_output=True,
                          text=True, timeout=60)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run() -> int:
    vault = make_vault()
    print(f"Memo MCP regression suite\nvault: {vault}\n")
    c = Client(vault)

    try:
        # -- protocol -------------------------------------------------
        print("Protocol")
        init = c.send("initialize", {"protocolVersion": "2025-06-18",
                                     "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1"}})
        check("initialize handshake",
              init["result"]["serverInfo"]["name"] == "memo-mcp")
        check("protocol negotiation echoes a supported client version",
              init["result"]["protocolVersion"] == "2025-06-18",
              f"got {init['result'].get('protocolVersion')}")
        old = c.send("initialize", {"protocolVersion": "1999-01-01"})
        check("unsupported version falls back to latest",
              old["result"]["protocolVersion"] == "2025-06-18")
        check("notifications produce no reply",
              c.send("notifications/initialized", notify=True) is None)

        batch = c.raw([{"jsonrpc": "2.0", "id": 900, "method": "ping"},
                       {"jsonrpc": "2.0", "id": 901, "method": "tools/list"}])
        check("batch request returns an array of responses",
              isinstance(batch, list) and len(batch) == 2,
              f"got {type(batch).__name__}")

        # -- tool contract --------------------------------------------
        print("\nTool contract")
        tools = c.send("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        expected = {"memo_search", "memo_get_entity", "memo_get_timeline",
                    "memo_get_dossier", "memo_list_projects", "memo_list_files",
                    "memo_status", "memo_lint", "memo_query_props",
                    "memo_append_fact", "memo_append_section", "memo_create_note",
                    "memo_update_fact", "memo_add_entity",
                    "memo_session_timeline", "memo_get_observations", "memo_save_memory"}
        check("all seventeen tools are exposed", names == expected,
              f"missing {expected - names}; extra {names - expected}")
        check("every tool declares annotations",
              all("annotations" in t for t in tools))
        check("every tool declares an outputSchema",
              all("outputSchema" in t for t in tools))
        by_name = {t["name"]: t for t in tools}
        check("read tools are marked readOnlyHint",
              all(by_name[n]["annotations"]["readOnlyHint"]
                  for n in ("memo_search", "memo_get_dossier", "memo_status",
                            "memo_lint", "memo_query_props")))
        check("memo_update_fact is marked destructiveHint",
              by_name["memo_update_fact"]["annotations"]["destructiveHint"] is True)
        check("memo_append_fact is not marked destructive",
              by_name["memo_append_fact"]["annotations"]["destructiveHint"] is False)
        check("tool results carry structuredContent",
              "structuredContent" in c.call("memo_status")["result"])

        # -- property inventory ---------------------------------------
        print("\nQuery props (inventory without text query)")
        qp = c.call("memo_query_props", {"type": "client", "status": "active"})["result"]
        check("query_props returns active clients",
              qp["structuredContent"]["count"] > 0)
        check("query_props paths look like clients/",
              all(r["path"].startswith("clients/")
                  for r in qp["structuredContent"]["results"]))

        # -- BUG 1: multi-word search ---------------------------------
        print("\nSearch (bug: phrase-only quoting returned zero hits)")
        res = c.call("memo_search", {"query": "Salla Supabase"})["result"]
        sc = res["structuredContent"]
        check("multi-word query returns hits", sc["count"] > 0,
              "regression: non-adjacent terms found nothing")
        check("winning strategy is reported", sc["strategy"] not in ("", "none"))
        check("explicit phrase search is honoured",
              c.call("memo_search", {"query": '"Demo Hackathon"'})
              ["result"]["structuredContent"]["strategy"] == "phrase")
        for term in ("خالد", "تجريبية"):
            check(f"arabic query '{term}' returns hits",
                  c.call("memo_search", {"query": term})
                  ["result"]["structuredContent"]["count"] > 0)
        check("nonsense query returns zero without erroring",
              c.call("memo_search", {"query": "zzzqqxxnotreal"})
              ["result"]["structuredContent"]["count"] == 0)

        # -- BUG 3: path traversal ------------------------------------
        print("\nPath safety (bug: any .md on the machine was readable)")
        outside = vault.parent / "leak-test.md"
        outside.write_text("SECRET-OUTSIDE-VAULT\n")
        esc = c.call("memo_get_dossier", {"path": "../leak-test"})["result"]
        check("traversal is refused", esc["isError"] is True)
        check("traversal leaks no content",
              "SECRET-OUTSIDE-VAULT" not in esc["content"][0]["text"])
        check("absolute path is refused",
              c.call("memo_get_dossier", {"path": "/etc/hosts"})["result"]["isError"])
        check("legitimate dossier still reads",
              "# " in c.text("memo_get_dossier", {"path": "projects/demo-project"}))

        # -- BUG 2 & 5: timeline roundtrip and placement ---------------
        print("\nTimeline (bugs: appended facts unreadable, written past the template)")
        summary = "Regression probe: Memo timeline roundtrip verified"
        add = c.call("memo_append_fact", {
            "target": "timeline", "date": "2026-09-05",
            "category": "Infrastructure", "summary": summary,
            "link": "projects/demo-project"})["result"]
        check("append reports success", add["isError"] is False)

        events = c.call("memo_get_timeline")["result"]["structuredContent"]["events"]
        check("appended fact is readable back",
              any(summary in e["summary"] for e in events),
              "regression: write/read cycle broken")
        check("no timeline event has an empty summary",
              all(e["summary"].strip() for e in events))
        check("timeline events carry a category",
              sum(1 for e in events if e["category"]) > len(events) // 2)

        raw_tl = (vault / "timeline.md").read_text()
        check("fact lands inside Living History, not after the template",
              raw_tl.index(summary) < raw_tl.index("## Log Ingestion Template"))
        check("fact lands under its own month section",
              raw_tl.index("### September 2026") < raw_tl.index(summary))

        # -- dedup -----------------------------------------------------
        print("\nIdempotency")
        again = c.call("memo_append_fact", {
            "target": "timeline", "date": "2026-09-05",
            "category": "Infrastructure", "summary": summary})["result"]
        check("duplicate append is skipped",
              again["structuredContent"].get("duplicate") is True)
        check("duplicate is written only once",
              (vault / "timeline.md").read_text().count(summary) == 1)

        # -- BUG 4: stale index ---------------------------------------
        print("\nIndex hygiene (bug: deleted files stayed searchable)")
        doomed = vault / "zz-temp-probe.md"
        doomed.write_text("# Doomed\n\nuniquetokenzzz content here\n")
        check("new file becomes searchable",
              c.call("memo_search", {"query": "uniquetokenzzz"})
              ["result"]["structuredContent"]["count"] > 0)
        doomed.unlink()
        check("deleted file disappears from search",
              c.call("memo_search", {"query": "uniquetokenzzz"})
              ["result"]["structuredContent"]["count"] == 0,
              "regression: stale rows survive")
        db = sqlite3.connect(vault / ".cache" / "memory.db")
        rows = [r[0] for r in db.execute("SELECT path FROM docs_meta")]
        db.close()
        check("index has no rows for missing files",
              all((vault / r).exists() for r in rows))

        # -- near-duplicate gate ---------------------------------------
        print("\nSemantic dedup (rephrased facts must not slip through)")
        rephrased = c.call("memo_append_fact", {
            "target": "timeline", "date": "2026-09-05", "category": "Infrastructure",
            "summary": "Memo timeline roundtrip regression probe verified"})["result"]
        check("rephrased duplicate is held back",
              rephrased["structuredContent"].get("near_duplicate") is True,
              "regression: only byte-identical repeats are caught")
        check("held-back write reports its evidence",
              rephrased["structuredContent"]["candidates"][0]["similarity"] >= 0.6)
        check("held-back fact was not written",
              "Memo timeline roundtrip regression probe verified"
              not in (vault / "timeline.md").read_text())
        forced = c.call("memo_append_fact", {
            "target": "timeline", "date": "2026-09-05", "category": "Infrastructure",
            "summary": "Memo timeline roundtrip regression probe verified",
            "force": True})["result"]
        check("force=true overrides the near-duplicate gate",
              forced["structuredContent"].get("duplicate") is False
              and forced["isError"] is False)
        check("a genuinely distinct fact still writes without force",
              c.call("memo_append_fact", {
                  "target": "timeline", "date": "2026-09-05", "category": "Client Agreement",
                  "summary": "Probe Motors retainer set to 4,750 SAR per month"})
              ["result"]["structuredContent"].get("near_duplicate") is False)

        # -- append_section --------------------------------------------
        print("\nStructural append (adding content without a fragile anchor)")
        # Pin the copy's updated: to a fixed past value, so the bump check
        # below tests touch_updated and not whatever the real note holds.
        acme_note = vault / "clients" / "acme-corp.md"
        pinned, n_pinned = re.subn(r"\A(---\n(?:(?!---\n).*\n)*?)updated:[^\n]*\n",
                                   r"\1updated: 2020-01-01\n", acme_note.read_text(), count=1)
        check("test setup: the acme-corp.md copy has a frontmatter updated: to pin", n_pinned == 1)
        acme_note.write_text(pinned)
        subprocess.run(["git", "commit", "-qm", "test setup: pin acme_corp updated:", "--",
                        "clients/acme-corp.md"], cwd=vault, check=True)
        section = "System Architecture & Deliverables"
        sec = c.call("memo_append_section", {
            "path": "clients/acme-corp.md", "heading": section,
            "text": "- Probe deliverable: WhatsApp cart recovery"})["result"]
        check("append_section writes under the heading", sec["isError"] is False,
              sec["content"][0]["text"][:200])
        acme_corp = (vault / "clients" / "acme-corp.md").read_text()
        check("appended text is in the file",
              "Probe deliverable: WhatsApp cart recovery" in acme_corp)
        check("append landed inside its section, not at end of file",
              acme_corp.index("Probe deliverable")
              < acme_corp.index("## Verified Technical Stack"))
        check("repeat append is skipped as duplicate",
              c.call("memo_append_section", {
                  "path": "clients/acme-corp.md", "heading": section,
                  "text": "- Probe deliverable: WhatsApp cart recovery"})
              ["result"]["structuredContent"]["duplicate"] is True)
        bad_head = c.call("memo_append_section", {
            "path": "clients/acme-corp.md", "heading": "Totally Fictional Heading",
            "text": "- nope"})["result"]
        check("unknown heading is refused", bad_head["isError"] is True)
        check("refusal lists the real headings",
              "Available headings" in bad_head["content"][0]["text"])
        check("create_missing adds the section",
              c.call("memo_append_section", {
                  "path": "clients/acme-corp.md", "heading": "Probe Notes",
                  "text": "- created by regression suite",
                  "create_missing": True})["result"]["isError"] is False)

        # -- create_note -----------------------------------------------
        print("\nNote creation (new client/project onboarding)")
        made = c.call("memo_create_note", {
            "path": "clients/probe-motors.md", "title": "Probe Motors",
            "type": "client", "tags": ["tech/salla"]})["result"]
        check("create_note succeeds", made["isError"] is False,
              made["content"][0]["text"][:200])
        check("created file exists", (vault / "clients" / "probe-motors.md").exists())
        created_text = (vault / "clients" / "probe-motors.md").read_text()
        check("frontmatter carries the taxonomy tags",
              "type/client" in created_text and "status/active" in created_text
              and "tech/salla" in created_text)
        check("created note is lint-clean",
              made["structuredContent"]["lint_errors"] == [],
              str(made["structuredContent"]["lint_errors"]))
        check("created note is immediately searchable",
              c.call("memo_search", {"query": "Probe Motors"})
              ["result"]["structuredContent"]["count"] > 0)
        check("created note appears in query_props",
              any(r["path"] == "clients/probe-motors.md"
                  for r in c.call("memo_query_props", {"type": "client"})
                  ["result"]["structuredContent"]["results"]))
        check("overwriting an existing note is refused",
              c.call("memo_create_note", {
                  "path": "clients/probe-motors.md", "title": "Dup",
                  "type": "client"})["result"]["isError"])
        root_new = c.call("memo_create_note", {
            "path": "probe-root.md", "title": "Probe Root", "type": "core"})["result"]
        check("creating a new root file is refused", root_new["isError"] is True)
        check("root refusal explains the six-hub policy",
              "six hubs" in root_new["content"][0]["text"])
        check("folder/type mismatch is refused",
              c.call("memo_create_note", {
                  "path": "clients/wrong.md", "title": "Wrong",
                  "type": "project"})["result"]["isError"])
        check("tag outside the taxonomy is refused",
              c.call("memo_create_note", {
                  "path": "projects/probe-proj.md", "title": "Probe Proj",
                  "type": "project", "tags": ["tech/cobol"]})["result"]["isError"])
        check("unknown entity_id is refused",
              c.call("memo_create_note", {
                  "path": "projects/probe-proj2.md", "title": "Probe Proj 2",
                  "type": "project", "entity_id": "not-a-real-entity"})["result"]["isError"])

        # -- update safety ---------------------------------------------
        print("\nMutation safety")
        probe = vault / "zz-update-probe.md"
        probe.write_text("# Probe\n\nrate is 10%\nrate is 10%\n")
        dup = c.call("memo_update_fact", {"path": "zz-update-probe.md",
                                           "old_text": "rate is 10%",
                                           "new_text": "rate is 15%"})["result"]
        check("ambiguous edit is refused", dup["isError"] is True)
        check("refusal explains why",
              "occurs 2 times" in dup["content"][0]["text"])
        check("file untouched after refusal",
              probe.read_text().count("rate is 10%") == 2)

        uniq = c.call("memo_update_fact", {
            "path": "zz-update-probe.md",
            "old_text": "# Probe\n\nrate is 10%",
            "new_text": "# Probe\n\nrate is 15%"})["result"]
        check("unique edit succeeds", uniq["isError"] is False)
        check("edit actually applied", "rate is 15%" in probe.read_text())
        check("missing old_text is refused",
              c.call("memo_update_fact", {"path": "zz-update-probe.md",
                                           "old_text": "does not exist anywhere",
                                           "new_text": "x"})["result"]["isError"])

        # -- graph ------------------------------------------------------
        print("\nKnowledge graph")
        ok = c.call("memo_add_entity", {
            "entity": {"id": "zz-probe-client", "type": "Client",
                       "name": "Probe Client", "file": "clients/probe.md"},
            "relationships": [{"source": "demo-org", "relation": "SERVICES_CLIENT",
                               "target": "zz-probe-client", "notes": "test"}]})["result"]
        check("valid entity is added", ok["isError"] is False)
        check("duplicate entity id is refused",
              c.call("memo_add_entity", {"entity": {
                  "id": "zz-probe-client", "type": "Client",
                  "name": "Dup"}})["result"]["isError"])
        check("unknown entity type is refused",
              c.call("memo_add_entity", {"entity": {
                  "id": "zz-other", "type": "Wormhole",
                  "name": "Bad"}})["result"]["isError"])
        check("dangling relationship endpoint is refused",
              c.call("memo_add_entity", {"relationships": [{
                  "source": "demo-org", "relation": "SERVICES_CLIENT",
                  "target": "does-not-exist"}]})["result"]["isError"])
        check("added entity is retrievable",
              c.call("memo_get_entity", {"name": "zz-probe-client"})
              ["result"]["structuredContent"]["count"] == 1)

        # -- git safety net ---------------------------------------------
        print("\nGit safety net")
        subjects = git_log(vault)
        check("mutations produced memo commits",
              any(s.startswith("memo:") for s in subjects),
              f"log: {subjects[:5]}")
        check("a pre-mutation snapshot exists",
              any("pre-mutation snapshot" in s for s in subjects))

        # -- errors ------------------------------------------------------
        print("\nError handling")
        check("unknown tool returns invalid-params",
              c.send("tools/call", {"name": "nope", "arguments": {}})
              ["error"]["code"] == -32602)
        check("unknown prompt returns invalid-params, not method-not-found",
              c.send("prompts/get", {"name": "nope"})["error"]["code"] == -32602)
        check("unknown resource returns invalid-params",
              c.send("resources/read", {"uri": "memo://nope"})["error"]["code"] == -32602)
        check("empty search query is refused",
              c.call("memo_search", {"query": "  "})["result"]["isError"])
        check("bad append target is refused",
              c.call("memo_append_fact", {"target": "nowhere", "category": "x",
                                           "summary": "y"})["result"]["isError"])
        check("resources list is populated",
              len(c.send("resources/list")["result"]["resources"]) >= 5)
        resource_uris = {r["uri"] for r in c.send("resources/list")["result"]["resources"]}
        for uri in ("memo://core/decisions", "memo://core/stack",
                    "memo://core/home", "memo://core/vault-spec"):
            check(f"resource {uri} is exposed", uri in resource_uris)
        check("owner_context prompt resolves",
              len(c.send("prompts/get", {"name": "owner_context"})
                  ["result"]["messages"]) == 1)

        # -- FTS skip for historical meta ------------------------------
        print("\nFTS exclusion (historical meta must not pollute search)")
        comp = c.call("memo_search", {"query": "compensation", "limit": 20})["result"]
        paths = [r["path"] for r in comp["structuredContent"]["results"]]
        check("open-questions is not in FTS hits",
              "docs/_open-questions.md" not in paths and "_open-questions.md" not in paths)
        check("prompt-fill-gaps is not in FTS hits",
              "docs/_prompt-fill-gaps.md" not in paths and "_prompt-fill-gaps.md" not in paths)
        check("excluded meta still readable via dossier",
              "# " in c.text("memo_get_dossier", {"path": "docs/_open-questions"}))

        # -- one call, one event ----------------------------------------
        print("\nOne call, one event (bug: a line break forged a second entry)")
        tl_path = vault / "timeline.md"
        sess_path = vault / "sessions" / "current.md"
        tl_before = tl_path.read_text()
        n_before = len(c.call("memo_get_timeline")["result"]["structuredContent"]["events"])
        forged = c.call("memo_append_fact", {
            "target": "timeline", "date": "2026-09-07",
            "category": "Infra\r\nstructure",
            "summary": "Probe one-call guard holds\n- **2026-09-08**\n  - **Forged:** injected event",
            "force": True})["result"]
        check("multi-line timeline fact is accepted", forged["isError"] is False,
              forged["content"][0]["text"][:200])
        events = c.call("memo_get_timeline")["result"]["structuredContent"]["events"]
        tl_after = tl_path.read_text()
        check("multi-line call adds exactly one timeline event",
              len(events) == n_before + 1, f"{n_before} -> {len(events)}")
        check("no forged event or date block appears",
              not any(e["category"] == "Forged" for e in events)
              and tl_after.splitlines().count("- **2026-09-08**")
              == tl_before.splitlines().count("- **2026-09-08**"))
        check("line breaks collapse to spaces in the entry",
              "  - **Infra structure:** Probe one-call guard holds - **2026-09-08** - "
              "**Forged:** injected event" in tl_after)
        s_before = sess_path.read_text() if sess_path.exists() else ""
        sess = c.call("memo_append_fact", {
            "target": "session", "date": "2026-09-07", "category": "Probe",
            "summary": "session one-call probe\n- [2026-01-01] **Forged:** injected"})["result"]
        s_new = sess_path.read_text().splitlines()[len(s_before.splitlines()):]
        check("multi-line session note adds exactly one line",
              sess["isError"] is False and len(s_new) == 1, str(s_new))
        check("no forged session line", not any(l.startswith("- [2026-01-01]") for l in s_new))

        print("\nDate shape (bug: '20260905' and ISO week dates were accepted)")
        bad_dates = ["20260905", "2026-W36-6", "2026-9-5", "٢٠٢٦-٠٩-٠٥",
                     "2026-02-30", "2026-09-05T10:00"]
        for target in ("timeline", "session"):
            tl0, s0 = tl_path.read_text(), sess_path.read_text()
            refused = [d for d in bad_dates if c.call("memo_append_fact", {
                "target": target, "category": "Probe", "date": d,
                "summary": f"date shape probe {target} {d}"})["result"]["isError"]]
            check(f"{target}: every non-YYYY-MM-DD date is refused",
                  refused == bad_dates, f"accepted {sorted(set(bad_dates) - set(refused))}")
            check(f"{target}: refused dates write nothing",
                  tl_path.read_text() == tl0 and sess_path.read_text() == s0)

        # -- updated: bump -----------------------------------------------
        print("\n'updated:' follows append_section and update_fact")
        check("append_section bumped clients/acme-corp.md updated: from the pinned 2020-01-01",
              f"\nupdated: {TODAY}\n" in acme_note.read_text()
              and "updated: 2020-01-01" not in acme_note.read_text())
        upd = vault / "docs" / "zz-updated-probe.md"
        upd.write_text('---\ntitle: Updated Probe\ntype: core\nstatus: active\n'
                       'tags: [type/core, status/active]\nupdated: "2020-01-01" # keep me\n'
                       '---\n# Updated Probe\n\n## Notes\n\n- first\n\n---\n\n'
                       'updated: 1999-01-01\n')
        c.call("memo_append_section", {"path": "docs/zz-updated-probe.md",
                                        "heading": "Notes", "text": "- second"})
        txt = upd.read_text()
        check("append_section sets updated: to today, keeping quotes and comment",
              f'\nupdated: "{TODAY}" # keep me\n' in txt, txt[:220])
        check("a body 'updated:' line below a rule is untouched",
              "\n---\n\nupdated: 1999-01-01\n" in txt, txt[-120:])
        upd.write_text(txt.replace(f'updated: "{TODAY}" # keep me', "updated: 2020-01-01 # plain"))
        c.call("memo_update_fact", {"path": "docs/zz-updated-probe.md",
                                     "old_text": "- second", "new_text": "- second (fixed)"})
        txt = upd.read_text()
        check("update_fact sets updated: to today, keeping the comment",
              f"\nupdated: {TODAY} # plain\n" in txt and "- second (fixed)" in txt, txt[:220])
        empty_fm = vault / "docs" / "zz-empty-fm-probe.md"
        empty_fm.write_text("---\n---\n# Empty FM Probe\n\n## Notes\n\n- a\n\n---\n"
                            "updated: 1999-01-01\n---\n")
        c.call("memo_append_section", {"path": "docs/zz-empty-fm-probe.md",
                                        "heading": "Notes", "text": "- b"})
        txt = empty_fm.read_text()
        check("empty frontmatter: the body 'updated:' line is left alone",
              "updated: 1999-01-01" in txt and TODAY not in txt and "- b" in txt, txt)
        nokey = vault / "docs" / "zz-nokey-probe.md"
        nokey.write_text("---\ntitle: No Key Probe\n---\n# No Key Probe\n\n## Notes\n\n- a\n")
        c.call("memo_append_section", {"path": "docs/zz-nokey-probe.md",
                                        "heading": "Notes", "text": "- b"})
        check("a note without updated: does not gain the key",
              "updated:" not in nokey.read_text())

        # -- create_note path refusals -----------------------------------
        print("\nNote paths (absolute, '..', dot segments, instruction-file names)")
        refusals = {
            "/clients/zz-abs.md": "absolute path",
            "clients/../projects/zz-dotdot.md": "'..' segment",
            "../zz-escape.md": "'..' segment",
            "clients/./zz-dot.md": "dot segment",
            ".obsidian/zz-hidden.md": "dot segment",
            "clients/.zz-hidden.md": "dot segment",
            "stack/CLAUDE.md": "instruction-file name",
            "stack/claude": "instruction-file name",
            "docs/Gemini.MD": "instruction-file name",
            "projects/agents.md": "instruction-file name",
            "AGENTS.md": "instruction-file name",
        }
        for bad_path, reason in refusals.items():
            r = c.call("memo_create_note", {"path": bad_path, "title": "Probe Path",
                                             "type": "client"})["result"]
            check(f"create_note refuses {bad_path!r} ({reason})",
                  r["isError"] is True and reason in r["content"][0]["text"],
                  r["content"][0]["text"][:200])
        leaked = [str(p) for p in vault.parent.rglob("*zz-*") if re.search(
            r"zz-(abs|dotdot|escape|dot|hidden)", p.name)]
        leaked += [f"{d}/{p.name}" for d in ("stack", "docs", "projects")
                   for p in (vault / d).iterdir()
                   if p.name.lower() in ("claude.md", "gemini.md", "agents.md",
                                         "gemini.md.md")]
        check("refused note paths write nothing", leaked == [], str(leaked))
        check("root AGENTS.md was not created", not (vault / "AGENTS.md").exists())
        desc = by_name["memo_create_note"]["description"]
        check("create_note description lists the new refusals",
              all(s in desc for s in ("absolute path", "'..'", "dot segment",
                                      "CLAUDE.md", "GEMINI.md", "AGENTS.md")), desc)
        check("append_fact description states one call, one event and the date shape",
              "One call writes one event" in by_name["memo_append_fact"]["description"]
              and "YYYY-MM-DD" in by_name["memo_append_fact"]["description"])

        # -- templates and YAML -----------------------------------------
        print("\nTemplates and YAML (stamp never copied; title and aliases quoted)")
        tpl = vault / "_templates" / "client.md"
        original_tpl = tpl.read_bytes()
        planted = "00000000-0000-4000-8000-00zzstampprobe"
        stamp = ("second-brain-id:\n  - " + planted +
                 "\nsecond-brain-synced: October 1, 2026 - 1:00 AM GMT+3\n")
        variants = {
            "crlf": ("---\ntitle: \"{{title}}\"\ntype: client\n" + stamp + "---\n"
                     "# {{title}}\n\n## Probe Template Section\n\n-\n").replace("\n", "\r\n"),
            "empty-frontmatter": ("---\n---\n" + stamp +
                                  "# {{title}}\n\n## Probe Template Section\n\n-\n"),
        }
        try:
            for label, tpl_text in variants.items():
                tpl.write_bytes(tpl_text.encode("utf-8"))
                rel = f"clients/zz-stamp-{label}.md"
                r = c.call("memo_create_note", {"path": rel, "title": f"Stamp {label}",
                                                 "type": "client"})["result"]
                body = (vault / rel).read_text() if (vault / rel).exists() else ""
                check(f"{label} template: the note is built from it",
                      r["isError"] is False and "## Probe Template Section" in body
                      and r.get("structuredContent", {}).get("templated") is True,
                      r["content"][0]["text"][:200])
                check(f"{label} template: no second-brain key or planted id is copied",
                      "second-brain" not in body and planted not in body, body[:300])
        finally:
            tpl.write_bytes(original_tpl)

        raw_title = 'Probe "Quoted": Co #1\nsecond-line: injected'
        flat_title = 'Probe "Quoted": Co #1 second-line: injected'
        yq = c.call("memo_create_note", {
            "path": "projects/zz-yaml-probe.md", "title": raw_title, "type": "project",
            "aliases": ["A, B", "x: y", "[bracket]", "plain"]})["result"]
        ytxt = (vault / "projects" / "zz-yaml-probe.md").read_text()
        fm_lines = ytxt.split("\n---\n", 1)[0].splitlines()
        check("title is YAML-quoted on one line",
              "title: " + json.dumps(flat_title, ensure_ascii=False) in fm_lines, str(fm_lines))
        check("a line break in the title cannot inject a frontmatter key",
              not any(l.startswith("second-line:") for l in fm_lines))
        check("aliases are YAML-quoted",
              'aliases: ["A, B", "x: y", "[bracket]", "plain"]' in fm_lines, str(fm_lines))
        check("the heading carries the one-line title", f"\n# {flat_title}\n" in ytxt)
        sc = yq.get("structuredContent") or {}
        check("the quoted note is lint-clean",
              sc.get("lint_errors") == [] or (
                  yq.get("isError") is False and (vault / "projects" / "zz-yaml-probe.md").exists()
                  and "lint_errors" not in sc),
              str(sc.get("lint_errors")))
        esc_title = 'Ocelot said "hi" \\ ok'
        c.call("memo_create_note", {"path": "projects/zz-escape-probe.md",
                                     "title": esc_title, "type": "project"})
        hits = c.call("memo_search", {"query": "Ocelot", "limit": 10})["result"]
        hit_titles = {r["path"]: r["title"] for r in hits["structuredContent"]["results"]}
        check("search shows a quoted title unescaped ('\"' and '\\')",
              hit_titles.get("projects/zz-escape-probe.md") == esc_title, str(hit_titles))
        qp_titles = {r["path"]: r["title"] for r in c.call(
            "memo_query_props", {"type": "project", "limit": 200}
        )["result"]["structuredContent"]["results"]}
        check("query_props shows a quoted title unescaped",
              qp_titles.get("projects/zz-escape-probe.md") == esc_title
              and qp_titles.get("projects/zz-yaml-probe.md") == flat_title,
              str({k: v for k, v in qp_titles.items() if "zz-" in k}))

        # -- lint scope and taxonomy ------------------------------------
        print("\nLint scope and taxonomy")
        planted_files = [vault / "AGENTS.md", vault / "zz-root-lint-probe.md",
                         vault / "docs" / "agents.md"]
        for p in planted_files:
            p.write_text("# Probe without frontmatter\n")
        lint = c.call("memo_lint")["result"]["structuredContent"]
        named = {e["file"] for e in lint["errors"]}
        check("root CLAUDE.md, GEMINI.md and AGENTS.md are not linted as notes",
              not ({"CLAUDE.md", "GEMINI.md", "AGENTS.md"} & named),
              str(sorted(n for n in named if "/" not in n)))
        check("other root files are still linted", "zz-root-lint-probe.md" in named)
        check("instruction names below the root are still linted", "docs/agents.md" in named)
        for p in planted_files:
            p.unlink()
        new_tags = ["tech/nextjs", "tech/capacitor", "domain/construction",
                    "service/web-development"]
        nt = c.call("memo_create_note", {"path": "projects/zz-new-tags.md",
                                          "title": "New Tags Probe", "type": "project",
                                          "tags": new_tags})["result"]
        check("the four new tags are accepted", nt["isError"] is False,
              nt["content"][0]["text"][:200])
        check("a note with the new tags is lint-clean",
              nt.get("structuredContent", {}).get("lint_errors") == [],
              str(nt.get("structuredContent", {}).get("lint_errors")))
        linter = load_module(vault / "scripts" / "vault_lint.py", "zz_vault_lint")
        spec_sec = (vault / "docs" / "VAULT_SPEC.md").read_text().split(
            "## 3. Tag Taxonomy", 1)[1].split("\n## ", 1)[0]
        spec_tags = set()
        for m in re.finditer(r"^- `([a-z]+/)` (.+)$", spec_sec, re.M):
            for item in m.group(2).split("·"):
                word = re.sub(r"\(.*?\)", "", item).strip()
                if word:
                    spec_tags.add(m.group(1) + word)
        check("VAULT_SPEC section 3 lists exactly ALLOWED_TAGS",
              spec_tags == linter.ALLOWED_TAGS,
              f"spec-only {sorted(spec_tags - linter.ALLOWED_TAGS)}, "
              f"lint-only {sorted(linter.ALLOWED_TAGS - spec_tags)}")
        adder = load_module(vault / "scripts" / "add_frontmatter.py", "zz_add_frontmatter")
        check("add_frontmatter keeps the four new tags", set(new_tags) <= adder.ALLOWED_TAGS)
        check("add_frontmatter has no tag outside the lint taxonomy",
              adder.ALLOWED_TAGS <= linter.ALLOWED_TAGS,
              str(sorted(adder.ALLOWED_TAGS - linter.ALLOWED_TAGS)))

        # -- path-scoped git commits ------------------------------------
        print("\nPath-scoped git commits (bug: git add -A swept in unrelated edits)")
        decisions = vault / "decisions.md"
        decisions.write_text(decisions.read_text() + "\nUnrelated hand edit, keep uncommitted.\n")
        (vault / "docs" / "zz-unrelated-untracked.md").write_text("# Stray\n")
        (vault / "docs" / "zz-unrelated-staged.md").write_text("# Staged\n")
        subprocess.run(["git", "add", "--", "docs/zz-unrelated-staged.md"], cwd=vault, check=True)
        rev = head(vault)
        pc = c.call("memo_append_fact", {
            "target": "timeline", "date": "2026-09-09", "category": "Probe",
            "summary": "Quokka scoped commit sentinel"})["result"]
        check("append with a dirty tree succeeds", pc["isError"] is False,
              pc["content"][0]["text"][:200])
        check("an unrelated dirty file is not committed",
              changed_since(vault, rev) == {"timeline.md"}, str(changed_since(vault, rev)))
        check("the unrelated edit stays in the working tree",
              git_out(vault, "status", "--porcelain", "--", "decisions.md").startswith(" M"))
        check("an untracked file stays untracked",
              git_out(vault, "status", "--porcelain", "--untracked-files=all", "--",
                      "docs/zz-unrelated-untracked.md").startswith("??"))
        check("a pre-staged file stays staged and out of the commit",
              git_out(vault, "diff", "--cached", "--name-only").split() ==
              ["docs/zz-unrelated-staged.md"])
        check("a clean touched file gets one commit and no snapshot",
              subjects_since(vault, rev) ==
              ["memo: timeline 2026-09-09 — Probe: Quokka scoped commit sentinel"],
              str(subjects_since(vault, rev)))

        acme_corp_path = vault / "clients" / "acme-corp.md"
        acme_corp_path.write_text(acme_corp_path.read_text() + "\nHand edit before the memo write.\n")
        rev = head(vault)
        c.call("memo_append_section", {"path": "clients/acme-corp.md", "heading": "Probe Notes",
                                        "text": "- snapshot probe line"})
        check("a dirty touched file is snapshotted first, then committed",
              subjects_since(vault, rev) == [
                  "memo: append to clients/acme-corp.md § Probe Notes",
                  "memo: pre-mutation snapshot (memo_append_section)"],
              str(subjects_since(vault, rev)))
        check("snapshot and commit touch only that file",
              changed_since(vault, rev) == {"clients/acme-corp.md"}, str(changed_since(vault, rev)))
        snap = git_out(vault, "show", "HEAD~1:clients/acme-corp.md")
        check("the snapshot holds the hand edit and not the memo write",
              "Hand edit before the memo write." in snap and "snapshot probe line" not in snap)

        if (vault / "CLIENTS").exists():  # case-insensitive filesystem, as on this Mac
            rev = head(vault)
            mc = c.call("memo_append_section", {"path": "Clients/Acme-Corp.md",
                                                 "heading": "Probe Notes",
                                                 "text": "- mis-cased path probe"})["result"]
            check("a mis-cased path reports the on-disk path",
                  mc.get("structuredContent", {}).get("file") == "clients/acme-corp.md",
                  mc["content"][0]["text"][:200])
            check("a mis-cased append is committed under the on-disk path",
                  changed_since(vault, rev) == {"clients/acme-corp.md"}
                  and len(subjects_since(vault, rev)) == 1
                  and not git_out(vault, "status", "--porcelain", "--", "clients/acme-corp.md"))
            rev = head(vault)
            mu = c.call("memo_update_fact", {"path": "CLIENTS/acme-corp",
                                              "old_text": "- mis-cased path probe",
                                              "new_text": "- mis-cased path probe (fixed)"})
            check("a mis-cased update_fact is committed under the on-disk path",
                  mu["result"].get("structuredContent", {}).get("file") == "clients/acme-corp.md"
                  and changed_since(vault, rev) == {"clients/acme-corp.md"})
            mcn = c.call("memo_create_note", {"path": "Clients/zz-miscased.md",
                                               "title": "Miscased", "type": "project"})["result"]
            check("a mis-cased folder still gets its folder/type check",
                  mcn["isError"] is True and "requires type 'client'" in mcn["content"][0]["text"],
                  mcn["content"][0]["text"][:200])
        else:
            print("  SKIP  path-case checks (case-sensitive filesystem)")

        rev = head(vault)
        c.call("memo_add_entity", {"entity": {"id": "zz-probe-scope", "type": "Client",
                                               "name": "Probe Scope"}})
        check("add_entity commits only graph/entities.json",
              changed_since(vault, rev) == {"graph/entities.json"}, str(changed_since(vault, rev)))
        rev = head(vault)
        c.call("memo_create_note", {"path": "projects/zz-scope-probe.md",
                                     "title": "Scope Probe", "type": "project"})
        check("create_note commits only the new file",
              changed_since(vault, rev) == {"projects/zz-scope-probe.md"},
              str(changed_since(vault, rev)))

        # -- refused and no-op writes commit nothing --------------------
        print("\nRefused and no-op writes commit nothing "
              "(bug: a refused call snapshot-committed the user's hand edit)")

        def porcelain(rel: str) -> str:
            return git_out(vault, "status", "--porcelain", "--untracked-files=all", "--", rel)

        c.call("memo_append_fact", {"target": "session", "category": "Probe",
                                     "summary": "Session duplicate gate probe"})
        readme = vault / "README.md"
        globex = vault / "clients" / "globex.md"
        tpl_path = vault / "_templates" / "client.md"
        graph_path = vault / "graph" / "entities.json"
        hand_note = vault / "clients" / "zz-hand.md"
        originals = {p: p.read_bytes() for p in (readme, tpl_path, tl_path, sess_path)}
        try:
            readme.write_text(readme.read_text() + "\nHand edit in README, keep uncommitted.\n")
            globex.write_text(globex.read_text() + "\nGlobex hand edit.\nGlobex hand edit.\n")
            tpl_path.write_text(tpl_path.read_text() + "\nHand edit in the template.\n")
            hand_note.write_text("# Hand-made note, never committed by memo\n")
            graph_path.write_text(graph_path.read_text() + "\n")
            tl_path.write_text(tl_path.read_text() + "\nHand edit in the timeline.\n")
            sess_path.write_text(sess_path.read_text() + "Hand edit in the session.\n")
            globex_dirty = globex.read_bytes()
            refused_calls = [
                ("create_note at the root ('README')", "memo_create_note",
                 {"path": "README", "title": "Readme", "type": "core"}, True, "README.md"),
                ("create_note on an existing dirty note", "memo_create_note",
                 {"path": "clients/globex", "title": "Globex", "type": "client"}, True,
                 "clients/globex.md"),
                ("create_note on an existing untracked note", "memo_create_note",
                 {"path": "clients/zz-hand.md", "title": "Hand", "type": "client"}, True,
                 "clients/zz-hand.md"),
                ("create_note in _templates/", "memo_create_note",
                 {"path": "_templates/client", "title": "Tpl", "type": "client"}, True,
                 "_templates/client.md"),
                ("update_fact with old_text not found", "memo_update_fact",
                 {"path": "clients/globex.md", "old_text": "no such text zz",
                  "new_text": "x"}, True, "clients/globex.md"),
                ("update_fact with an ambiguous old_text", "memo_update_fact",
                 {"path": "clients/globex.md", "old_text": "Globex hand edit.",
                  "new_text": "x"}, True, "clients/globex.md"),
                ("update_fact with new_text identical to old_text", "memo_update_fact",
                 {"path": "clients/globex.md", "old_text": "## Technical Notes",
                  "new_text": "## Technical Notes"}, False, "clients/globex.md"),
                ("append_section under an unknown heading", "memo_append_section",
                 {"path": "clients/globex.md", "heading": "No Such Heading zz",
                  "text": "- nope"}, True, "clients/globex.md"),
                ("append_section with text already present", "memo_append_section",
                 {"path": "clients/globex.md", "heading": "Technical Notes",
                  "text": "Globex hand edit."}, False, "clients/globex.md"),
                ("add_entity with a duplicate id", "memo_add_entity",
                 {"entity": {"id": "zz-probe-client", "type": "Client", "name": "Dup"}}, True,
                 "graph/entities.json"),
                ("add_entity with nothing new", "memo_add_entity",
                 {"relationships": [{"source": "demo-org", "relation": "SERVICES_CLIENT",
                                     "target": "zz-probe-client", "notes": "test"}]}, False,
                 "graph/entities.json"),
                ("append_fact with an exact timeline duplicate", "memo_append_fact",
                 {"target": "timeline", "date": "2026-09-09", "category": "Probe",
                  "summary": "Quokka scoped commit sentinel"}, False, "timeline.md"),
                ("append_fact held back as a near-duplicate", "memo_append_fact",
                 {"target": "timeline", "date": "2026-09-09", "category": "Probe",
                  "summary": "Quokka scoped commit sentinel again"}, False, "timeline.md"),
                ("append_fact with a session duplicate", "memo_append_fact",
                 {"target": "session", "date": TODAY, "category": "Probe",
                  "summary": "Session duplicate gate probe"}, False, "sessions/current.md"),
            ]
            for label, tool, args, want_error, dirty_rel in refused_calls:
                rev = head(vault)
                dirty_before = porcelain(dirty_rel)
                r = c.call(tool, args)["result"]
                check(f"{label}: {'refused' if want_error else 'no-op'}, no commit, "
                      f"{dirty_rel} still dirty",
                      r["isError"] is want_error and head(vault) == rev
                      and bool(dirty_before) and porcelain(dirty_rel) == dirty_before,
                      f"isError={r['isError']} dirty_before={dirty_before!r} "
                      f"commits={subjects_since(vault, rev)} {r['content'][0]['text'][:160]}")
            same = c.call("memo_update_fact", {"path": "clients/globex.md",
                                                "old_text": "## Technical Notes",
                                                "new_text": "## Technical Notes"})["result"]
            check("an identical update_fact reports changed=false and writes nothing",
                  same["structuredContent"].get("changed") is False
                  and globex.read_bytes() == globex_dirty, same["content"][0]["text"][:200])
            tl_dirty = tl_path.read_text()
            tl_path.write_text(tl_dirty.replace("## Living History", "## Living Hist0ry", 1))
            rev = head(vault)
            nl = c.call("memo_append_fact", {"target": "timeline", "date": "2026-09-16",
                                              "category": "Probe", "force": True,
                                              "summary": "Capybara write refused late"})["result"]
            check("append_fact refused after the dedup gates (no Living History): no commit",
                  nl["isError"] is True and head(vault) == rev,
                  f"{subjects_since(vault, rev)} {nl['content'][0]['text'][:160]}")
            tl_path.write_text(tl_dirty)
            check("every hand edit is still uncommitted after the refusals",
                  all(porcelain(r).startswith(" M") for r in (
                      "README.md", "clients/globex.md", "_templates/client.md",
                      "graph/entities.json", "timeline.md", "sessions/current.md"))
                  and porcelain("clients/zz-hand.md").startswith("??"),
                  git_out(vault, "status", "--porcelain"))
            check("no refused call wrote to the dirty note", globex.read_bytes() == globex_dirty)

            rev = head(vault)
            c.call("memo_add_entity", {"entity": {"id": "zz-probe-snap", "type": "Client",
                                                   "name": "Probe Snap"}})
            check("a real add_entity on a dirty graph snapshots right before the write",
                  subjects_since(vault, rev) == ["memo: graph +zz-probe-snap",
                                                 "memo: pre-mutation snapshot (memo_add_entity)"]
                  and changed_since(vault, rev) == {"graph/entities.json"},
                  str(subjects_since(vault, rev)))
            rev = head(vault)
            c.call("memo_update_fact", {"path": "clients/globex.md",
                                         "old_text": "Globex hand edit.\nGlobex hand edit.\n",
                                         "new_text": "Globex hand edit.\n"})
            check("a real update_fact on a dirty note snapshots right before the write",
                  subjects_since(vault, rev) == ["memo: update clients/globex.md",
                                                 "memo: pre-mutation snapshot (memo_update_fact)"]
                  and changed_since(vault, rev) == {"clients/globex.md"},
                  str(subjects_since(vault, rev)))
        finally:
            for p, data in originals.items():
                p.write_bytes(data)
            hand_note.unlink(missing_ok=True)
        check("hand edits restored: README, template, timeline and session are clean again",
              not any(porcelain(r) for r in ("README.md", "_templates/client.md",
                                             "timeline.md", "sessions/current.md")),
              git_out(vault, "status", "--porcelain"))

        # -- CLI shares the gateway -------------------------------------
        print("\nCLI add-event shares the MCP gateway (lock, dedup gates, scoped commits)")
        rev = head(vault)
        cli_sum = "Platypus ledger reconciled for CLI gateway"
        first = cli(vault, "add-event", "--date", "2026-09-12", "--category", "Probe",
                    "--summary", cli_sum)
        check("CLI add-event writes", first.returncode == 0
              and "Appended to timeline.md" in first.stdout, first.stdout + first.stderr)
        check("CLI commit is path-scoped", changed_since(vault, rev) == {"timeline.md"},
              str(changed_since(vault, rev)))
        again = cli(vault, "add-event", "--date", "2026-09-12", "--category", "Probe",
                    "--summary", cli_sum)
        check("CLI skips an exact duplicate", "Skipped" in again.stdout, again.stdout)
        rephrase = "Platypus ledger reconciled again for CLI gateway"
        near = cli(vault, "add-event", "--date", "2026-09-12", "--category", "Probe",
                   "--summary", rephrase)
        check("CLI holds back a rephrased duplicate",
              "Held back" in near.stdout and rephrase not in tl_path.read_text(),
              near.stdout + near.stderr)
        forced_cli = cli(vault, "add-event", "--date", "2026-09-12", "--category", "Probe",
                         "--summary", rephrase, "--force")
        check("CLI --force writes past the near-duplicate gate",
              "Appended" in forced_cli.stdout and rephrase in tl_path.read_text(),
              forced_cli.stdout + forced_cli.stderr)
        bad_cli = cli(vault, "add-event", "--date", "20260913", "--category", "Probe",
                      "--summary", "CLI bad date probe")
        check("CLI refuses a non-YYYY-MM-DD date",
              bad_cli.returncode == 1 and "Expected YYYY-MM-DD" in bad_cli.stderr, bad_cli.stderr)
        lock_fh = open(vault / ".cache" / "memo.lock", "w")
        oslock.lock(lock_fh)
        try:
            waiter = subprocess.Popen(
                [sys.executable, str(ENGINE), "add-event", "--date", "2026-09-14",
                 "--category", "Probe", "--summary", "Axolotl waits for the vault lock"],
                cwd=vault, env=cli_env(vault), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            time.sleep(1.0)
            waited = waiter.poll() is None and "Axolotl" not in tl_path.read_text()
        finally:
            oslock.unlock(lock_fh)
            lock_fh.close()
        out, err = waiter.communicate(timeout=60)
        check("CLI add-event waits for the vault lock", waited)
        check("CLI add-event writes once the lock is free",
              waiter.returncode == 0 and "Axolotl waits" in tl_path.read_text(), out + err)

        # -- hooks ------------------------------------------------------
        print("\nGit hooks (bug: --no-verify skipped them; a failure must not lose a write)")
        hook = vault / ".git" / "hooks" / "pre-commit"
        marker = vault / ".git" / "zz-hook-ran"
        hook.write_text("#!/bin/sh\ntouch .git/zz-hook-ran\nexit 0\n")
        hook.chmod(0o755)
        rev = head(vault)
        c.call("memo_append_fact", {"target": "timeline", "date": "2026-09-10",
                                     "category": "Probe",
                                     "summary": "Wombat pre-commit hook observed"})
        check("git hooks run on memo commits", marker.exists())
        check("a passing hook still commits", len(subjects_since(vault, rev)) == 1,
              str(subjects_since(vault, rev)))
        hook.write_text("#!/bin/sh\nexit 1\n")
        rev = head(vault)
        rej_sum = "Numbat write survives a rejecting hook"
        rej = cli(vault, "add-event", "--date", "2026-09-11", "--category", "Probe",
                  "--summary", rej_sum, log_level="WARNING")
        check("a rejecting hook does not fail the write (CLI exit 0)", rej.returncode == 0,
              rej.stderr[-300:])
        check("the write stands after a hook rejection", rej_sum in tl_path.read_text())
        check("no commit is made when the hook rejects", head(vault) == rev)
        check("the warning names the exit code, the path and the hook",
              all(s in rej.stderr for s in ("exit 1", "timeline.md", "hook", "left staged")),
              rej.stderr[-400:])
        mrej = c.call("memo_append_fact", {"target": "session", "category": "Probe",
                                            "summary": "Session note survives a rejecting hook"})
        check("an MCP write still succeeds when the hook rejects",
              mrej["result"]["isError"] is False
              and "survives a rejecting hook" in sess_path.read_text())
        hook.unlink()
        rev = head(vault)
        c.call("memo_append_fact", {"target": "timeline", "date": "2026-09-15",
                                     "category": "Probe",
                                     "summary": "Echidna commit after hook removal"})
        check("after the hook is removed, the stranded write is snapshotted, then committed",
              subjects_since(vault, rev) == [
                  "memo: timeline 2026-09-15 — Probe: Echidna commit after hook removal",
                  "memo: pre-mutation snapshot (memo_append_fact)"]
              and rej_sum in git_out(vault, "show", "HEAD~1:timeline.md"),
              str(subjects_since(vault, rev)))

        # -- session memory (three-step retrieval) ------------------------
        print("\nSession memory")
        init = c.send("initialize", {"protocolVersion": "2025-06-18"})["result"]
        check("initialize explains the three-step flow",
              "memo_get_observations" in init.get("instructions", ""))
        saved = c.call("memo_save_memory", {"title": "Decided to keep Supabase for auth",
                                            "text": "Kept Supabase Auth because RLS is already in place.",
                                            "project": "probe-proj", "type": "decision",
                                            "facts": ["Supabase Auth stays"]})["result"]["structuredContent"]
        check("memo_save_memory writes a vault session note",
              saved["new"] and saved["markdown"].startswith("sessions/probe-proj/")
              and (vault / saved["markdown"]).exists(), str(saved))
        again = c.call("memo_save_memory", {"title": "Decided to keep Supabase for auth",
                                            "text": "same", "project": "probe-proj", "type": "decision",
                                            "facts": ["Supabase Auth stays"]})["result"]["structuredContent"]
        check("saving the same memory twice writes nothing new", not again["new"] and again["id"] == saved["id"])
        found = c.call("memo_search", {"query": "Supabase auth", "scope": "sessions",
                                       "project": "probe-proj"})["result"]
        sess = found["structuredContent"]["sessions"]
        check("memo_search returns the session index", any(h["id"] == saved["id"] for h in sess),
              str(sess)[:300])
        check("memo_search text points to steps 2 and 3",
              "memo_session_timeline" in found["content"][0]["text"])
        tl = c.call("memo_session_timeline", {"anchor": str(saved["id"])})["result"]["structuredContent"]
        check("memo_session_timeline finds the anchor", any(i["id"] == saved["id"] for i in tl["items"]))
        full = c.call("memo_get_observations", {"ids": [saved["id"]]})["result"]
        check("memo_get_observations returns facts and narrative",
              "Supabase Auth stays" in full["content"][0]["text"]
              and "RLS is already in place" in full["content"][0]["text"])
        arabic = c.call("memo_save_memory", {"title": "قررنا الإبقاء على سلة", "text": "متجر طوع نجد على سلة",
                                             "project": "probe-proj"})["result"]["structuredContent"]
        hits = c.call("memo_search", {"query": "سلة", "scope": "sessions"})["result"]["structuredContent"]
        check("Arabic search finds Arabic memory", any(h["id"] == arabic["id"] for h in hits["sessions"]))

        # -- stdout purity -----------------------------------------------
        print("\nTransport hygiene")
        check("server survived every request", c.proc.poll() is None)
    finally:
        c.close()
        shutil.rmtree(vault.parent, ignore_errors=True)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name, detail in FAILED:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
