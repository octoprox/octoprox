# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""How the proxy listener authenticates a client and reads the routing suffixes of its username.

The project is found by the username without its ``-sessid-``, ``-cc-``,
``-st-`` and ``-city-`` suffixes, through the router.
"""

import asyncio
import base64
from unittest.mock import MagicMock

import structlog

from api.core.proxy_server import ProxyServer
from api.gateway.metering import RequestMetering
from api.models.location import LocationTarget
from api.models.project import Project
from api.routing.router import Router


def _server(project: Project) -> ProxyServer:
    router = MagicMock(spec=Router)
    router.get_project_by_username.side_effect = lambda username: project if username == project.username else None
    return ProxyServer(router, MagicMock(spec=RequestMetering), host="127.0.0.1", port=0, timeout=30.0)


def _headers(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"proxy-authorization": f"Basic {token}"}


class TestCountryAuth:
    def test_country_and_sessid_extracted(self) -> None:
        project = Project(name="P", username="geo", password="pw")
        result = _server(project)._authenticate_project(_headers("geo-cc-us-sessid-abc", "pw"))
        assert result is not None
        assert result.project.id == project.id
        assert result.sessid == "abc"
        assert result.location == LocationTarget(country="US") and result.location_error is None

    def test_country_alone(self) -> None:
        project = Project(name="P", username="geo", password="pw")
        result = _server(project)._authenticate_project(_headers("geo-cc-de", "pw"))
        assert result is not None
        assert result.sessid is None
        assert result.location == LocationTarget(country="DE")

    def test_wrong_password_with_country_fails(self) -> None:
        project = Project(name="P", username="geo", password="pw")
        assert _server(project)._authenticate_project(_headers("geo-cc-de", "nope")) is None

    def test_no_proxy_message_names_the_place(self) -> None:
        assert "for US and" in ProxyServer._no_proxy_message(LocationTarget(country="US"))
        assert "US, state NY, city new_york" in ProxyServer._no_proxy_message(
            LocationTarget(country="US", state="NY", city="new_york")
        )
        assert "for this domain" in ProxyServer._no_proxy_message(None)

    def test_state_and_city_are_extracted(self) -> None:
        project = Project(name="P", username="geo", password="pw")
        result = _server(project)._authenticate_project(_headers("geo-cc-us-st-ny-city-new_york-sessid-abc", "pw"))
        assert result is not None
        assert result.location == LocationTarget(country="US", state="NY", city="new_york") and result.sessid == "abc"
        assert result.location_error is None
        # A state without a country authenticates but cannot be routed.
        orphan = _server(project)._authenticate_project(_headers("geo-st-ny", "pw"))
        assert orphan is not None and orphan.location_error


class TestCredentials:
    def test_unknown_user_and_malformed_headers_fail(self) -> None:
        server = _server(Project(name="P", username="geo", password="pw"))
        assert server._authenticate_project(_headers("someone-else", "pw")) is None
        assert server._authenticate_project({}) is None
        assert server._authenticate_project({"proxy-authorization": "Bearer abc"}) is None
        assert server._authenticate_project({"proxy-authorization": "Basic !!!"}) is None
        no_password = base64.b64encode(b"geo").decode()
        assert server._authenticate_project({"proxy-authorization": f"Basic {no_password}"}) is None

    async def test_credentials_never_reach_the_log(self) -> None:
        """A request through the listener, logged at every level: neither the password nor the header shows."""
        project = Project(name="P", username="geo", password="s3cret-pw")
        server = _server(project)
        router = server._router
        assert isinstance(router, MagicMock)
        router.select_proxy_for_project.return_value = None
        router.are_all_proxies_quarantined.return_value = False
        router.traffic_limit_status.return_value = None
        token = base64.b64encode(b"geo-cc-us:s3cret-pw").decode()
        await server.start()
        try:
            with structlog.testing.capture_logs() as logs:
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                writer.write(f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode())
                await writer.drain()
                assert (await asyncio.wait_for(reader.read(), 5)).startswith(b"HTTP/1.1 502")
                writer.close()
        finally:
            await server.stop()
        # Debug lines were captured; none carries the credential.
        assert "Authenticated project" in [entry["event"] for entry in logs]
        logged = repr(logs)
        assert "s3cret-pw" not in logged and token not in logged
