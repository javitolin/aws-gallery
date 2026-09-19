"""Identity cookie verification.

A silent failure here means archive attribution becomes forgeable, which no
test elsewhere would catch.
"""
import hashlib
import hmac
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for package in ("media_processor", "api"):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "lambdas", package))
from loader import load  # noqa: E402

os.environ.setdefault("BUCKET", "test-bucket")
os.environ.setdefault("HMAC_PARAM", "/test/hmac")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

handler = load("api_handler", "api", "handler.py")
mutations = load("api_mutations", "api", "mutations.py")

SECRET = b"a-test-secret"


def cookie(email, expires, secret=SECRET):
    payload = f"{email}|{expires}"
    digest = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"gallery_user={payload}|{digest}"


def event(*cookies):
    return {"cookies": list(cookies)}


class Caller(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(handler, "_identity_secret", return_value=SECRET)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_cookie_yields_the_email(self):
        who = handler.caller(event(cookie("javi@example.com", int(time.time()) + 600)))
        self.assertEqual(who, "javi@example.com")

    def test_forged_signature_is_rejected(self):
        self.assertIsNone(
            handler.caller(event("gallery_user=attacker@evil.com|9999999999|deadbeef"))
        )

    def test_tampered_email_is_rejected(self):
        raw = cookie("javi@example.com", int(time.time()) + 600)
        tampered = raw.replace("javi@", "attacker@")
        self.assertIsNone(handler.caller(event(tampered)))

    def test_cookie_signed_with_another_secret_is_rejected(self):
        self.assertIsNone(
            handler.caller(event(cookie("javi@example.com", int(time.time()) + 600, b"wrong")))
        )

    def test_expired_cookie_is_rejected(self):
        self.assertIsNone(handler.caller(event(cookie("javi@example.com", int(time.time()) - 1))))

    def test_missing_and_malformed_cookies_are_rejected(self):
        self.assertIsNone(handler.caller(event()))
        self.assertIsNone(handler.caller(event("gallery_user=garbage")))
        self.assertIsNone(handler.caller(event("other=value")))


class TagSanitising(unittest.TestCase):
    def test_hebrew_category_survives(self):
        # S3 allows Unicode letters in tag values; an ASCII-only filter would
        # have mangled these into underscores.
        self.assertEqual(mutations.TAG_SAFE.sub("_", "טיול משפחתי"), "טיול משפחתי")

    def test_disallowed_characters_are_replaced(self):
        self.assertEqual(mutations.TAG_SAFE.sub("_", "a,b;c"), "a_b_c")


if __name__ == "__main__":
    unittest.main(verbosity=2)
