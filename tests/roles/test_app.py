# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The app of every role, booted as its process boots it: what each serves, its ops endpoints, the api's jobs, and the web UI's files.

Each app runs its real node through the ``TestClient``'s lifespan on the test containers; the internal token guard and
the web UI's file serving are checked on apps that need no node.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from api import __version__
from api.core.config import Settings
from api.core.roles import JobKind, Role
from api.db.redis import RedisClient
from api.feed.codec import internal_token
from api.jobs.queue import JobQueue
from api.main import create_app
from api.routes import internal
from tests.conftest import api_runtime, on_app_loop, queued_job_keys
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

# Short enough that an ``all`` process compiles and publishes its first feed within a second.
FAST = {"feed_compile_interval": 0.1}


def wait_ready(client: TestClient, timeout: float = 15.0) -> httpx.Response:
    """``/ready`` once it answers 200; fails with the last reasons when it never does."""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get("/ready")
        if response.status_code == 200 or time.monotonic() > deadline:
            assert response.status_code == 200, response.json()
            return response
        time.sleep(0.05)


class TestEveryRoleBoots:
    @pytest.mark.parametrize("role", [Role.API, Role.CONTROLLER, Role.WORKER, Role.ALL])
    def test_answers_its_ops_endpoints(self, build_app: Callable[..., FastAPI], role: Role) -> None:
        with TestClient(build_app(role, **FAST)) as client:
            assert client.get("/live").json() == {"status": "alive"}
            assert wait_ready(client).json() == {"status": "ready", "role": role.value, "reasons": []}
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json() == {
                "status": "healthy",
                "role": role.value,
                "instance": client.app.state.node.instance_id,  # type: ignore[attr-defined]
                "version": __version__,
            }
            metrics = client.get("/metrics")
            assert metrics.status_code == 200
            assert f'octoprox_ready{{role="{role.value}"}} 1.0' in metrics.text

    @pytest.mark.parametrize("role", [Role.CONTROLLER, Role.WORKER, Role.GATEWAY])
    def test_a_role_without_the_api_serves_ops_only(self, build_app: Callable[..., FastAPI], role: Role) -> None:
        app = build_app(role, **FAST)
        paths = {context.path for context in iter_route_contexts(app.routes)}
        assert {"/live", "/ready", "/health", "/metrics"} <= paths
        assert not any(path.startswith(("/api/", "/internal/", "/echo")) for path in paths)
        # No web UI either: nothing answers an unknown path.
        assert "/{full_path:path}" not in paths

    def test_api_and_all_serve_the_admin_api_echo_and_internal_endpoints(self, test_settings: Settings) -> None:
        for role in (Role.API, Role.ALL):
            app = create_app(test_settings.model_copy(update={"role": role}))
            paths = {context.path for context in iter_route_contexts(app.routes)}
            assert {"/live", "/ready", "/health", "/metrics", "/echo", "/api/v1/projects"} <= paths, role

    def test_a_gateway_without_a_feed_is_unready(self, build_app: Callable[..., FastAPI]) -> None:
        """Readiness is the runtimes': a gateway that has applied no feed yet must not take traffic."""
        # A secret no controller publishes under, so whatever feed Redis holds does not verify.
        app = build_app(Role.GATEWAY, internal_secret="a-gateway-nobody-feeds", **FAST)
        with TestClient(app) as client:
            assert client.get("/live").status_code == 200
            ready = client.get("/ready")
            assert ready.status_code == 503
            assert ready.json() == {"status": "unready", "role": "gateway", "reasons": ["gateway: feed not applied yet"]}
            health = client.get("/health")
            assert health.status_code == 503
            assert health.json()["status"] == "unhealthy"
            assert health.json()["reasons"] == ["gateway: feed not applied yet"]
            assert 'octoprox_ready{role="gateway"} 0.0' in client.get("/metrics").text

    def test_echo_serves_echo_and_its_ops_endpoints(self, build_app: Callable[..., FastAPI], tmp_path: Path) -> None:
        db = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
        app = build_app(Role.ECHO, geo_databases=[{"path": str(db)}], geo_echo_trusted_proxies=["127.0.0.0/8"])
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            forwarded = client.get("/echo", headers={"X-Forwarded-For": "81.2.69.160"}).json()
            assert forwarded["ip"] == "81.2.69.160" and forwarded["country"] == "GB" and forwarded["city"] == "London"
            assert client.get("/echo?nonce=n1").json()["nonce"] == "n1"

            assert client.get("/live").status_code == 200
            assert wait_ready(client).json() == {"status": "ready", "role": "echo", "reasons": []}
            assert client.get("/health").json()["role"] == "echo"
            metrics = client.get("/metrics").text
            assert 'octoprox_ready{role="echo"} 1.0' in metrics
            assert client.get("/api/v1/projects").status_code == 404


