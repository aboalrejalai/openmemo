import json
import os
import time
import unittest
from unittest import mock

from tests.capture import ROOT  # noqa: F401
from memo_capture import privacy, settings

# Synthetic secrets, assembled at run time so no scanner mistakes them for real ones.
GH = "ghp_" + "A1b2C3d4" * 4 + "Zz9x"
OPENAI = "sk-" + "proj" + "x" * 30
ANTHROPIC = "sk-ant-" + "api03-" + "y" * 30
SLACK = "xoxb-" + "1234567890-abcdefghij"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxIn0" + ".c2lnbmF0dXJlX3Bhcn"
GOOGLE = "AIza" + "S" * 35
STRIPE = "sk_" + "live_" + "Q" * 24
SUPA = "sbp_" + "0123456789abcdef" * 2 + "01234567"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"


def r(text):
    return privacy.redact(text)


class RedactionTest(unittest.TestCase):
    def setUp(self):
        settings.load(refresh=True)
        privacy._compiled = None

    def test_vendor_patterns(self):
        for name, secret in [("github_pat", GH), ("openai_key", OPENAI), ("anthropic_key", ANTHROPIC),
                             ("slack_token", SLACK), ("jwt", JWT), ("google_api_key", GOOGLE),
                             ("stripe_key", STRIPE), ("supabase_key", SUPA), ("private_key_pem", PEM),
                             ("aws_access_key", "AKIAIOSFODNN7EXAMPLE")]:
            out = r(f"value {secret} end")
            self.assertIn(f"<redacted type='{name}'/>", out, name)
            self.assertNotIn(secret, out, name)

    def test_google_key_must_end_after_35_chars(self):
        self.assertEqual(r("AIza" + "S" * 36), "AIza" + "S" * 36)

    def test_aws_secret_only_when_keyed(self):
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        self.assertEqual(r(secret), secret)
        out = r(f"AWS_SECRET_ACCESS_KEY={secret}")
        self.assertEqual(out, "AWS_SECRET_ACCESS_KEY=<redacted type='aws_secret_key'/>")
        self.assertIn("<redacted type='aws_secret_key'/>", r(f'"SecretAccessKey": "{secret}"'))

    def test_generic_password_token_bearer(self):
        self.assertEqual(r("DB_PASSWORD=hunter2hunter2"), "DB_PASSWORD=<redacted type='generic_password'/>")
        self.assertIn("generic_token", r("HOSTINGER_API_TOKEN: abcdEFGH1234zz"))
        out = r('"Authorization": "Bearer fake0-token-abcd-efgh"')
        self.assertIn("<redacted type='bearer_token'/>", out)
        self.assertIn('"Authorization": "Bearer ', out)

    def test_near_misses_are_kept(self):
        for text in ["sk-short", "AKIA123", "password=short", "token=12345678", "ghp_tooShort"]:
            self.assertEqual(r(text), text)

    def test_idempotent(self):
        once = r(f"a {GH} b DB_PASSWORD=hunter2hunter2")
        self.assertEqual(r(once), once)

    def test_disabled_builtin_and_custom_pattern(self):
        with mock.patch.dict(os.environ, {"MEMO_REDACT_DISABLED_BUILTINS": "jwt",
                                          "MEMO_REDACT_CUSTOM_PATTERNS": json.dumps(
                                              [{"name": "acme", "pattern": r"ACME-\d{6}"},
                                               {"name": "bad", "pattern": "("}, {"name": "", "pattern": "x"}])}):
            settings.load(refresh=True)
            self.assertEqual(r(JWT), JWT)
            self.assertEqual(r("id ACME-123456"), "id <redacted type='acme'/>")
        settings.load(refresh=True)

    def test_disabled_redaction_still_redacts_logs(self):
        with mock.patch.dict(os.environ, {"MEMO_REDACT_ENABLED": "false"}):
            settings.load(refresh=True)
            self.assertEqual(r(GH), GH)
            self.assertNotIn(GH, privacy.redact_for_log(GH))
        settings.load(refresh=True)

    def test_oversize_fails_closed(self):
        self.assertEqual(r("x" * (privacy.MAX_REDACT_CHARS + 1)), privacy.OVERSIZE_MARKER)

    def test_linear_on_long_whitespace(self):
        start = time.time()
        r("password" + " " * 200_000 + "=")
        self.assertLess(time.time() - start, 2)

    def test_json_keyed_values(self):
        out = privacy.redact_json({"env": {"MY_SECRET": "abcdefgh12", "NAME": "abcdefgh12",
                                           "list": [GH]}, "api_key": "ab"})
        self.assertEqual(out["env"]["MY_SECRET"], "<redacted type='generic_token'/>")
        self.assertEqual(out["env"]["NAME"], "abcdefgh12")
        self.assertEqual(out["env"]["list"], ["<redacted type='github_pat'/>"])
        self.assertEqual(out["api_key"], "ab")  # too short to be a credential


class TagStripTest(unittest.TestCase):
    def test_balanced_blocks_removed(self):
        text = "keep <private>hide me</private> keep2 <system-reminder>x</system-reminder>."
        self.assertEqual(privacy.strip_tags(text), "keep  keep2 .")

    def test_nested_and_attributes(self):
        self.assertEqual(privacy.strip_tags('a<private x="1">b<private>c</private>d</private>e'), "ae")

    def test_unclosed_tag_kept(self):
        self.assertEqual(privacy.strip_tags("a <private> b"), "a <private> b")

    def test_other_tags_untouched(self):
        self.assertEqual(privacy.strip_tags("<div>x</div><privateer>y</privateer>"),
                         "<div>x</div><privateer>y</privateer>")

    def test_linear_on_many_opens(self):
        start = time.time()
        privacy.strip_tags("<private>" * 50_000)
        self.assertLess(time.time() - start, 2)

    def test_private_prompt(self):
        self.assertEqual(privacy.clean_prompt("<private>all of it</private>"), ("", True))
        self.assertEqual(privacy.clean_prompt("hi <private>x</private>"), ("hi", False))
        self.assertEqual(privacy.clean_prompt(""), ("", False))

    def test_task_notification_only(self):
        self.assertTrue(privacy.is_task_notification_only("<task-notification>done</task-notification>"))
        self.assertFalse(privacy.is_task_notification_only("hi <task-notification>x</task-notification>"))


class SizeTest(unittest.TestCase):
    def test_truncate_head_tail(self):
        text = "H" * 20_000 + "T" * 20_000
        out = privacy.truncate(text)
        self.assertTrue(out.startswith("H" * 9_600) and out.endswith("T" * 4_800))
        self.assertIn("<elided chars=", out)

    def test_images_removed(self):
        value = {"content": [{"type": "image", "source": {"type": "base64", "data": "QUJD" * 100}},
                             "see data:image/png;base64," + "A" * 200]}
        out = privacy.clean_json(value)
        self.assertNotIn("QUJD", out)
        self.assertIn("[image removed]", out)


if __name__ == "__main__":
    unittest.main()
