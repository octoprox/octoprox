# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The backup: the file's crypto and compatibility gate, the entities' rows, the kept user, the schema version, and the import.

Most tests are pure; those of the schema version and the import run on the test Postgres, and the Redis cleanup on the
test Redis.
"""

from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.backup import (
    _ENTITY_SPECS,
    BACKUP_FORMAT_VERSION,
    BackupDecryptError,
    BackupError,
    BackupFile,
    BackupIncompatibleError,
    BackupRepository,
    ImportResult,
    _EntitySpec,
    _KeptUser,
)
from api.core.stats import FailureKind
from api.db.models import (
    ApiTokenModel,
    AuthIdentityModel,
    GeoDatabaseBlobModel,
    IpObservationModel,
    MetricFlushModel,
    MetricTotalsModel,
    ProxyMetricsModel,
    ProxyModel,
    UserModel,
)
from api.db.redis import FAILURE_LOG_APPENDED_KEY, MITM_REQUESTS_KEY, RedisClient, project_keys
from api.db.repository import ProjectRepository
from api.models.backup import BackupPayload, ImportSummary
from api.models.project import Project
from tests.conftest import HEAD

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
        "host_metrics": [{"project_id": "p1", "connector_id": "c1", "host": "example.com", "request_count": 2}],
        "failure_metrics": [{"project_id": "p1", "kind": "upstream_timeout", "failure_count": 3}],
        "openvpn_settings": [{"id": "default", "ca_cert": "-----BEGIN CERTIFICATE-----"}],
        "openvpn_peers": [{"id": "o1", "project_id": "p1", "name": "laptop"}],
        "tunnel_peer_metrics": [{"peer_id": "o1", "project_id": "p1", "request_count": 1}],
        "geo_settings": [{"id": 1, "exit_ip_retention_days": 90}],
        "geo_databases": [{"id": "db1", "vendor": "maxmind", "kind": "country"}],
    }


def _built(**options: Any) -> BackupFile:
    """A file of the sample payload, as the export builds one."""
    return BackupFile.build(_sample_payload(), PASSPHRASE, schema_version=SCHEMA, include_metrics=False, **options)


class TestCryptoRoundTrip:
    def test_encrypt_decrypt_round_trip(self) -> None:
        payload = _sample_payload()
        backup = BackupFile.read(_built().content)
        envelope = backup.envelope
        assert envelope.schema_version == SCHEMA
        assert envelope.includes_metrics is False
        # Database files are opt-in; the flag defaults off and the payload
        # still carries the attribution configuration rows.
        assert envelope.includes_database_files is False

        out = backup.decrypt(PASSPHRASE)
        assert out.users == payload["users"]
        assert out.projects == payload["projects"]
        # A collection the payload model does not name would be dropped here, silently.
        assert out.host_metrics == payload["host_metrics"]
        assert out.failure_metrics == payload["failure_metrics"]
        assert out.openvpn_settings == payload["openvpn_settings"]
        assert out.openvpn_peers == payload["openvpn_peers"]
        assert out.tunnel_peer_metrics == payload["tunnel_peer_metrics"]
        assert out.geo_settings == payload["geo_settings"]
        assert out.geo_databases == payload["geo_databases"]
        assert out.geo_database_blobs == []

    def test_database_files_flag_recorded_in_envelope(self) -> None:
        assert BackupFile.read(_built(include_database_files=True).content).envelope.includes_database_files is True

    def test_wrong_passphrase_raises(self) -> None:
        with pytest.raises(BackupDecryptError):
            BackupFile.read(_built().content).decrypt("totally-wrong-passphrase")

    def test_garbage_file_rejected(self) -> None:
        with pytest.raises(BackupError):
            BackupFile.read(b"not a backup at all")

    def test_each_file_has_a_salt_of_its_own(self) -> None:
        """The key is derived per file: two exports under one passphrase share no key."""
        assert _built().envelope.kdf.salt != _built().envelope.kdf.salt


class TestEveryEntityTravels:
    def test_every_exported_entity_has_a_payload_and_a_summary_field(self) -> None:
        # Export writes every spec; import reads the payload model. A spec
        # the model does not name is exported and then dropped on import.
        keys = [spec.key for spec in _ENTITY_SPECS]
        assert [k for k in keys if k not in BackupPayload.model_fields] == []
        assert [k for k in keys if k not in ImportSummary.model_fields] == []

    def test_derived_state_stays_out_of_the_file(self) -> None:
        # Totals are recomputed from the imported history, and the flushes are the install's own bookkeeping.
        models = {spec.model for spec in _ENTITY_SPECS}
        assert MetricTotalsModel not in models and MetricFlushModel not in models


class TestCompatibility:
    @staticmethod
    def _file(**envelope: Any) -> BackupFile:
        """A built file read back, its envelope changed by ``envelope``."""
        changed = _built().envelope.model_copy(update=envelope)
        return BackupFile.read(changed.model_dump_json().encode("utf-8"))

    def test_matching_schema_passes(self) -> None:
        self._file().check_compatibility(SCHEMA)  # no raise

    def test_mismatched_schema_version_raises(self) -> None:
        with pytest.raises(BackupIncompatibleError):
            self._file().check_compatibility("001_initial_schema")

    def test_newer_format_version_raises(self) -> None:
        with pytest.raises(BackupIncompatibleError):
            self._file(format_version=BACKUP_FORMAT_VERSION + 1).check_compatibility(SCHEMA)

    def test_wrong_format_magic_raises(self) -> None:
        with pytest.raises(BackupIncompatibleError):
            self._file(format="something-else").check_compatibility(SCHEMA)


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
        spec = _EntitySpec("proxies", ProxyModel)
        row = spec.dump(proxy)
        # Datetimes are serialized to ISO strings; the metadata_ attribute
        # (DB column "metadata") is dumped under its ORM attribute name.
        assert row["created_at"] == "2026-01-01T12:00:00"
        assert row["metadata_"] == {"region": "eu"}

        rebuilt = spec.build(row)
        assert rebuilt.host == "proxy.example.com"
        assert rebuilt.port == 8080
        assert rebuilt.tags == ["a", "b"]
        assert rebuilt.metadata_ == {"region": "eu"}
        assert rebuilt.created_at == created

    def test_a_key_the_model_does_not_map_is_skipped(self) -> None:
        rebuilt = _EntitySpec("proxies", ProxyModel).build({"id": "proxy-1", "host": "h.example.com", "retired": 1})
        assert rebuilt.host == "h.example.com" and not hasattr(rebuilt, "retired")

    def test_metric_id_dropped_on_import(self) -> None:
        spec = _EntitySpec(
            "proxy_metrics", ProxyMetricsModel, is_metric=True, preserve_id=False
        )
        rebuilt = spec.build({"id": 999, "proxy_id": "proxy-1", "request_count": 5})
        # The auto-increment id must not be carried over (it would clash with
        # the sequence); the sequence assigns a fresh one on flush.
        assert rebuilt.id is None
        assert rebuilt.proxy_id == "proxy-1"
        assert rebuilt.request_count == 5

    def test_database_blob_round_trips_as_base64(self) -> None:
        # An mmdb file is binary and not JSON-safe; it travels base64-encoded
        # and comes back as the same bytes.
        content = bytes(range(256)) * 3
        blob = GeoDatabaseBlobModel(database_id="db1", data=content)
        spec = _EntitySpec("geo_database_blobs", GeoDatabaseBlobModel, is_database_file=True)
        row = spec.dump(blob)
        assert isinstance(row["data"], str)

        rebuilt = spec.build(row)
        assert rebuilt.data == content
        assert rebuilt.database_id == "db1"

    def test_observation_id_dropped_on_import(self) -> None:
        spec = _EntitySpec("ip_observations", IpObservationModel, is_metric=True, preserve_id=False)
        rebuilt = spec.build({"id": 42, "ip": "1.2.3.4", "source": "health_check", "observed_at": "2026-01-01T12:00:00"})
        assert rebuilt.id is None
        assert rebuilt.ip == "1.2.3.4"
        assert rebuilt.observed_at == datetime(2026, 1, 1, 12, 0, 0)


class TestUserConflictResolution:
    KEPT = _KeptUser(id="kept-id", username="admin", email="admin@example.com")

    def test_no_conflict_leaves_rows_untouched(self) -> None:
        rows = [{"id": "u2", "username": "bob", "email": "bob@example.com"}]
        assert self.KEPT.resolve_conflicts(rows, {}) == []
        assert rows == [{"id": "u2", "username": "bob", "email": "bob@example.com"}]

    def test_same_id_gets_fresh_id(self) -> None:
        rows = [{"id": "kept-id", "username": "other", "email": ""}]
        id_map: dict[str, str] = {}
        conflicts = self.KEPT.resolve_conflicts(rows, id_map)
        assert rows[0]["id"] != "kept-id"
        assert len(rows[0]["id"]) == 36  # uuid4
        # The imported sign-ins and tokens of that user follow it to the fresh id.
        assert id_map == {"kept-id": rows[0]["id"]}
        assert conflicts[0].new_id is True
        assert conflicts[0].new_username == "other"

    def test_same_username_is_renamed(self) -> None:
        rows = [{"id": "u2", "username": "admin", "email": ""}]
        conflicts = self.KEPT.resolve_conflicts(rows, {})
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
        self.KEPT.resolve_conflicts(rows, {})
        assert rows[0]["username"] == "admin-imported-3"
        usernames = [r["username"] for r in rows]
        assert len(set(usernames)) == 3

    def test_same_email_is_cleared(self) -> None:
        rows = [{"id": "u2", "username": "other", "email": "admin@example.com"}]
        conflicts = self.KEPT.resolve_conflicts(rows, {})
        assert rows[0]["email"] == ""
        assert conflicts[0].email_cleared is True

    def test_empty_kept_email_never_conflicts(self) -> None:
        kept = replace(self.KEPT, email="")
        rows = [{"id": "u2", "username": "other", "email": ""}]
        assert kept.resolve_conflicts(rows, {}) == []

    def test_all_three_conflicts_reported_once(self) -> None:
        rows = [{"id": "kept-id", "username": "admin", "email": "admin@example.com"}]
        conflicts = self.KEPT.resolve_conflicts(rows, {})
        assert len(conflicts) == 1
        c = conflicts[0]
        assert (c.new_id, c.new_username, c.email_cleared) == (
            True,
            "admin-imported",
            True,
        )


class TestForgetProjects:
    def test_a_projects_keys_are_its_captures_and_failure_log(self) -> None:
        keys = project_keys("p1")
        assert len(keys) == len(set(keys)) == 2 + len(FailureKind)
        assert all(":p1:" in key or key.endswith(":p1") for key in keys)
        assert MITM_REQUESTS_KEY.format(project_id="p1") in keys
        assert FAILURE_LOG_APPENDED_KEY.format(project_id="p1") in keys

    async def test_deletes_only_the_removed_projects_keys_in_batches(self, redis_client: RedisClient) -> None:
        # Enough projects that their key names fill more than one DEL.
        removed = [f"gone-{n}" for n in range(80)]
        for project_id in [*removed, "kept"]:
            await redis_client.client.xadd(MITM_REQUESTS_KEY.format(project_id=project_id), {"method": "GET"})
            appended = FAILURE_LOG_APPENDED_KEY.format(project_id=project_id)
            await redis_client.client.hset(appended, "upstream_timeout", 1)

        result = ImportResult(ImportSummary(), removed_project_ids=removed)
        assert await result.forget_removed_projects(redis_client) == 2 * len(removed)
        assert await redis_client.client.exists(MITM_REQUESTS_KEY.format(project_id="gone-7")) == 0
        assert await redis_client.client.exists(
            MITM_REQUESTS_KEY.format(project_id="kept"), FAILURE_LOG_APPENDED_KEY.format(project_id="kept")
        ) == 2
        assert await ImportResult(ImportSummary()).forget_removed_projects(redis_client) == 0


class TestOnTheDatabase:
    async def test_the_schema_version_is_the_revision_the_database_records(self, db_session: AsyncSession) -> None:
        """A 5.x file carries 047 and is refused; a 6.0 file carries 6.0's revision."""
        assert await BackupRepository(db_session).schema_version() == HEAD

    async def test_a_database_without_a_version_table_has_no_schema_version(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Read as Alembic reads it: no table is no revision, so the file is refused rather than the query failing."""
        async with db_session_factory() as session:
            # Hidden in this transaction only: the rollback brings it back.
            await session.execute(text("ALTER TABLE alembic_version RENAME TO hidden_version_table"))
            with pytest.raises(BackupError, match="no applied migration revision"):
                await BackupRepository(session).schema_version()
            await session.rollback()

    async def test_a_file_exported_here_imports_here(self, db_session: AsyncSession) -> None:
        await ProjectRepository(db_session).create(Project(name="Kept", username="kept", password="pw"))
        await db_session.commit()
        repository = BackupRepository(db_session)
        payload = await repository.export(include_metrics=True)
        built = BackupFile.build(
            payload, PASSPHRASE, schema_version=await repository.schema_version(), include_metrics=True
        )

        backup = BackupFile.read(built.content)
        backup.check_compatibility(await repository.schema_version())
        result = await repository.replace_all(backup.decrypt(PASSPHRASE))
        await db_session.commit()

        assert set(payload) == {spec.key for spec in _ENTITY_SPECS}
        assert result.summary.projects == 1
        assert [project.name for project in await ProjectRepository(db_session).get_all()] == ["Kept"]

    async def test_the_kept_user_keeps_its_sign_ins_and_tokens_and_its_imported_copy_takes_its_own(
        self, db_session: AsyncSession
    ) -> None:
        """The file's copy of the kept user gets a fresh id its sign-ins and tokens follow; any that is the kept one's is dropped."""
        db_session.add(UserModel(id="kept-user", username="kept", email="kept@example.com"))
        await db_session.flush()
        db_session.add(AuthIdentityModel(id="ki", user_id="kept-user", provider="github", subject="kept-gh"))
        db_session.add(ApiTokenModel(id="kt", user_id="kept-user", name="k", token_hash="hash-kept", token_prefix="p"))
        await db_session.commit()
        payload = BackupPayload(
            users=[
                {"id": "kept-user", "username": "kept", "email": "kept@example.com"},
                {"id": "other", "username": "other", "email": ""},
            ],
            auth_identities=[
                {"id": "i-follows", "user_id": "kept-user", "provider": "google", "subject": "copy-google"},
                {"id": "i-taken", "user_id": "other", "provider": "github", "subject": "kept-gh"},
            ],
            api_tokens=[
                {"id": "t-follows", "user_id": "kept-user", "name": "c", "token_hash": "hash-copy", "token_prefix": "p"},
                {"id": "t-taken", "user_id": "other", "name": "o", "token_hash": "hash-kept", "token_prefix": "p"},
            ],
        )

        result = await BackupRepository(db_session).replace_all(payload, keep_user_id="kept-user")
        await db_session.commit()

        copy = (await db_session.execute(select(UserModel.id).where(UserModel.username == "kept-imported"))).scalar_one()
        identities = {
            row.id: row.user_id for row in await db_session.execute(select(AuthIdentityModel.id, AuthIdentityModel.user_id))
        }
        tokens = {row.id: row.user_id for row in await db_session.execute(select(ApiTokenModel.id, ApiTokenModel.user_id))}
        assert copy != "kept-user"
        assert identities == {"ki": "kept-user", "i-follows": copy}
        assert tokens == {"kt": "kept-user", "t-follows": copy}
        assert [conflict.original_username for conflict in result.summary.user_conflicts] == ["kept"]
