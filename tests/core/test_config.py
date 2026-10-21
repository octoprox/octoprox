# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the settings of the role split: where each is read from and what it defaults to.

Every test builds ``Settings`` from the environment and its own YAML file (or
none) only: the ``.env`` file is skipped and the variables under test are
cleared, so neither the repository's config/dev.yaml nor the shell leaks in.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from api.core import config
from api.core.config import Settings
from api.core.roles import JobKind, Role

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_yaml_config_path", None)
    for name in (
        "ROLE",
        "WORKER_JOBS",
        "DB_APPLICATION_NAME",
        "INSTANCE_ID",
        "INTERNAL_API_URL",
        "REDIS_REQUEST_TIMEOUT",
        "FEED_COMPILE_INTERVAL",
        "RECONCILE_INTERVAL",
        "HEALTH_CHECK_CONCURRENCY",
        "DEFAULT_STRATEGY",
    ):
        monkeypatch.delenv(f"OCTOPROX_{name}", raising=False)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def _yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    path = tmp_path / "octoprox.yaml"
    path.write_text(text)
    monkeypatch.setattr(config, "_yaml_config_path", path)


class TestRole:
    def test_a_process_runs_everything_unless_told_otherwise(self) -> None:
        assert _settings().role is Role.ALL

    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OCTOPROX_ROLE", "gateway")
        assert _settings().role is Role.GATEWAY

    def test_the_names_reserved_before_the_split_are_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OCTOPROX_ROLE", "data")
        with pytest.raises(ValidationError):
            _settings()


class TestWorkerJobs:
    def test_a_worker_runs_every_kind_by_default(self) -> None:
        assert _settings().worker_jobs == list(JobKind)

    def test_the_environment_lists_them_comma_separated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not JSON, which is what pydantic-settings expects of a list variable unless told otherwise."""
        monkeypatch.setenv("OCTOPROX_WORKER_JOBS", "health,sync")
        assert _settings().worker_jobs == [JobKind.HEALTH, JobKind.SYNC]

    def test_spaces_empty_entries_and_repeats_are_forgiven(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OCTOPROX_WORKER_JOBS", " scale , ,health,scale")
        assert _settings().worker_jobs == [JobKind.SCALE, JobKind.HEALTH]

    def test_an_empty_list_runs_no_job_kinds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OCTOPROX_WORKER_JOBS", "")
        assert _settings().worker_jobs == []

    def test_an_unknown_kind_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OCTOPROX_WORKER_JOBS", "health,reboot")
        with pytest.raises(ValidationError, match="reboot"):
            _settings()

    def test_yaml_and_arguments_give_a_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _yaml(monkeypatch, tmp_path, "server:\n  worker_jobs: [enrich, reattribute]\n")
        assert _settings().worker_jobs == [JobKind.ENRICH, JobKind.REATTRIBUTE]
        assert _settings(worker_jobs=["provision"]).worker_jobs == [JobKind.PROVISION]


class TestDatabaseApplicationName:
    def test_unset_it_names_the_role(self) -> None:
        """So pg_stat_activity tells the roles' load apart."""
        settings = _settings(role=Role.WORKER)

        assert settings.db_application_name == "octoprox-worker"
        assert settings.database_url_sync.endswith("?application_name=octoprox-worker")

    def test_a_name_given_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert (
            _settings(role=Role.WORKER, db_application_name="reporting").db_application_name
            == "reporting"
        )
        monkeypatch.setenv("OCTOPROX_DB_APPLICATION_NAME", "octoprox-blue")
        assert _settings(role=Role.API).db_application_name == "octoprox-blue"


class TestInstanceId:
    def test_unset_it_names_the_host_with_a_suffix_per_process(self) -> None:
        first, second = _settings().instance_id, _settings().instance_id

        for instance_id in (first, second):
            assert re.fullmatch(re.escape(socket.gethostname()) + r"-[0-9a-f]{6}", instance_id)
        assert first != second

    def test_a_given_id_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OCTOPROX_INSTANCE_ID", "octoprox-gateway-1")
        assert _settings().instance_id == "octoprox-gateway-1"