class NotedQueue(JobQueue):
    """A job queue that notes each publish, kind and keys, before it publishes."""

    def __init__(self, redis_client: RedisClient) -> None:
        super().__init__(redis_client)
        self.published: list[tuple[JobKind, list[str]]] = []

    async def publish(self, kind: JobKind, keys: Iterable[str]) -> int:
        keys = list(keys)
        self.published.append((kind, keys))
        return await super().publish(kind, keys)


class TestApiJobs:
    def test_the_routes_ask_for_jobs_on_the_api_runtime_s_queue(
        self, build_app: Callable[..., FastAPI], auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read when a request asks, so a queue the runtime holds is the one every route uses."""
        with TestClient(build_app(Role.API, **FAST), headers=auth_headers) as client:
            runtime = api_runtime(client)
            noted = NotedQueue(runtime.redis_client)
            monkeypatch.setattr(runtime, "jobs", noted)

            response = client.post("/api/v1/geo/reattribute", json={})

            assert response.status_code == 202
            assert noted.published == [(JobKind.REATTRIBUTE, ["all"])]
            assert on_app_loop(client, queued_job_keys, runtime.redis_client, JobKind.REATTRIBUTE) == ["all"]


class TestInternalToken:
    """Every ``/internal/v1`` route requires the token derived from ``internal_secret``."""

    def _client(self, test_settings: Settings) -> TestClient:
        app = FastAPI()
        app.state.settings = test_settings
        guarded = APIRouter(prefix="/internal/v1", dependencies=[Depends(internal.require_internal_token)])

        @guarded.get("/probe")
        async def probe() -> dict[str, str]:
            return {"status": "ok"}

        app.include_router(guarded)
        return TestClient(app)

    def test_the_router_is_guarded(self) -> None:
        assert internal.router.prefix == "/internal/v1"
        assert [dependency.dependency for dependency in internal.router.dependencies] == [
            internal.require_internal_token
        ]

    def test_only_the_derived_token_passes(self, test_settings: Settings) -> None:
        client = self._client(test_settings)
        token = internal_token(test_settings.internal_secret)

        assert client.get("/internal/v1/probe", headers={"Authorization": f"Bearer {token}"}).json() == {"status": "ok"}
        assert client.get("/internal/v1/probe", headers={"Authorization": f"bearer {token}"}).status_code == 200
        for headers in (
            {},
            {"Authorization": "Bearer "},
            {"Authorization": f"Bearer {token[:-1]}"},
            {"Authorization": f"Basic {token}"},
            {"Authorization": f"Bearer {internal_token('another-secret')}"},
            # The secret itself is not the token.
            {"Authorization": f"Bearer {test_settings.internal_secret}"},
        ):
            response = client.get("/internal/v1/probe", headers=headers)
            assert response.status_code == 401, headers
            assert response.headers["www-authenticate"] == "Bearer"


class TestWebUi:
    """The web UI's files are served from its build directory and from nowhere else."""

    @pytest.fixture
    def dist(self, tmp_path: Path) -> Path:
        dist = tmp_path / "web" / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>app</html>")
        (dist / "robots.txt").write_text("User-agent: *")
        (dist / "assets" / "app.js").write_text("console.log(1)")
        (tmp_path / "web" / "secret.txt").write_text("outside the build")
        (tmp_path / "ca.key").write_text("the CA key")
        (dist / "linked.txt").symlink_to(tmp_path / "ca.key")
        return dist

    def test_files_and_routes(self, test_settings: Settings, dist: Path) -> None:
        client = TestClient(create_app(test_settings.model_copy(update={"role": Role.API}), web_dist=dist))

        assert client.get("/robots.txt").text == "User-agent: *"
        assert client.get("/assets/app.js").text == "console.log(1)"
        # Every other path is a route of the single-page app.
        assert client.get("/projects/abc/overview").text == "<html>app</html>"
        assert client.get("/").text == "<html>app</html>"

    @pytest.mark.parametrize(
        "path",
        ["/%2e%2e/secret.txt", "/..%2fsecret.txt", "/%2e%2e%2fsecret.txt", "/%2e%2e/%2e%2e/ca.key", "/linked.txt"],
    )
    def test_nothing_outside_the_build_is_served(self, test_settings: Settings, dist: Path, path: str) -> None:
        """A percent-encoded ``..`` reaches the route decoded and unnormalised; a symlink can point anywhere."""
        client = TestClient(create_app(test_settings.model_copy(update={"role": Role.API}), web_dist=dist))

        response = client.get(path)

        assert response.status_code == 404
        assert "outside the build" not in response.text and "the CA key" not in response.text

    def test_no_build_no_web_ui(self, test_settings: Settings, tmp_path: Path) -> None:
        app = create_app(test_settings.model_copy(update={"role": Role.API}), web_dist=tmp_path / "missing")

        assert "/{full_path:path}" not in {context.path for context in iter_route_contexts(app.routes)}
