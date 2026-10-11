# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The OAuth round trip with a sign-in provider.

``start`` sends the browser to the provider with a fresh ``state``; what
the callback will need (provider, nonce, PKCE verifier) waits in Redis
under that state. ``complete`` takes the provider's answer, trades the code
for tokens, verifies them and returns the identity. Nothing here touches
the user table: matching the identity to a user is the route's job.

Google and Microsoft are OpenID Connect: the ID token is verified against
the provider's published keys and the nonce stored at start. GitHub is
plain OAuth 2.0, so identity comes from its API, over the access token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog
from jose import JWTError, jwt  # type: ignore[import-untyped]

from api.db.redis import (
    SSO_LOGIN_CODE_KEY,
    SSO_LOGIN_CODE_TTL_SECONDS,
    SSO_STATE_KEY,
    SSO_STATE_TTL_SECONDS,
    RedisClient,
)
from api.models.auth import PROVIDER_LABELS, AuthProviderConfig, AuthProviderKind
from api.sso.identity import (
    Identity,
    SsoError,
    github_identity,
    google_identity,
    microsoft_identity,
)

logger = structlog.get_logger()

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
# Google documents both spellings of its issuer as valid in an ID token.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
MICROSOFT_DISCOVERY_URL = "https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_URL = "https://api.github.com"

SCOPES: dict[AuthProviderKind, str] = {
    AuthProviderKind.GOOGLE: "openid email profile",
    AuthProviderKind.MICROSOFT: "openid email profile",
    AuthProviderKind.GITHUB: "read:user user:email",
}

# Discovery documents and key sets change rarely; refetching them on every
# login would make the provider's availability part of every sign-in twice.
_METADATA_TTL_SECONDS = 3600
_HTTP_TIMEOUT_SECONDS = 15.0


@dataclass
class _Cached:
    expires_at: float
    value: dict[str, Any]


