# OIDC / Keycloak auth tests for the JSON telemetry interface.
#
# Most tests inject a stub "verifier" into OidcValidator, so the authorization
# logic (roles, scopes, mode routing, error -> 401) is covered WITHOUT PyJWT or
# any crypto installed - they always run. One class (TestOidcRealCrypto) does a
# real RS256 sign/verify and auto-skips unless `PyJWT[crypto]` is importable
# (it is inside COSMOS, where requirements.txt installs it).
#
# Run the same way as the other tests:
#   cd cosmos/openc3/python
#   COSMOS_PLUGIN_DIR=/path/to/openc3-cosmos-json-tlm python -m pytest $COSMOS_PLUGIN_DIR/test -v

import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

PLUGIN_DIR = os.environ.get("COSMOS_PLUGIN_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.join(PLUGIN_DIR, "lib"))

from test.test_helper import mock_redis  # noqa: E402  (COSMOS repo test harness)

from openc3.system.system import System  # noqa: E402
from openc3.models.target_model import TargetModel  # noqa: E402
from openc3.utilities.logger import Logger  # noqa: E402

from json_oidc_auth import OidcValidator  # noqa: E402
from json_tlm_server_interface import JsonTlmServerInterface  # noqa: E402

ISSUER = "https://keycloak.example/auth/realms/openc3"
JWKS = f"{ISSUER}/protocol/openid-connect/certs"


def load_system():
    System.instance_obj = None
    TargetModel.clear_packet_cache()
    System.instance(["JSONDEMO"], os.path.join(PLUGIN_DIR, "targets"))


def stub_verifier(tokens):
    """Return a verifier that maps a bearer string to claims, raising for anything else
    (simulating a bad signature / expired / wrong-audience token)."""

    def verify(bearer):
        if bearer not in tokens:
            raise ValueError("invalid token")
        return tokens[bearer]

    return verify


class TestOidcValidator(unittest.TestCase):
    """Authorization logic on its own, with the crypto backend stubbed out."""

    def setUp(self):
        mock_redis(self)
        self.tokens = {
            "writer": {"realm_access": {"roles": ["tlm_writer", "other"]}, "scope": "openid telemetry"},
            "reader": {"realm_access": {"roles": ["tlm_reader"]}, "scope": "openid"},
            "noroles": {},
        }

    def validator(self, **kwargs):
        return OidcValidator(
            issuer=ISSUER, jwks_url=JWKS, audience="api", verifier=stub_verifier(self.tokens), **kwargs
        )

    def test_required_role_enforced(self):
        val = self.validator(required_role="tlm_writer")
        self.assertTrue(val.valid("writer"))
        self.assertFalse(val.valid("reader"))  # authenticated but wrong role
        self.assertFalse(val.valid("noroles"))

    def test_required_scope_enforced(self):
        val = self.validator(required_scope="telemetry")
        self.assertTrue(val.valid("writer"))
        self.assertFalse(val.valid("reader"))

    def test_role_and_scope_both_required(self):
        val = self.validator(required_role="tlm_writer", required_scope="telemetry")
        self.assertTrue(val.valid("writer"))
        # reader has neither
        self.assertFalse(val.valid("reader"))

    def test_no_requirement_accepts_any_valid_token(self):
        val = self.validator()
        self.assertTrue(val.valid("reader"))
        self.assertTrue(val.valid("noroles"))

    def test_bad_token_is_rejected_not_raised(self):
        val = self.validator(required_role="tlm_writer")
        self.assertFalse(val.valid("garbage"))  # verifier raises -> valid() returns False

    def test_claims_returns_payload_on_success(self):
        val = self.validator(required_role="tlm_writer")
        self.assertEqual(val.claims("writer")["realm_access"]["roles"], ["tlm_writer", "other"])
        self.assertIsNone(val.claims("reader"))

    def test_preflight_noop_with_injected_verifier(self):
        # A stubbed validator must not require PyJWT to be installed
        self.validator(required_role="tlm_writer").preflight()


class TestOidcHttp(unittest.TestCase):
    """The HTTP layer in OIDC mode: Bearer required, 401 on anything invalid,
    202 on a role-bearing token - all the way into the interface queue."""

    def setUp(self):
        mock_redis(self)
        load_system()
        Logger.stdout = False
        tokens = {"good": {"realm_access": {"roles": ["tlm_writer"]}}, "norole": {"realm_access": {"roles": ["nope"]}}}
        self.interface = JsonTlmServerInterface(0, "/json-tlm")  # port 0 = free port
        self.interface.name = "JSON_TLM_INT"
        self.interface.target_names = ["JSONDEMO"]
        self.interface.tlm_target_names = ["JSONDEMO"]
        self.interface.tlm_target_enabled = {"JSONDEMO": True}
        self.interface.set_option("AUTH_MODE", ["OIDC"])
        self.interface.set_option("OIDC_REQUIRED_ROLE", ["tlm_writer"])
        # Inject the stubbed validator so connect() doesn't need PyJWT/Keycloak
        self.interface.oidc = OidcValidator(
            issuer=ISSUER, jwks_url=JWKS, audience="api", required_role="tlm_writer", verifier=stub_verifier(tokens)
        )
        self.interface.connect()
        self.addCleanup(self.interface.disconnect)
        self.base = f"http://127.0.0.1:{self.interface.server.server_address[1]}/json-tlm"

    def request(self, path, body=None, bearer=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_valid_role_token_accepted(self):
        status, body = self.request("/tlm/JSONDEMO/STATUS", {"items": {"COUNTER": 7}}, bearer="good")
        self.assertEqual((status, body), (202, {"accepted": 1}))
        self.assertEqual(self.interface.request_queue.qsize(), 1)

    def test_missing_bearer_rejected(self):
        self.assertEqual(self.request("/tlm/JSONDEMO/STATUS", {"items": {"COUNTER": 1}})[0], 401)

    def test_token_without_role_rejected(self):
        self.assertEqual(self.request("/tlm/JSONDEMO/STATUS", {"items": {"COUNTER": 1}}, bearer="norole")[0], 401)

    def test_bad_token_rejected(self):
        self.assertEqual(self.request("/tlm/JSONDEMO/STATUS", {"items": {"COUNTER": 1}}, bearer="forged")[0], 401)

    def test_health_still_open(self):
        req = urllib.request.Request(self.base + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        self.assertEqual(body["status"], "ok")

    def test_details_report_oidc(self):
        details = self.interface.details()
        self.assertEqual(details["auth_mode"], "OIDC")
        self.assertTrue(details["authenticated"])
        self.assertEqual(details["oidc"]["required_role"], "tlm_writer")


try:
    import jwt as _jwt  # noqa: F401
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa  # noqa: F401

    HAVE_JWT = True
except ImportError:
    HAVE_JWT = False


@unittest.skipUnless(HAVE_JWT, "PyJWT[crypto] not installed (runs inside COSMOS or after `pip install 'PyJWT[crypto]'`)")
class TestOidcRealCrypto(unittest.TestCase):
    """End-to-end RS256: real PyJWT signature + iss/aud/exp verification, offline
    (we hand the public key straight to jwt.decode instead of fetching JWKS)."""

    def setUp(self):
        mock_redis(self)
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.jwt = jwt
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = self.key.public_key()

        def verifier(bearer):
            return jwt.decode(
                bearer,
                pub,
                algorithms=["RS256"],
                audience="api",
                issuer=ISSUER,
                leeway=30,
                options={"require": ["exp", "iss", "aud"]},
            )

        self.val = OidcValidator(
            issuer=ISSUER, jwks_url=JWKS, audience="api", required_role="tlm_writer", verifier=verifier
        )

    def mint(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": "api",
            "exp": now + 300,
            "iat": now,
            "realm_access": {"roles": ["tlm_writer"]},
        }
        claims.update(overrides)
        return self.jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    def test_good_token(self):
        self.assertTrue(self.val.valid(self.mint()))

    def test_expired_token(self):
        self.assertFalse(self.val.valid(self.mint(exp=int(time.time()) - 60)))

    def test_wrong_audience(self):
        self.assertFalse(self.val.valid(self.mint(aud="someone-else")))

    def test_wrong_issuer(self):
        self.assertFalse(self.val.valid(self.mint(iss="https://evil/realms/openc3")))

    def test_valid_signature_but_missing_role(self):
        self.assertFalse(self.val.valid(self.mint(realm_access={"roles": ["tlm_reader"]})))

    def test_tampered_token_fails_signature(self):
        token = self.mint()
        tampered = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
        self.assertFalse(self.val.valid(tampered))


if __name__ == "__main__":
    unittest.main()
