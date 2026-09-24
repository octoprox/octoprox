# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the logging configuration: formats and request context."""

import json
import logging
import logging.config
from collections.abc import Generator

import pytest
import structlog

from api.core.logging import setup_logging


@pytest.fixture
def restore_logging() -> Generator[None, None, None]:
    """Put logging back the way conftest expects it after a test reconfigures it."""
    yield
    setup_logging("DEBUG", "console")


def _last_line(capsys: pytest.CaptureFixture[str]) -> str:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "nothing was logged"
    return lines[-1]


class TestSetupLogging:
    def test_rejects_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="Unknown log format"):
            setup_logging("INFO", "xml")

    def test_json_lines_include_bound_context(
        self, capsys: pytest.CaptureFixture[str], restore_logging: None
    ) -> None:
        setup_logging("INFO", "json")
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id="req-1", user_id="u-1", username="alice")
        try:
            structlog.get_logger("test.json").info("Did a thing", project="p1")
        finally:
            structlog.contextvars.clear_contextvars()

        record = json.loads(_last_line(capsys))
        assert record["event"] == "Did a thing"
        assert record["project"] == "p1"
        assert record["request_id"] == "req-1"
        assert record["user_id"] == "u-1"
        assert record["username"] == "alice"
        assert record["level"] == "info"
        assert record["logger"] == "test.json"
        assert "timestamp" in record

    def test_instance_id_on_every_line(
        self, capsys: pytest.CaptureFixture[str], restore_logging: None
    ) -> None:
        setup_logging("INFO", "json", instance_id="octoprox-2")
        structlog.contextvars.clear_contextvars()

        structlog.get_logger("test.instance").info("outside a request")
        logging.getLogger("uvicorn.error").info("stdlib line")

        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        by_event = {record["event"]: record for record in lines}
        assert by_event["outside a request"]["instance"] == "octoprox-2"
        assert by_event["stdlib line"]["instance"] == "octoprox-2"

    def test_instance_id_omitted_when_unset(
        self, capsys: pytest.CaptureFixture[str], restore_logging: None
    ) -> None:
        setup_logging("INFO", "json")
        structlog.get_logger("test.instance").info("no instance")
        assert "instance" not in json.loads(_last_line(capsys))

    def test_stdlib_loggers_render_through_same_formatter(
        self, capsys: pytest.CaptureFixture[str], restore_logging: None
    ) -> None:
        setup_logging("INFO", "json")
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id="req-2")
        try:
            logging.getLogger("uvicorn.error").warning("plain %s message", "stdlib")
        finally:
            structlog.contextvars.clear_contextvars()

        record = json.loads(_last_line(capsys))
        assert record["event"] == "plain stdlib message"
        assert record["logger"] == "uvicorn.error"
        assert record["level"] == "warning"
        assert record["request_id"] == "req-2"

    def test_uvicorn_cli_handlers_are_routed_through_formatter(
        self, capsys: pytest.CaptureFixture[str], restore_logging: None
    ) -> None:
        """Mimic `uvicorn api.main:app`: uvicorn configures logging before importing the app."""
        import uvicorn.config

        logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
        setup_logging("INFO", "json", instance_id="octoprox-1")

        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "172.19.0.7:49574", "GET", "/health", "1.0", 200
        )
        logging.getLogger("uvicorn.error").info("Application startup complete.")

        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        records = [json.loads(line) for line in lines]
        events = [record["event"] for record in records]
        assert events == [
            '172.19.0.7:49574 - "GET /health HTTP/1.0" 200',
            "Application startup complete.",
        ]
        assert {record["logger"] for record in records} == {"uvicorn.access", "uvicorn.error"}
        assert all(record["instance"] == "octoprox-1" for record in records)

    def test_console_format_is_not_json(
        self, capsys: pytest.CaptureFixture[str], restore_logging: None
    ) -> None:
        setup_logging("INFO", "console")
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id="req-3")
        try:
            structlog.get_logger("test.console").info("Console line")
        finally:
            structlog.contextvars.clear_contextvars()

        line = _last_line(capsys)
        assert "Console line" in line
        assert "request_id" in line
        # capsys is not a terminal, so no ANSI escape codes may appear.
        assert "\x1b[" not in line
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)

    def test_level_filters_below_threshold(
        self, capsys: pytest.CaptureFixture[str], restore_logging: None
    ) -> None:
        setup_logging("WARNING", "json")
        structlog.get_logger("test.level").info("should not appear")
        structlog.get_logger("test.level").warning("should appear")

        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        events = [json.loads(line)["event"] for line in lines]
        assert "should not appear" not in events
        assert "should appear" in events
