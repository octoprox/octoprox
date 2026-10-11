# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The rule behind every sign-in: only an email the provider vouches for counts."""

import pytest

from api.models.auth import AuthProviderKind
from api.sso.identity import SsoError, github_identity, google_identity, microsoft_identity

ENTRA_ISSUER = "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0"


class TestGoogle:
    def test_verified_email_is_lowercased(self) -> None:
        identity = google_identity({"sub": "123", "email": "Jane@Example.com", "email_verified": True, "name": "Jane"})
        assert identity.provider == AuthProviderKind.GOOGLE
        assert identity.subject == "123"
        assert identity.email == "jane@example.com"
        assert identity.name == "Jane"

    def test_unverified_email_is_refused(self) -> None:
        with pytest.raises(SsoError, match="verified"):
            google_identity({"sub": "123", "email": "jane@example.com", "email_verified": False})

    def test_missing_email_is_refused(self) -> None:
        with pytest.raises(SsoError):
            google_identity({"sub": "123", "email_verified": True})

    def test_missing_subject_is_refused(self) -> None:
        with pytest.raises(SsoError):
            google_identity({"email": "jane@example.com", "email_verified": True})


class TestMicrosoft:
    def test_tenant_member_with_email(self) -> None:
        identity = microsoft_identity({"sub": "abc", "iss": ENTRA_ISSUER, "email": "Jane@Contoso.com"}, ENTRA_ISSUER)
        assert identity.provider == AuthProviderKind.MICROSOFT
        assert identity.email == "jane@contoso.com"

    def test_user_principal_name_stands_in_for_a_missing_email(self) -> None:
        identity = microsoft_identity({"sub": "abc", "iss": ENTRA_ISSUER, "preferred_username": "jane@contoso.com"}, ENTRA_ISSUER)
        assert identity.email == "jane@contoso.com"

    def test_other_tenant_is_refused(self) -> None:
        other = "https://login.microsoftonline.com/99999999-0000-0000-0000-000000000000/v2.0"
        with pytest.raises(SsoError, match="tenant"):
            microsoft_identity({"sub": "abc", "iss": other, "email": "jane@contoso.com"}, ENTRA_ISSUER)

    def test_no_address_at_all_is_refused(self) -> None:
        with pytest.raises(SsoError):
            microsoft_identity({"sub": "abc", "iss": ENTRA_ISSUER, "preferred_username": "jane"}, ENTRA_ISSUER)


class TestGitHub:
    def test_primary_verified_email_wins(self) -> None:
        identity = github_identity(
            {"id": 42, "name": "Jane"},
            [
                {"email": "old@example.com", "verified": True, "primary": False},
                {"email": "Jane@Example.com", "verified": True, "primary": True},
            ],
        )
        assert identity.provider == AuthProviderKind.GITHUB
        assert identity.subject == "42"
        assert identity.email == "jane@example.com"

    def test_falls_back_to_any_verified_email(self) -> None:
        identity = github_identity({"id": 42}, [{"email": "only@example.com", "verified": True, "primary": False}])
        assert identity.email == "only@example.com"

    def test_unverified_addresses_do_not_count(self) -> None:
        with pytest.raises(SsoError, match="verified"):
            github_identity({"id": 42}, [{"email": "x@example.com", "verified": False, "primary": True}])

    def test_profile_email_is_ignored(self) -> None:
        # The email on /user is whatever the person chose to show; only /user/emails says what is verified.
        with pytest.raises(SsoError):
            github_identity({"id": 42, "email": "shown@example.com"}, [])
