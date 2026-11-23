#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json` used by the tests.

Replies with one <observation> per event in an event prompt, a <summary> for summary
prompts and "ok" otherwise. Writes its argv and MEMO_OBSERVER to $FAKE_LOG.
Set FAKE_FAIL=1 to answer every message with an error result.
"""
import json
import os
import sys
import uuid

log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "observer": os.environ.get("MEMO_OBSERVER")}) + "\n")
prompts_log = os.environ.get("FAKE_PROMPTS")
sid = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else str(uuid.uuid4())
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-opus-5-5"}), flush=True)
n = 0
for line in sys.stdin:
    n += 1
    text = json.loads(line)["message"]["content"]
    if prompts_log:
        with open(prompts_log, "a") as fh:
            fh.write(json.dumps(text) + "\n")
    if os.environ.get("FAKE_FAIL") == "1":
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": "usage limit reached", "session_id": sid}), flush=True)
        continue
    if "PROGRESS SUMMARY" in text:
        reply = ("<summary><request>Fake request</request><investigated>files</investigated>"
                 "<learned>how</learned><completed>done</completed><next_steps>more</next_steps>"
                 "<notes></notes></summary>")
    elif "<what_happened>" in text:
        tools = [part.split("</what_happened>")[0] for part in text.split("<what_happened>")[1:]]
        reply = "".join(
            f"<observation><type>discovery</type><title>Ran {tool} #{n}.{i}</title>"
            "<subtitle>Fake.</subtitle><facts><fact>fact one</fact></facts>"
            "<narrative>Because.</narrative><concepts><concept>how-it-works</concept></concepts>"
            "<files_read><file>src/a.py</file></files_read><files_modified></files_modified></observation>"
            for i, tool in enumerate(tools, 1))
    else:
        reply = "ok"
    print(json.dumps({"type": "assistant", "session_id": sid,
                      "message": {"role": "assistant", "model": "claude-opus-5-5",
                                  "content": [{"type": "text", "text": reply}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
                      "result": reply, "usage": {"input_tokens": 100, "output_tokens": 20}}), flush=True)
