import base64
import unittest

import helpers  # noqa: F401  (sys.path)
from paseo_bridge import signature

BODY = b'{"type":"agent.turn_ended"}'
NOW = 1_800_000_000


class StandardWebhooksTest(unittest.TestCase):
    def headers(self, secret, msg_id="msg_1", t=NOW, body=BODY):
        return signature.signing_headers("standard-webhooks", secret, msg_id, body, timestamp=t)

    def test_raw_secret_roundtrip(self):
        h = self.headers("raw-secret-from-hermes-token-urlsafe")
        self.assertTrue(signature.verify("standard-webhooks", "raw-secret-from-hermes-token-urlsafe", h, BODY, now=NOW))

    def test_whsec_secret_uses_decoded_key(self):
        secret = "whsec_" + base64.b64encode(b"0123456789abcdef0123").decode()
        h = self.headers(secret)
        self.assertTrue(signature.verify("standard-webhooks", secret, h, BODY, now=NOW))
        # The same bytes as a raw secret must not verify: the key is the decoded value.
        self.assertFalse(signature.verify("standard-webhooks", secret[6:], h, BODY, now=NOW))

    def test_matches_known_vector(self):
        # From the Standard Webhooks spec test suite.
        secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
        msg_id, t = "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330
        body = b'{"test": 2432232314}'
        expected = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
        self.assertEqual(signature.standard_webhooks_signature(secret, msg_id, t, body), expected)

    def test_tampered_body_stale_and_wrong_secret_fail(self):
        h = self.headers("s3cret-s3cret-s3cret")
        self.assertFalse(signature.verify("standard-webhooks", "s3cret-s3cret-s3cret", h, BODY + b" ", now=NOW))
        self.assertFalse(signature.verify("standard-webhooks", "s3cret-s3cret-s3cret", h, BODY, now=NOW + 301))
        self.assertFalse(signature.verify("standard-webhooks", "other-secret-xxxxx", h, BODY, now=NOW))
        self.assertFalse(signature.verify("standard-webhooks", "", h, BODY, now=NOW))

    def test_one_of_several_signatures_may_match(self):
        h = self.headers("s3cret-s3cret-s3cret")
        h["webhook-signature"] = "v1,AAAA " + h["webhook-signature"]
        self.assertTrue(signature.verify("standard-webhooks", "s3cret-s3cret-s3cret", h, BODY, now=NOW))

    def test_headers_are_case_insensitive(self):
        h = {k.upper(): v for k, v in self.headers("s3cret-s3cret-s3cret").items()}
        self.assertTrue(signature.verify("standard-webhooks", "s3cret-s3cret-s3cret", h, BODY, now=NOW))

    def test_invalid_whsec_base64_is_refused(self):
        h = self.headers("whsec_abc-_def0123456789xyz")
        self.assertFalse(signature.verify("standard-webhooks", "whsec_abc-_def0123456789xyz", h, BODY, now=NOW))


class PaseoSchemeTest(unittest.TestCase):
    def test_roundtrip_and_failures(self):
        h = signature.signing_headers("paseo", "paseo-secret-123456", "d1", BODY, timestamp=NOW)
        self.assertTrue(signature.verify("paseo", "paseo-secret-123456", h, BODY, now=NOW))
        self.assertFalse(signature.verify("paseo", "paseo-secret-123456", h, BODY + b"x", now=NOW))
        self.assertFalse(signature.verify("paseo", "paseo-secret-123456", h, BODY, now=NOW - 400))

    def test_unknown_scheme(self):
        self.assertFalse(signature.verify("github", "x" * 20, {}, BODY, now=NOW))


class DeliveryIdTest(unittest.TestCase):
    def test_order(self):
        self.assertEqual(signature.delivery_id({"webhook-id": "a", "Paseo-Mcp-Delivery": "b"}, {"delivery_id": "c"}), "a")
        self.assertEqual(signature.delivery_id({"paseo-mcp-delivery": "b"}, {"delivery_id": "c"}), "b")
        self.assertEqual(signature.delivery_id({}, {"delivery_id": "c"}), "c")


if __name__ == "__main__":
    unittest.main()
