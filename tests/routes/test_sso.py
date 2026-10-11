# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Sign-in through a provider: the admin's settings and the browser's round trip.

The provider itself is patched out at the flow's seam: ``start`` is real
(with the discovery document faked) and ``complete`` returns whatever
identity a test wants, so what is covered is everything on this side of
the provider: matching, linking, the one-time code, and the ways a sign-in
is refused.
"""

from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from starlette.testclient import TestClient

from api.models.auth import AuthProviderKind
from api.sso import Identity, SsoError, SsoFlow

PROVIDERS = "/api/v1/sso/providers"
PUBLIC = "/api/v1/auth/sso"

FAKE_GOOGLE_METADATA = {
    "issuer": "https://accounts.google.com",
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
}


@pytest.fixture
def offline_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(self: SsoFlow, url: str) -> dict[str, Any]:
        return FAKE_GOOGLE_METADATA

    monkeypatch.setattr(SsoFlow, "_cached_json", fake)


def _identity_from(monkeypatch: pytest.MonkeyPatch, identity: Identity | Exception) -> None:
    """Make every callback resolve to ``identity`` (or raise it), skipping the provider."""

    async def fake(self: SsoFlow, config: Any, redirect_uri: str, state: str, code: str) -> Identity:
        if isinstance(identity, Exception):
            raise identity
        return identity

    monkeypatch.setattr(SsoFlow, "complete", fake)


def _enable_google(client: TestClient) -> None:
    response = client.put(
        f"{PROVIDERS}/google", json={"enabled": True, "client_id": "cid", "client_secret": "sec"}
    )
    assert response.status_code == 200, response.text


def _disable_all(client: TestClient) -> None:
    for kind in AuthProviderKind:
        client.put(f"{PROVIDERS}/{kind.value}", json={"enabled": False, "client_id": "", "client_secret": ""})


def _start(client: TestClient, kind: str = "google") -> str:
    """Run the start leg and return the state the provider would echo back."""
    response = client.get(f"{PUBLIC}/{kind}/start", follow_redirects=False)
    assert response.status_code == 302, response.text
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def _callback(client: TestClient, state: str, kind: str = "google") -> str:
    response = client.get(f"{PUBLIC}/{kind}/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"]


def _error_of(location: str) -> str:
    return unquote(parse_qs(urlparse(location).query)["sso_error"][0])


class TestProviderSettings:
    def test_fresh_install_lists_every_provider_unconfigured(self, authenticated_client: TestClient) -> None:
        _disable_all(authenticated_client)
        body = authenticated_client.get(PROVIDERS).json()["providers"]
        assert [p["kind"] for p in body] == ["google", "microsoft", "github"]
        for p in body:
            assert p["enabled"] is False
            assert p["configured"] is False
            assert p["redirect_uri"].endswith(f"/api/v1/auth/sso/{p['kind']}/callback")
            assert "client_secret" not in p

    def test_save_keeps_the_secret_when_null(self, authenticated_client: TestClient) -> None:
        _enable_google(authenticated_client)
        saved = authenticated_client.put(
            f"{PROVIDERS}/google", json={"enabled": True, "client_id": "cid2", "client_secret": None}
        ).json()
        assert saved["client_id"] == "cid2"
        assert saved["has_client_secret"] is True
        assert saved["configured"] is True
        assert saved["updated_by"] == "testadmin"
        _disable_all(authenticated_client)

    def test_cannot_enable_without_a_client(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.put(f"{PROVIDERS}/github", json={"enabled": True, "client_id": "", "client_secret": ""})
        assert response.status_code == 400

    def test_microsoft_needs_one_tenant(self, authenticated_client: TestClient) -> None:
        body = {"enabled": True, "client_id": "cid", "client_secret": "sec", "tenant_id": "common"}
        response = authenticated_client.put(f"{PROVIDERS}/microsoft", json=body)
        assert response.status_code == 400
        assert "tenant" in response.json()["detail"]
        body["tenant_id"] = ""
        assert authenticated_client.put(f"{PROVIDERS}/microsoft", json=body).status_code == 400
        body["tenant_id"] = "11111111-2222-3333-4444-555555555555"
        assert authenticated_client.put(f"{PROVIDERS}/microsoft", json=body).status_code == 200
        _disable_all(authenticated_client)

    def test_unknown_kind(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get(f"{PUBLIC}/okta/start").status_code == 422
        assert authenticated_client.put(f"{PROVIDERS}/okta", json={"enabled": False}).status_code == 422

    def test_editors_cannot_read_providers(self, editor_client: TestClient) -> None:
        assert editor_client.get(PROVIDERS).status_code == 403

    def test_viewers_cannot_write_providers(self, viewer_client: TestClient) -> None:
        assert viewer_client.put(f"{PROVIDERS}/google", json={"enabled": False}).status_code == 403

    def test_public_list_shows_only_enabled(self, authenticated_client: TestClient) -> None:
        _disable_all(authenticated_client)
        assert authenticated_client.get(f"{PUBLIC}/providers").json()["providers"] == []
        _enable_google(authenticated_client)
        assert authenticated_client.get(f"{PUBLIC}/providers").json()["providers"] == [{"kind": "google", "label": "Google"}]
        _disable_all(authenticated_client)


class TestRoundTrip:
    def test_start_sends_the_browser_to_the_provider(self, authenticated_client: TestClient, offline_metadata: None) -> None:
        _enable_google(authenticated_client)
        response = authenticated_client.get(f"{PUBLIC}/google/start", follow_redirects=False)
        assert response.status_code == 302
        location = urlparse(response.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == FAKE_GOOGLE_METADATA["authorization_endpoint"]
        query = parse_qs(location.query)
        assert query["client_id"] == ["cid"]
        assert query["response_type"] == ["code"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"][0].endswith("/api/v1/auth/sso/google/callback")
        assert "state" in query and "nonce" in query
        _disable_all(authenticated_client)

    def test_github_start_needs_no_discovery(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.put(
            f"{PROVIDERS}/github", json={"enabled": True, "client_id": "gh", "client_secret": "s"}
        ).status_code == 200
        response = authenticated_client.get(f"{PUBLIC}/github/start", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"].startswith("https://github.com/login/oauth/authorize?")
        _disable_all(authenticated_client)

    def test_start_is_refused_when_not_enabled(self, authenticated_client: TestClient) -> None:
        """A top-level navigation, so the answer is the login page with a reason, not a JSON 404."""
        _disable_all(authenticated_client)
        response = authenticated_client.get(f"{PUBLIC}/google/start", follow_redirects=False)
        assert response.status_code == 303
        assert "not enabled" in _error_of(response.headers["location"])

    def test_matching_user_signs_in_and_is_linked(
        self, authenticated_client: TestClient, offline_metadata: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_google(authenticated_client)
        user = authenticated_client.post(
            "/api/v1/users/invite", json={"username": "jane", "email": "Jane@Example.com", "role": "editor"}
        ).json()["user"]
        _identity_from(monkeypatch, Identity(AuthProviderKind.GOOGLE, "g-123", "jane@example.com", "Jane"))

        location = _callback(authenticated_client, _start(authenticated_client))
        assert urlparse(location).path == "/sso/complete"
        code = parse_qs(urlparse(location).query)["code"][0]

        # The code is traded for a bearer token once.
        exchanged = authenticated_client.post(f"{PUBLIC}/exchange", json={"code": code})
        assert exchanged.status_code == 200, exchanged.text
        headers = {"Authorization": f"Bearer {exchanged.json()['access_token']}"}
        me = authenticated_client.get("/api/v1/users/me", headers=headers).json()
        assert me["username"] == "jane"
        assert me["role"] == "editor"
        assert me["last_login_at"] is not None
        assert authenticated_client.post(f"{PUBLIC}/exchange", json={"code": code}).status_code == 400

        # Linked: the admin and the user both see it.
        linked = authenticated_client.get(f"/api/v1/users/{user['id']}/identities").json()["identities"]
        assert [(i["provider"], i["email"]) for i in linked] == [("google", "jane@example.com")]
        assert authenticated_client.get("/api/v1/users/me/identities", headers=headers).json()["identities"] == linked

        # A second sign-in finds the link even if the email at the provider changed.
        _identity_from(monkeypatch, Identity(AuthProviderKind.GOOGLE, "g-123", "jane.doe@example.com"))
        location = _callback(authenticated_client, _start(authenticated_client))
        assert urlparse(location).path == "/sso/complete"
        linked = authenticated_client.get(f"/api/v1/users/{user['id']}/identities").json()["identities"]
        assert linked[0]["email"] == "jane.doe@example.com"
        assert linked[0]["last_login_at"] is not None

        # Unlinking, then signing in again, relinks by email.
        assert authenticated_client.delete(f"/api/v1/users/me/identities/{linked[0]['id']}", headers=headers).status_code == 204
        assert authenticated_client.get("/api/v1/users/me/identities", headers=headers).json()["identities"] == []
        _identity_from(monkeypatch, Identity(AuthProviderKind.GOOGLE, "g-999", "jane@example.com"))
        assert urlparse(_callback(authenticated_client, _start(authenticated_client))).path == "/sso/complete"

        authenticated_client.request("DELETE", f"/api/v1/users/{user['id']}")
        _disable_all(authenticated_client)

    def test_unknown_email_is_turned_away(
        self, authenticated_client: TestClient, offline_metadata: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_google(authenticated_client)
        _identity_from(monkeypatch, Identity(AuthProviderKind.GOOGLE, "g-1", "stranger@example.com"))
        message = _error_of(_callback(authenticated_client, _start(authenticated_client)))
        assert "stranger@example.com" in message
        assert "admin" in message
        _disable_all(authenticated_client)

    def test_disabled_user_is_turned_away(
        self, authenticated_client: TestClient, offline_metadata: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_google(authenticated_client)
        user = authenticated_client.post(
            "/api/v1/users/invite", json={"username": "gone", "email": "gone@example.com", "role": "viewer"}
        ).json()["user"]
        authenticated_client.patch(f"/api/v1/users/{user['id']}", json={"is_active": False})
        _identity_from(monkeypatch, Identity(AuthProviderKind.GOOGLE, "g-2", "gone@example.com"))
        assert "disabled" in _error_of(_callback(authenticated_client, _start(authenticated_client)))
        authenticated_client.request("DELETE", f"/api/v1/users/{user['id']}")
        _disable_all(authenticated_client)

    def test_second_account_at_the_same_provider_is_refused(
        self, authenticated_client: TestClient, offline_metadata: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_google(authenticated_client)
        user = authenticated_client.post(
            "/api/v1/users/invite", json={"username": "two", "email": "two@example.com", "role": "viewer"}
        ).json()["user"]
        _identity_from(monkeypatch, Identity(AuthProviderKind.GOOGLE, "g-first", "two@example.com"))
        assert urlparse(_callback(authenticated_client, _start(authenticated_client))).path == "/sso/complete"
        _identity_from(monkeypatch, Identity(AuthProviderKind.GOOGLE, "g-second", "two@example.com"))
        assert "already linked" in _error_of(_callback(authenticated_client, _start(authenticated_client)))
        authenticated_client.request("DELETE", f"/api/v1/users/{user['id']}")
        _disable_all(authenticated_client)

    def test_provider_errors_land_on_the_login_page(
        self, authenticated_client: TestClient, offline_metadata: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_google(authenticated_client)
        # The person cancelled at the provider.
        response = authenticated_client.get(
            f"{PUBLIC}/google/callback", params={"error": "access_denied", "state": "x"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "cancelled" in _error_of(response.headers["location"])
        # The provider's answer could not be verified.
        _identity_from(monkeypatch, SsoError("Google returned an identity token that could not be verified", "bad sig"))
        assert "could not be verified" in _error_of(_callback(authenticated_client, _start(authenticated_client)))
        # Nothing to go on at all.
        response = authenticated_client.get(f"{PUBLIC}/google/callback", follow_redirects=False)
        assert response.status_code == 303 and "sso_error" in response.headers["location"]
        _disable_all(authenticated_client)

    def test_state_is_single_use_and_checked(
        self, authenticated_client: TestClient, offline_metadata: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real ``complete`` up to the point it would talk to the provider."""
        _enable_google(authenticated_client)
        state = _start(authenticated_client)

        async def no_network(self: SsoFlow, *args: Any, **kwargs: Any) -> None:
            raise SsoError("The sign-in provider could not be reached", "test: no network")

        monkeypatch.setattr(SsoFlow, "_post_token", no_network)
        # First use gets as far as the provider; the state is consumed either way.
        assert "could not be reached" in _error_of(_callback(authenticated_client, state))
        assert "expired" in _error_of(_callback(authenticated_client, state))
        assert "expired" in _error_of(_callback(authenticated_client, "never-issued"))
        _disable_all(authenticated_client)

    def test_callback_must_come_from_the_browser_that_started(
        self, authenticated_client: TestClient, offline_metadata: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine state handed to another browser (login CSRF) is refused: the state cookie is missing."""
        _enable_google(authenticated_client)
        _identity_from(monkeypatch, Identity(AuthProviderKind.GOOGLE, "g-csrf", "someone@example.com"))
        state = _start(authenticated_client)
        authenticated_client.cookies.clear()
        assert "did not start in this browser" in _error_of(_callback(authenticated_client, state))
        # With the cookie back, the same state gets through to user matching: the cookie is the gate.
        authenticated_client.cookies.set("octoprox_sso_state", state)
        assert "No Octoprox account" in _error_of(_callback(authenticated_client, state))
        _disable_all(authenticated_client)

    def test_exchange_rejects_garbage(self, async_client: TestClient) -> None:
        assert async_client.post(f"{PUBLIC}/exchange", json={"code": "nope"}).status_code == 400
        assert async_client.post(f"{PUBLIC}/exchange", json={"code": ""}).status_code == 422
