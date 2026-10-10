# OIDC / Keycloak bearer-token validation for the JSON telemetry interface.
#
# In TOKEN mode the interface compares a static shared secret. In OIDC mode
# (COSMOS Enterprise with Keycloak) there is no shared secret: the posting
# service gets a short-lived JWT access token from Keycloak and sends it as
# "Authorization: Bearer <jwt>". This module turns that JWT into a yes/no
# decision by doing what an OAuth2 resource server does:
#
#   1. authentication  - verify the RS256 signature against the realm's public
#                        keys (JWKS), and check iss / aud / exp / nbf
#   2. authorization   - optionally require a realm role and/or an OAuth scope
#
# Verification is local and offline after the first JWKS fetch (PyJWKClient
# caches keys and refetches only when it sees an unknown `kid`, i.e. on key
# rotation), so there is no Keycloak round-trip per request.
#
# PyJWT and its crypto extra do the signature work:  PyJWT[crypto]  (see
# requirements.txt). The import is deferred to preflight()/the default verifier
# so this module - and the whole plugin - still imports in TOKEN mode and in the
# test suite without PyJWT installed. The crypto backend is injectable
# (`verifier=`) so the authorization logic can be unit tested without crypto.

from openc3.utilities.logger import Logger


class OidcValidator:
    def __init__(
        self,
        *,
        issuer,
        jwks_url,
        audience,
        required_role=None,
        required_scope=None,
        leeway=30,
        algorithms=("RS256",),
        verifier=None,
    ):
        self.issuer = issuer
        self.jwks_url = jwks_url
        self.audience = audience
        self.required_role = required_role
        self.required_scope = required_scope
        self.leeway = leeway
        self.algorithms = list(algorithms)
        # A custom verifier (used by tests) bypasses the PyJWT/JWKS backend.
        self._custom = verifier is not None
        self._verify = verifier or self._default_verifier
        self._jwks = None

    # Fail fast at connect() time if OIDC is configured but the crypto backend
    # isn't installed, instead of 401ing every request at runtime with a
    # confusing "verification failed".
    def preflight(self):
        if self._custom:
            return
        try:
            import jwt  # noqa: F401
            from jwt import PyJWKClient  # noqa: F401
        except ImportError as error:
            raise RuntimeError(
                "AUTH_MODE OIDC needs PyJWT with its crypto extra. Add "
                "'PyJWT[crypto]' to the plugin's requirements.txt (COSMOS installs "
                "it into the interface venv on load), or run "
                "`pip install 'PyJWT[crypto]'` for local testing."
            ) from error

    def _default_verifier(self, bearer):
        import jwt
        from jwt import PyJWKClient

        if self._jwks is None:
            self._jwks = PyJWKClient(self.jwks_url)
        signing_key = self._jwks.get_signing_key_from_jwt(bearer).key
        # jwt.decode raises on bad signature / wrong aud / wrong iss / expiry.
        return jwt.decode(
            bearer,
            signing_key,
            algorithms=self.algorithms,
            audience=self.audience,
            issuer=self.issuer,
            leeway=self.leeway,
            options={"require": ["exp", "iss", "aud"]},
        )

    # Returns the verified claims, or None if the token is not valid and
    # authorized. Never raises: an exception here would 500 instead of 401.
    def claims(self, bearer):
        try:
            claims = self._verify(bearer)
        except Exception as error:
            Logger.warn(f"OIDC token rejected: {error}")
            return None
        if not self._authorized(claims):
            return None
        return claims

    def valid(self, bearer):
        return self.claims(bearer) is not None

    # Authentication proved *who* the caller is; this proves they're *allowed*
    # to write telemetry. Without a required role/scope any valid realm token
    # would be accepted, so a role (or scope) is what actually gates ingest.
    def _authorized(self, claims):
        if self.required_role:
            roles = (claims.get("realm_access") or {}).get("roles") or []
            if self.required_role not in roles:
                Logger.warn(f"OIDC token missing required role '{self.required_role}'")
                return False
        if self.required_scope:
            scopes = (claims.get("scope") or "").split()
            if self.required_scope not in scopes:
                Logger.warn(f"OIDC token missing required scope '{self.required_scope}'")
                return False
        return True

    def details(self):
        return {
            "issuer": self.issuer,
            "jwks_url": self.jwks_url,
            "audience": self.audience,
            "required_role": self.required_role,
            "required_scope": self.required_scope,
            "leeway": self.leeway,
        }
