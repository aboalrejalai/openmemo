---
name: memo-status
description: Show Memo capture status (worker, queue, today's sessions)
---

Run this and report the result to the user:

```bash
"${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo" status
```

If the worker is down, start it with `memo start` and re-check.