class TestInternalApiUrl:
    def test_a_trailing_slash_is_dropped(self) -> None:
        """Gateways append paths to it."""
        assert (
            _settings(internal_api_url=" http://octoprox-api:8000/ ").internal_api_url
            == "http://octoprox-api:8000"
        )

    def test_it_must_be_an_http_url(self) -> None:
        with pytest.raises(ValidationError, match="internal_api_url"):
            _settings(internal_api_url="octoprox-api:8000")


class TestRedisRequestTimeout:
    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OCTOPROX_REDIS_REQUEST_TIMEOUT", "0.5")
        assert _settings().redis_request_timeout == 0.5

    def test_a_deadline_of_nothing_is_refused(self) -> None:
        """It bounds every call a request or a save waits on; zero would fail them all."""
        with pytest.raises(ValidationError, match="redis_request_timeout"):
            _settings(redis_request_timeout=0)


class TestReconcileInterval:
    def test_is_read_from_the_environment_without_the_feed_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every role reads it, though its YAML key sits under ``feed:``."""
        monkeypatch.setenv("OCTOPROX_RECONCILE_INTERVAL", "600")
        assert _settings().reconcile_interval == 600

    def test_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="reconcile_interval"):
            _settings(reconcile_interval=0)


class TestDefaultStrategy:
    def test_a_name_no_router_has_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """So every role stops at start, as 5.0.11 did, rather than a gateway failing each request it routes by it."""
        monkeypatch.setenv("OCTOPROX_DEFAULT_STRATEGY", "fastest")
        with pytest.raises(ValidationError, match="Unknown strategy: fastest"):
            _settings()

    def test_a_known_name_is_kept_as_routers_know_it(self) -> None:
        assert _settings(default_strategy="Health_Based").default_strategy == "health_based"


class TestRetiredSettings:
    def test_a_5x_file_that_sets_the_observation_flush_still_loads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Workers ingest observations as they arrive, so 6.0 ignores the flush 5.x read and still reads its sibling."""
        _yaml(
            monkeypatch,
            tmp_path,
            "geo:\n"
            "  observations:\n"
            "    publish_interval_seconds: 2\n"
            "    flush_interval_seconds: 30\n",
        )

        assert _settings().geo_observation_publish_interval == 2


class TestYamlKeys:
    def test_the_new_settings_sit_beside_their_siblings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _yaml(
            monkeypatch,
            tmp_path,
            "redis:\n"
            "  url: redis://redis:6379/0\n"
            "  request_timeout_seconds: 0.5\n"
            "feed:\n"
            "  compile_interval_seconds: 2\n"
            "  reconcile_interval_seconds: 120\n"
            "proxy:\n"
            "  health_check:\n"
            "    concurrency: 8\n",
        )
        settings = _settings()

        assert settings.redis_request_timeout == 0.5
        assert (settings.feed_compile_interval, settings.reconcile_interval) == (2, 120)
        assert settings.health_check_concurrency == 8

    def test_defaults_without_them(self) -> None:
        settings = _settings()

        assert settings.redis_request_timeout == 2.0
        # A compile at most once a second; everything read in full once an hour.
        assert (settings.feed_compile_interval, settings.reconcile_interval) == (1.0, 3600.0)
        assert settings.health_check_concurrency == 50
        assert settings.internal_api_url == "http://localhost:8000"

    @pytest.mark.parametrize("name", ["dev.yaml", "prod.yaml"])
    def test_the_shipped_files_load_and_leave_the_session_name_to_the_role(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        monkeypatch.setattr(config, "_yaml_config_path", CONFIG_DIR / name)
        settings = _settings(role=Role.CONTROLLER)

        assert settings.db_application_name == "octoprox-controller"
        assert settings.health_check_concurrency == 50
        # The files spell out the Redis deadline and the feed's intervals.
        shipped = config._load_yaml_config(CONFIG_DIR / name)
        assert shipped["redis_request_timeout"] == settings.redis_request_timeout == 2
        assert shipped["feed_compile_interval"] == settings.feed_compile_interval == 1
        assert shipped["reconcile_interval"] == settings.reconcile_interval == 3600
