"""Round-trips the CloudFront cookie signing against the public key.

If this breaks, either every viewer is locked out or the signature stops
meaning anything — neither fails loudly on its own.
"""
import base64
import json
import os
import sys
import unittest
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loader import load  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

os.environ.update(
    COGNITO_DOMAIN="example.auth.us-east-1.amazoncognito.com",
    CLIENT_ID="client",
    USER_POOL_ID="us-east-1_test",
    REGION="us-east-1",
    SITE_URL="https://photos.example.com",
    KEY_PAIR_ID="K123",
    PRIVATE_KEY_PARAM="/gallery/test",
    SESSION_TTL="3600",
)

handler = load("auth_handler", "auth", "handler.py")


class SignedCookies(unittest.TestCase):
    def setUp(self):
        handler._private_key = KEY

    def test_signature_verifies_against_the_public_key(self):
        cookies = handler._signed_cookies(2000000000)

        policy = base64.b64decode(
            cookies["CloudFront-Policy"].translate(str.maketrans("-_~", "+=/"))
        )
        signature = base64.b64decode(
            cookies["CloudFront-Signature"].translate(str.maketrans("-_~", "+=/"))
        )
        # Raises InvalidSignature if CloudFront would reject it.
        KEY.public_key().verify(signature, policy, padding.PKCS1v15(), hashes.SHA1())

    def test_policy_scopes_to_the_site_and_carries_the_expiry(self):
        cookies = handler._signed_cookies(1999999999)
        policy = json.loads(
            base64.b64decode(cookies["CloudFront-Policy"].translate(str.maketrans("-_~", "+=/")))
        )
        statement = policy["Statement"][0]

        self.assertEqual(statement["Resource"], "https://photos.example.com/*")
        self.assertEqual(
            statement["Condition"]["DateLessThan"]["AWS:EpochTime"], 1999999999
        )

    def test_encoding_avoids_characters_that_break_cookies(self):
        cookies = handler._signed_cookies(2000000000)
        for name in ("CloudFront-Policy", "CloudFront-Signature"):
            self.assertNotRegex(cookies[name], r"[+=/]", f"{name} kept a raw base64 char")

    def test_session_cookies_are_locked_down(self):
        cookie = handler._cookie("CloudFront-Policy", "value", 3600)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        # Host-only: no Domain attribute, so it cannot leak to sibling hosts.
        self.assertNotIn("Domain", cookie)


class Callback(unittest.TestCase):
    def test_state_mismatch_is_refused(self):
        response = handler._callback(
            {
                "queryStringParameters": {"code": "abc", "state": "attacker"},
                "cookies": ["state=genuine", "pkce=verifier"],
            }
        )
        self.assertEqual(response["statusCode"], 400)

    def test_missing_code_is_refused(self):
        response = handler._callback({"queryStringParameters": {}, "cookies": []})
        self.assertEqual(response["statusCode"], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
