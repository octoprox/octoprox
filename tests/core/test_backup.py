# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the backup service crypto, compatibility and serialization.

These are pure (no DB / containers): they exercise the encryption round-trip,
the version-compatibility gate, and the generic column (de)serialization.
"""

from datetime import datetime

import pytest

from api.core.backup import (
    BACKUP_FORMAT_VERSION,
    BackupDecryptError,
    BackupIncompatibleError,
    _build_model,
    _dump_row,
    _EntitySpec,
    _resolve_user_conflicts,
    build_backup_file,
    check_compatibility,
    decrypt_payload,
    read_envelope,
)
from api.db.models import ProxyMetricsModel, ProxyModel

PASSPHRASE = "correct horse battery"
SCHEMA = "019_add_entity_version_columns"


def _sample_payload() -> dict:
    return {
        "users": [{"id": "u1", "username": "admin"}],
        "projects": [{"id": "p1", "name": "Proj"}],
        "credentials": [],
        "connectors": [],
        "proxies": [],
        "proxy_metrics": [],
        "project_metrics": [],
    }


class TestCryptoRoundTrip:
    def test_encrypt_decrypt_round_trip(self) -> None:
        payload = _sample_payload()
        file_bytes = build_backup_file(
            payload, PASSPHRASE, schema_version=SCHEMA, include_metrics=False
        )
        envelope = read_envelope(file_bytes)
        assert envelope.schema_version == SCHEMA
        assert envelope.includes_metrics is False

        out = decrypt_payload(envelope, PASSPHRASE)
        assert out.users == payload["users"]
        assert out.projects == payload["projects"]

    def test_wrong_passphrase_raises(self) -> None:
        file_bytes = build_backup_file(
            _sample_payload(), PASSPHRASE, schema_version=SCHEMA, include_metrics=False
        )
        envelope = read_envelope(file_bytes)
        with pytest.raises(BackupDecryptError):
            decrypt_payload(envelope, "totally-wrong-passphrase")

    def test_garbage_file_rejected(self) -> None:
        from api.core.backup import BackupError

        with pytest.raises(BackupError):
            read_envelope(b"not a backup at all")


class TestCompatibility:
    def _envelope(self):
        file_bytes = build_backup_file(
            _sample_payload(), PASSPHRASE, schema_version=SCHEMA, include_metrics=False
        )
        return read_envelope(file_bytes)

    def test_matching_schema_passes(self) -> None:
        check_compatibility(self._envelope(), SCHEMA)  # no raise

    def test_mismatched_schema_version_raises(self) -> None:
        with pytest.raises(BackupIncompatibleError):
            check_compatibility(self._envelope(), "001_initial_schema")

    def test_newer_format_version_raises(self) -> None:
        env = self._envelope().model_copy(
            update={"format_version": BACKUP_FORMAT_VERSION + 1}
        )
        with pytest.raises(BackupIncompatibleError):
            check_compatibility(env, SCHEMA)

    def test_wrong_format_magic_raises(self) -> None:
        env = self._envelope().model_copy(update={"format": "something-else"})
        with pytest.raises(BackupIncompatibleError):
            check_compatibility(env, SCHEMA)


class TestColumnSerialization:
    def test_proxy_row_round_trip_preserves_fields(self) -> None:
        created = datetime(2026, 1, 1, 12, 0, 0)
        proxy = ProxyModel(
            id="proxy-1",
            host="proxy.example.com",
            port=8080,
            protocol="http",
            connector_id="conn-1",
            tags=["a", "b"],
            metadata_={"region": "eu"},
            created_at=created,
            updated_at=datetime(2026, 1, 2, 0, 0, 0),
        )
        row = _dump_row(proxy)
        # Datetimes are serialized to ISO strings; the metadata_ attribute
        # (DB column "metadata") is dumped under its ORM attribute name.
        assert row["created_at"] == "2026-01-01T12:00:00"
        assert row["metadata_"] == {"region": "eu"}

        rebuilt = _build_model(_EntitySpec("proxies", ProxyModel), row)
        assert rebuilt.host == "proxy.example.com"
        assert rebuilt.port == 8080
        assert rebuilt.tags == ["a", "b"]
        assert rebuilt.metadata_ == {"region": "eu"}
        assert rebuilt.created_at == created

    def test_metric_id_dropped_on_import(self) -> None:
        spec = _EntitySpec(
            "proxy_metrics", ProxyMetricsModel, is_metric=True, preserve_id=False
        )
        rebuilt = _build_model(spec, {"id": 999, "proxy_id": "proxy-1", "request_count": 5})
        # The auto-increment id must not be carried over (it would clash with
        # the sequence); the sequence assigns a fresh one on flush.
        assert rebuilt.id is None
        assert rebuilt.proxy_id == "proxy-1"
        assert rebuilt.request_count == 5


class TestUserConflictResolution:
    KEPT = {"id": "kept-id", "username": "admin", "email": "admin@example.com"}

    def test_no_conflict_leaves_rows_untouched(self) -> None:
        rows = [{"id": "u2", "username": "bob", "email": "bob@example.com"}]
        assert _resolve_user_conflicts(rows, self.KEPT) == []
        assert rows == [{"id": "u2", "username": "bob", "email": "bob@example.com"}]

    def test_same_id_gets_fresh_id(self) -> None:
        rows = [{"id": "kept-id", "username": "other", "email": ""}]
        conflicts = _resolve_user_conflicts(rows, self.KEPT)
        assert rows[0]["id"] != "kept-id"
        assert len(rows[0]["id"]) == 36  # uuid4
        assert conflicts[0].new_id is True
        assert conflicts[0].new_username == "other"

    def test_same_username_is_renamed(self) -> None:
        rows = [{"id": "u2", "username": "admin", "email": ""}]
        conflicts = _resolve_user_conflicts(rows, self.KEPT)
        assert rows[0]["username"] == "admin-imported"
        assert conflicts == [
            conflicts[0].model_copy(
                update={"original_username": "admin", "new_username": "admin-imported"}
            )
        ]

    def test_rename_avoids_other_imported_usernames(self) -> None:
        rows = [
            {"id": "u2", "username": "admin", "email": ""},
            {"id": "u3", "username": "admin-imported", "email": ""},
            {"id": "u4", "username": "admin-imported-2", "email": ""},
        ]
        _resolve_user_conflicts(rows, self.KEPT)
        assert rows[0]["username"] == "admin-imported-3"
        usernames = [r["username"] for r in rows]
        assert len(set(usernames)) == 3

    def test_same_email_is_cleared(self) -> None:
        rows = [{"id": "u2", "username": "other", "email": "admin@example.com"}]
        conflicts = _resolve_user_conflicts(rows, self.KEPT)
        assert rows[0]["email"] == ""
        assert conflicts[0].email_cleared is True

    def test_empty_kept_email_never_conflicts(self) -> None:
        kept = {**self.KEPT, "email": ""}
        rows = [{"id": "u2", "username": "other", "email": ""}]
        assert _resolve_user_conflicts(rows, kept) == []

    def test_all_three_conflicts_reported_once(self) -> None:
        rows = [{"id": "kept-id", "username": "admin", "email": "admin@example.com"}]
        conflicts = _resolve_user_conflicts(rows, self.KEPT)
        assert len(conflicts) == 1
        c = conflicts[0]
        assert (c.new_id, c.new_username, c.email_cleared) == (
            True,
            "admin-imported",
            True,
        )
