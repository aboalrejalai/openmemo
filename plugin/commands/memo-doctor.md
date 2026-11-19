---
name: memo-doctor
description: Run Memo doctor and fix any failed checks
---

Run this and fix every failed check it prints:

```bash
"${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo" doctor
```

For Cursor specifically: the plugin should be at `~/.cursor/plugins/local/memo` (symlink to the vault `plugin/`), and after install reload the Cursor window.
