#!/usr/bin/env python3
from pathlib import Path
html = Path("/tmp/authorize.html").read_text(encoding="utf-8")
if "`" in html:
    raise SystemExit("authorize.html must not contain backticks")
idp = Path("pkg/idp/idp.go")
text = idp.read_text(encoding="utf-8")
old = (
    'c.Data(http.StatusOK, "text/html; charset=utf-8", []byte(`'
    '<!doctype html><html><body><form method="post">'
    '<button type="submit">Authorize</button></form></body></html>`))'
)
new = 'c.Data(http.StatusOK, "text/html; charset=utf-8", []byte(`' + html + '`))'
if old not in text:
    raise SystemExit("authorize Data() line not found in idp.go")
idp.write_text(text.replace(old, new, 1), encoding="utf-8")
print("patched authorize HTML")
