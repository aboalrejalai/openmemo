import unittest

from tests.capture import ROOT  # noqa: F401
from memo_capture import parser, prompts

TYPES, CONCEPTS = prompts.valid_types(), prompts.valid_concepts()

TWO = """Here you go:
```xml
<observation>
  <type>feature</type>
  <title>Added the Memo plugin hooks</title>
  <subtitle>Every Claude Code event now reaches the spool.</subtitle>
  <facts><fact>plugin/hooks/hooks.json registers 8 events</fact><fact>plugin/hooks/hooks.json registers 8 events</fact></facts>
  <narrative>The launcher runs the vault code &amp; exits 0.</narrative>
  <concepts><concept>how-it-works</concept><concept>feature</concept><concept>bogus</concept></concepts>
  <files_read><file>plugin/bin/memo</file></files_read>
  <files_modified><file>plugin/hooks/hooks.json</file></files_modified>
</observation>
<observation><type>weird</type><title>Decided to keep user-level MCP</title>
<narrative>Plugin MCP would rename tools.
</observation>
```"""


class ParserTest(unittest.TestCase):
    def test_two_observations_with_cleanup(self):
        r = parser.parse(TWO, TYPES, CONCEPTS)
        self.assertEqual(len(r.observations), 2)
        a, b = r.observations
        self.assertEqual(a.type, "feature")
        self.assertEqual(a.facts, ["plugin/hooks/hooks.json registers 8 events"])  # deduped
        self.assertEqual(a.narrative, "The launcher runs the vault code & exits 0.")
        self.assertEqual(a.concepts, ["how-it-works"])        # type and unknown removed
        self.assertEqual(a.files_modified, ["plugin/hooks/hooks.json"])
        self.assertEqual(b.type, "change")                     # unknown type falls back
        self.assertEqual(b.narrative, "Plugin MCP would rename tools.")  # unclosed tag tolerated

    def test_summary(self):
        r = parser.parse("<summary><request>Rename OldName</request><investigated>scripts/</investigated>"
                         "<learned>x</learned><completed>Phase 1</completed><next_steps>Phase 2</next_steps>"
                         "<notes></notes></summary>", TYPES, CONCEPTS)
        self.assertEqual((r.summary.request, r.summary.next_steps), ("Rename OldName", "Phase 2"))
        self.assertEqual(r.observations, [])

    def test_skip(self):
        r = parser.parse('<skip_summary reason="noise" />', TYPES, CONCEPTS)
        self.assertTrue(r.skipped)
        self.assertEqual(r.skip_reason, "noise")

    def test_placeholders_and_garbage_ignored(self):
        r = parser.parse("<observation><type>change</type><title>[short title: x]</title></observation> hi",
                         TYPES, CONCEPTS)
        self.assertEqual(r.observations, [])
        self.assertFalse(r.skipped)
        self.assertEqual(parser.parse("", TYPES).observations, [])

    def test_empty_summary_is_none(self):
        self.assertIsNone(parser.parse("<summary><request></request></summary>").summary)


class PromptTest(unittest.TestCase):
    def test_init_prompt_has_schema_rules_and_request(self):
        text = prompts.init_prompt("memo", "rename the vault", requested_at="2026-10-28")
        for needle in ("<observation>", "<files_modified>", "skip_summary", "bugfix", "gotcha",
                       "<user_request>rename the vault</user_request>", "MEMORY RECORDING START",
                       "redacted type"):
            self.assertIn(needle, text)
        self.assertNotIn("Claude-Mem", text)

    def test_language_switch(self):
        self.assertIn("بالعربية", prompts.init_prompt("memo", "x", language="ar"))

    def test_continuation_and_summary(self):
        cont = prompts.continuation_prompt("memo", "next", 3)
        self.assertIn("request #3", cont)
        self.assertNotIn("WHAT TO RECORD", cont)
        self.assertIn("WHAT TO RECORD", prompts.continuation_prompt("memo", "next", 3, include_guidance=True))
        s = prompts.summary_prompt("Done: renamed.")
        self.assertIn("<next_steps>", s)
        self.assertIn("Done: renamed.", s)
        self.assertIn("MODE SWITCH", s)

    def test_observation_prompt(self):
        text = prompts.observation_prompt("Bash", '{"command": "ls"}', '{"stdout": "a"}', "/p", 1_800_000_000_000)
        self.assertIn("<what_happened>Bash</what_happened>", text)
        self.assertIn("<working_directory>/p</working_directory>", text)


if __name__ == "__main__":
    unittest.main()
