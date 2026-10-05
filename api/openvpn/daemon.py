# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The ``openvpn`` process: started with a config file, watched, and stopped.

Unlike WireGuard, which is an interface the kernel keeps, OpenVPN is a
daemon: it creates the tun interface when it starts and takes it down when
it exits. Octoprox runs it as a child, with the same ambient capabilities
the other tunnel tools get (see :mod:`api.tunnel.system`), reads its output
into the log and keeps the last lines for the error an operator sees when
it exits.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque

import structlog

from api.tunnel.system import CommandRunner

logger = structlog.get_logger()

STOP_GRACE_SECONDS = 5.0
_KEPT_LINES = 25


class OpenVpnDaemon:
    """One run of the daemon."""

    def __init__(self, runner: CommandRunner, *, binary: str = "openvpn") -> None:
        self._runner = runner
        self._binary = binary
        self._process: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task[None] | None = None
        self._recent: deque[str] = deque(maxlen=_KEPT_LINES)

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def returncode(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def recent_output(self) -> str:
        return "\n".join(self._recent)

    async def start(self, config_path: str) -> None:
        self._process = await self._runner.spawn(self._binary, "--config", config_path)
        self._pump = asyncio.create_task(self._pump_output(), name="openvpn_log_pump")
        logger.info("OpenVPN daemon started", pid=self._process.pid, config=config_path)

    async def wait(self) -> int:
        """Block until the daemon exits; its status."""
        if self._process is None:
            return -1
        return await self._process.wait()

    async def stop(self) -> None:
        """SIGTERM, then SIGKILL after a grace period; the tun interface goes with the process."""
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if self._pump is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None

    async def _pump_output(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._recent.append(line)
                lowered = line.lower()
                if "error" in lowered or "fatal" in lowered or "failed" in lowered:
                    logger.warning("openvpn", line=line)
                else:
                    # verb 3 is quiet in steady state and says what matters when
                    # a device connects (TLS, verification, admission), which is
                    # what an operator looks for; so info, not debug.
                    logger.info("openvpn", line=line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("OpenVPN output pump ended", error=str(exc))