class SsoFlow:
    """One per process, on ``app.state``. Holds the HTTP client and the metadata cache."""

    def __init__(self, redis: RedisClient, http: httpx.AsyncClient | None = None) -> None:
        self._redis = redis
        self._http = http or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False)
        self._metadata: dict[str, _Cached] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # --- authorize ---------------------------------------------------------------------

    async def start(self, config: AuthProviderConfig, redirect_uri: str) -> tuple[str, str]:
        """Store the login's state and return the URL to send the browser to, and the state itself.

        The route also hands the state to the browser as a cookie, so the
        callback can tell that the browser coming back is the one that left.
        """
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        await self._redis.client.set(
            SSO_STATE_KEY.format(state=state),
            json.dumps({"provider": config.kind.value, "nonce": nonce, "verifier": verifier}),
            ex=SSO_STATE_TTL_SECONDS,
        )
        params: dict[str, str] = {
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES[config.kind],
            "state": state,
        }
        if config.kind == AuthProviderKind.GITHUB:
            authorize_url = GITHUB_AUTHORIZE_URL
            # GitHub: no ID token, so no nonce, and the client secret alone
            # binds the code to this install.
            params["allow_signup"] = "false"
        else:
            metadata = await self._metadata_for(config)
            authorize_url = str(metadata["authorization_endpoint"])
            params["nonce"] = nonce
            params["code_challenge"] = _s256(verifier)
            params["code_challenge_method"] = "S256"
            if config.kind == AuthProviderKind.GOOGLE:
                # Which account to use, every time: a person with several
                # Google accounts should not be signed in with whichever
                # the browser last used.
                params["prompt"] = "select_account"
        return f"{authorize_url}?{urlencode(params)}", state

    # --- callback ----------------------------------------------------------------------

    async def complete(
        self, config: AuthProviderConfig, redirect_uri: str, state: str, code: str
    ) -> Identity:
        """Finish a login the provider sent back. The state is consumed whether or not it works."""
        raw = await self._redis.client.getdel(SSO_STATE_KEY.format(state=state))
        if raw is None:
            raise SsoError(
                "This sign-in attempt has expired. Please try again.",
                "sso state missing or already used",
            )
        stored = json.loads(raw)
        if stored.get("provider") != config.kind.value:
            raise SsoError(
                "This sign-in attempt does not match the provider it came back from",
                f"state for {stored.get('provider')} returned via {config.kind.value}",
            )
        if config.kind == AuthProviderKind.GITHUB:
            return await self._complete_github(config, redirect_uri, code)
        return await self._complete_oidc(config, redirect_uri, code, stored["nonce"], stored["verifier"])

    async def _complete_oidc(
        self, config: AuthProviderConfig, redirect_uri: str, code: str, nonce: str, verifier: str
    ) -> Identity:
        metadata = await self._metadata_for(config)
        tokens = await self._post_token(
            str(metadata["token_endpoint"]),
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": verifier,
            },
        )
        label = PROVIDER_LABELS[config.kind]
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise SsoError(f"{label} did not return an identity token", "token response without id_token")
        jwks_uri = str(metadata["jwks_uri"])
        issuer = str(metadata["issuer"])
        try:
            jwks = await self._jwks_for(jwks_uri, jwt.get_unverified_header(id_token).get("kid"))
            claims: dict[str, Any] = jwt.decode(
                id_token,
                jwks,
                algorithms=["RS256"],
                audience=config.client_id,
                # Entra signs every tenant's tokens with the same keys, so a
                # token from another tenant passes the signature check and
                # differs only in its issuer. microsoft_identity checks that
                # and names the reason; a generic claims error here would hide it.
                issuer=GOOGLE_ISSUERS if config.kind == AuthProviderKind.GOOGLE else None,
                access_token=tokens.get("access_token"),
            )
        except JWTError as exc:
            raise SsoError(f"{label} returned an identity token that could not be verified", str(exc)) from exc
        if claims.get("nonce") != nonce:
            raise SsoError("This sign-in attempt could not be verified", "id token nonce mismatch")
        if config.kind == AuthProviderKind.GOOGLE:
            return google_identity(claims)
        return microsoft_identity(claims, issuer)

    async def _complete_github(self, config: AuthProviderConfig, redirect_uri: str, code: str) -> Identity:
        tokens = await self._post_token(
            GITHUB_TOKEN_URL,
            {
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
            },
        )
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str):
            raise SsoError("GitHub did not return an access token", f"github token response: {tokens}")
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
        user = await self._get_json(f"{GITHUB_API_URL}/user", headers)
        emails = await self._get_json(f"{GITHUB_API_URL}/user/emails", headers)
        if not isinstance(user, dict) or not isinstance(emails, list):
            raise SsoError("GitHub returned an unexpected answer", "github /user or /user/emails malformed")
        return github_identity(user, emails)

    # --- the hop back into the SPA ------------------------------------------------------

    async def issue_login_code(self, bearer_token: str) -> str:
        """Park a freshly minted bearer token behind a one-time code for the browser to redeem.

        The callback is a top-level navigation, which cannot write the SPA's
        storage; a code in the redirect URL can be traded for the token with
        one API call, and unlike the token itself it is useless a minute
        later or a second time.
        """
        code = secrets.token_urlsafe(32)
        await self._redis.client.set(
            SSO_LOGIN_CODE_KEY.format(code=code), bearer_token, ex=SSO_LOGIN_CODE_TTL_SECONDS
        )
        return code

    async def redeem_login_code(self, code: str) -> str | None:
        value = await self._redis.client.getdel(SSO_LOGIN_CODE_KEY.format(code=code))
        return str(value) if value is not None else None

    # --- http ---------------------------------------------------------------------------

    async def _metadata_for(self, config: AuthProviderConfig) -> dict[str, Any]:
        if config.kind == AuthProviderKind.GOOGLE:
            url = GOOGLE_DISCOVERY_URL
        else:
            url = MICROSOFT_DISCOVERY_URL.format(tenant=config.tenant_id)
        return await self._cached_json(url)

    async def _jwks_for(self, jwks_uri: str, kid: str | None) -> dict[str, Any]:
        """The provider's signing keys, refetched once when the token names a key the cache lacks.

        Providers rotate keys; a set cached an hour ago may predate the one
        that signed this token, and every sign-in would fail until the cache
        expired.
        """
        jwks = await self._cached_json(jwks_uri)
        if kid and not any(k.get("kid") == kid for k in jwks.get("keys", []) if isinstance(k, dict)):
            jwks = await self._cached_json(jwks_uri, refresh=True)
        return jwks

    async def _cached_json(self, url: str, refresh: bool = False) -> dict[str, Any]:
        cached = self._metadata.get(url)
        now = time.monotonic()
        if cached and cached.expires_at > now and not refresh:
            return cached.value
        value = await self._get_json(url, {})
        if not isinstance(value, dict):
            raise SsoError("The sign-in provider returned an unexpected answer", f"{url}: not a JSON object")
        self._metadata[url] = _Cached(now + _METADATA_TTL_SECONDS, value)
        return value

    async def _get_json(self, url: str, headers: dict[str, str]) -> Any:
        try:
            response = await self._http.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise SsoError("The sign-in provider could not be reached", f"GET {url}: {exc}") from exc
        except ValueError as exc:
            raise SsoError("The sign-in provider returned an unexpected answer", f"GET {url}: {exc}") from exc

    async def _post_token(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._http.post(url, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SsoError("The sign-in provider could not be reached", f"POST {url}: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise SsoError("The sign-in provider returned an unexpected answer", f"POST {url}: {exc}") from exc
        if response.status_code >= 400 or not isinstance(body, dict) or "error" in body:
            error = body.get("error") if isinstance(body, dict) else response.status_code
            raise SsoError(
                "The sign-in provider rejected this sign-in attempt",
                f"POST {url}: {response.status_code} {error}",
            )
        return body


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
