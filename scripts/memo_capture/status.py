"""`memo status`: is capture running, and what has it recorded?"""

from __future__ import annotations

import datetime
from typing import List

from . import settings, state, worker_client
from .paths import vault_dir


def _when(ms) -> str:
    return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M") if ms else "never"


def main(args: List[str]) -> int:
    from . import health as capture_health
    count, oldest = capture_health.spool_backlog()
    warn = capture_health.warning()
    try:
        conn = state.connect()
        conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()
    except Exception as exc:
        print("Memo capture status")
        print(f"  Index:        cannot open ({type(exc).__name__}: {exc})")
        print(f"  Spool:        {count} event(s) waiting" + (f", oldest {_when(oldest * 1000)}" if oldest else ""))
        if warn:
            print(f"  {warn}")
        sw = capture_health.sync_warning()
        if sw:
            print(f"  {sw}")
        return 1
    health = worker_client.request("/health", timeout=0.5)
    today = int(datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp() * 1000)
    last_obs = conn.execute("SELECT MAX(created_at) FROM observations").fetchone()[0]
    queue = state.queue_counts(conn)
    print("Memo capture status")
    print(f"  Vault:        {vault_dir()}")
    print(f"  Observer:     {settings.get('observer_model')} (effort {settings.get('observer_effort')})")
    print(f"  Worker:       {'running, pid ' + str(health['pid']) if health else 'not running'} "
          f"(port {settings.worker_port()})")
    print(f"  Spool:        {count} event(s) waiting" + (f", oldest {_when(oldest * 1000)}" if oldest else ""))
    print(f"  Queue:        {queue.get('pending', 0)} pending, {queue.get('processing', 0)} processing")
    print(f"  Last capture: {_when(last_obs)}")
    print(f"  Today:        "
          f"{conn.execute('SELECT COUNT(*) FROM sessions WHERE started_at >= ?', (today,)).fetchone()[0]} session(s), "
          f"{conn.execute('SELECT COUNT(*) FROM observations WHERE created_at >= ?', (today,)).fetchone()[0]} "
          f"observation(s)")
    rows = conn.execute("""SELECT project, COUNT(*) AS n, MAX(created_at) AS last FROM observations
                           GROUP BY project ORDER BY last DESC LIMIT 15""").fetchall()
    if rows:
        print("  Projects:")
        for r in rows:
            print(f"    {r['project']:<32} {r['n']:>5} observation(s), last {_when(r['last'])}")
    failed = conn.execute("SELECT last_error FROM pending_messages WHERE last_error IS NOT NULL "
                          "ORDER BY updated_at DESC LIMIT 1").fetchone()
    if failed:
        print(f"  Last retry:   {failed[0][:200]}")
    if warn:
        print(f"  {warn}")
    sw = capture_health.sync_warning()
    if sw:
        print(f"  {sw}")
    return 0
