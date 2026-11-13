#!/usr/bin/env -S uv run --script
# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pillow==13.0.0",
#     "playwright==1.65.1",
# ]
# ///

"""Re-take the screenshots of octoprox.com (docs/assets/screens) from the real web UI with demo data.

  uv run scripts/site_screenshots/capture.py                                 every screen, light and dark
  uv run scripts/site_screenshots/capture.py --only overview,overview-phone  some screens (phones end in -phone)
  uv run scripts/site_screenshots/capture.py --themes dark                   one theme
  uv run scripts/site_screenshots/capture.py --node "$(nvm which 24)"        a Node other than the one on PATH

Once per machine, Chromium for the Playwright this script pins (uvx leaves the project's .venv and uv.lock alone):
  uvx --from playwright==1.65.1 playwright install chromium

uv gives the script an environment of its own, so Playwright and Pillow stay out of the project's
dependencies. It needs web/node_modules (npm ci in web/) and a Node as new as web/package.json's
engines. It serves web/ with Vite on a free local port (or --port), answers every /api call in the
browser from fixtures/ so no backend is involved, pins the browser clock to fixtures.NOW and writes
each screen, taken at 2x, as lossless WebP: <screen>-<theme>.webp, phones <screen>-phone-<theme>.webp.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fixtures import NOW, VERSION, handle
from fixtures.world import P1, connectors_of
from PIL import Image
from playwright.async_api import Browser, ConsoleMessage, Page, Request, Route, async_playwright
from playwright.async_api import Error as PlaywrightError

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"
OUT_DIR = ROOT / "docs" / "assets" / "screens"
HOST = "127.0.0.1"
THEMES = ("light", "dark")

LOCALE = "en-US-u-hc-h23"  # US formats with a 24-hour clock
TIMEZONE = "Europe/London"  # UTC in November, so the charts show the hours the data was made for
SCALE = 2  # sharp on high-density screens
DESKTOP: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
PHONE: dict[str, Any] = {
    "viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True,
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                  "Version/18.5 Mobile/15E148 Safari/604.1",
}
# Lossless, at the most effort: on these shots it is about as small as lossy quality 80, whose
# chroma subsampling smears coloured badges and text on dark backgrounds.
WEBP = {"lossless": True, "quality": 100, "method": 6}

BRIGHT_DATA = next(c for c in connectors_of(P1) if c.key == "brightdata")


# --------------------------------------------------------------------------- what a screen does before its shot

async def open_traffic_tab(page: Page) -> None:
    """Show the inspector's Traffic tab, scrolled to the traffic limit."""
    panel = page.locator("aside[aria-label='Edit connector']")
    await panel.wait_for()
    await panel.get_by_role("tab", name="Traffic", exact=True).click()
    await page.wait_for_timeout(400)
    # From the limit down, the action at it, the price, this month's usage and the traffic chart are all in
    # view (the shot is 1000 px tall for that).
    await panel.evaluate("""(aside) => {
        const body = aside.querySelector('.overflow-y-auto')
        const label = [...aside.querySelectorAll('label span')].find((s) => s.textContent.trim() === 'Traffic limit')
        // 22 px above the label puts the section's rule at the top and keeps the next section (Latency) out of view.
        if (body && label) body.scrollTop += label.getBoundingClientRect().top - body.getBoundingClientRect().top - 22
    }""")


async def open_limit_change(page: Page) -> None:
    """Open the Activity entry of the Bright Data traffic limit raised 12 minutes ago."""
    await page.locator("tbody tr").filter(has_text="12 min ago").filter(has_text="Bright Data").first.click()
    await page.wait_for_timeout(500)


async def fill_connect(page: Page) -> None:
    """Ask the Connect page for New York, with a test URL for the code snippets."""
    await page.select_option("#connect-country", "US")
    await page.select_option("#connect-state", "NY")
    await page.fill("#connect-city", "New York")
    await page.get_by_label("Test URL").fill("https://shop.example.com/products/4711")
    # No focus ring in the shot, and back at the top of the page.
    await page.evaluate("() => document.activeElement && document.activeElement.blur()")
    await page.wait_for_timeout(600)
    await page.evaluate("() => document.querySelectorAll('.overflow-y-auto').forEach((el) => { el.scrollTop = 0 })")


# --------------------------------------------------------------------------- where a shot ends, in CSS px

# The bottom of the last table row wholly in view, outside the docked inspector; null when every row fits.
CUT_LAST_WHOLE_ROW = """() => {
    const vh = window.innerHeight
    const rows = [...document.querySelectorAll('tbody tr')].filter((r) => !r.closest('aside') && r.getBoundingClientRect().height > 0)
    if (!rows.some((r) => r.getBoundingClientRect().bottom > vh + 0.5)) return null
    const whole = rows.filter((r) => r.getBoundingClientRect().bottom <= vh + 0.5)
    return whole.length ? Math.ceil(whole[whole.length - 1].getBoundingClientRect().bottom) : null
}"""

# The bottom of the page's own content, its padding included.
CUT_CONTENT = """() => {
    const h1 = document.querySelector('h1')
    const content = h1 && h1.closest('.overflow-y-auto') && h1.closest('.overflow-y-auto').firstElementChild
    return content ? Math.ceil(content.getBoundingClientRect().bottom) : null
}"""

# The Overview down to its charts and locations row, with a little of the page's gap under it.
CUT_OVERVIEW = """() => {
    const chart = document.querySelector('[aria-label="Traffic over time"]')
    const row = chart && chart.closest('.grid.items-stretch')
    return row ? Math.ceil(row.getBoundingClientRect().bottom + 12) : null
}"""

# A crop line moved off any sidebar entry it would cut through: the nearest clear line at most `slack` px above it
# (a content cut may lose some of the page's bottom padding), otherwise the first clear line below it.
SNAP_PAST_SIDEBAR = """([y, slack]) => {
    const nav = document.querySelector('nav')
    const side = nav && nav.parentElement
    if (!side) return y
    const boxes = [...side.querySelectorAll('a, button, div, h2, h3, span')]
        .map((el) => el.getBoundingClientRect()).filter((r) => r.height > 0 && r.height <= 48)
    const crosses = (c) => boxes.some((r) => r.top < c - 0.5 && r.bottom > c + 0.5)
    for (let c = Math.floor(y); c >= y - slack; c--) if (!crosses(c)) return c
    let c = Math.ceil(y)
    while (crosses(c) && c < y + 200) c++
    return c
}"""


@dataclass(frozen=True)
class Screen:
    """One screenshot: the page, what is done on it first and where the shot ends."""

    name: str
    route: str
    phone: bool = False
    viewport: dict[str, int] | None = None
    time_range: str = "24h"
    setup: Callable[[Page], Awaitable[None]] | None = None
    # JS answering where the shot ends, so it does not end inside a table row or on empty page. The
    # viewport shrinks to the cut, so the sidebar lays out to the frame, unless `crop` cuts the shot there
    # instead (for content that ends far up), moved to a line clear of the sidebar's entries (SNAP_PAST_SIDEBAR).
    cut: str | None = None
    crop: bool = False
    slack: int = 0

    @property
    def key(self) -> str:
        return f"{self.name}-phone" if self.phone else self.name


# The screens the site shows.
SCREENS = [
    # 24 hours of requests, success rate, latency by step, traffic and where the pool exits.
    Screen("overview", f"/projects/{P1}/overview", viewport={"width": 1440, "height": 960}, cut=CUT_OVERVIEW),
    # AWS and GCP instances, Bright Data and Oxylabs sessions and a Webshare list: weights, traffic split, usage, spend.
    Screen("connectors", f"/projects/{P1}/connectors", cut=CUT_CONTENT, crop=True, slack=20),
    # Bright Data in the docked inspector: its monthly traffic limit, the action at it, the price and the usage so far.
    Screen("connector-detail", f"/projects/{P1}/connectors?open={BRIGHT_DATA.id}",
           viewport={"width": 1440, "height": 1000}, setup=open_traffic_tab),
    # How often each provider's exits are where the vendor claims, by country, state and city.
    Screen("locations", f"/projects/{P1}/locations", time_range="30d", cut=CUT_CONTENT, crop=True, slack=20),
    # Why requests failed, by kind, vendor status and connector, with a morning burst of vendor 502s.
    Screen("failures", f"/projects/{P1}/failures", cut=CUT_LAST_WHOLE_ROW),
    # The Enterprise audit log with one change open: a traffic limit raised from 120 GB to 160 GB.
    Screen("activity", "/admin/activity", setup=open_limit_change, cut=CUT_LAST_WHOLE_ROW),
    # The provider catalog: the shipped vendors and one added as a YAML descriptor, no code and no redeploy. Without
    # the inspector open, which would squeeze the list at this width.
    Screen("providers", "/admin/providers", cut=CUT_CONTENT),
    # Phones, a TV rig and routers on WireGuard and OpenVPN tunnels, where each exits and what the pool relayed.
    Screen("devices", f"/projects/{P1}/devices", cut=CUT_CONTENT, crop=True, slack=20),
    # On a phone: the success rate, pool health and the first KPI tiles.
    Screen("overview", f"/projects/{P1}/overview", phone=True),
    # On a phone: the place pickers and the session choice for the proxy username.
    Screen("connect", f"/projects/{P1}/connect", phone=True, setup=fill_connect),
]


# --------------------------------------------------------------------------- Node and the Vite dev server

def version_of(text: str) -> tuple[int, ...]:
    """The first dotted version number in ``text``, such as (22, 22, 0) for ">=22.22.0"."""
    match = re.search(r"\d+(?:\.\d+)*", text)
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def find_node(explicit: str | None) -> str:
    """The node to run Vite with, ``explicit`` or the one on PATH, once it is as new as web/ asks."""
    node = explicit or shutil.which("node")
    if node is None:
        raise SystemExit("No node on PATH: install Node or pass --node /path/to/node.")
    wanted = json.loads((WEB_DIR / "package.json").read_text())["engines"]["node"]
    try:
        found = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"Could not run {node}: {e}") from e
    if version_of(found) < version_of(wanted):
        raise SystemExit(f"web/ needs Node {wanted} (package.json engines) and {node} is {found}. "
                         'Pass a newer one with --node, with nvm for example --node "$(nvm which 24)".')
    return node


def port_number(text: str) -> int:
    """The --port value, refused unless it is a TCP port."""
    port = int(text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{text} is not a port: pick one from 1 to 65535")
    return port


def free_port() -> int:
    """A free port from the system's ephemeral range, so never the dev server's 3000 or the API's 8000."""
    with socket.socket() as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, port)) == 0


def start_vite(node: str, port: int) -> subprocess.Popen[bytes]:
    """The Vite dev server of web/ on ``port``, once it listens."""
    vite = WEB_DIR / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.exists():
        raise SystemExit("web/node_modules is missing: run npm ci in web/ first.")
    if listening(port):
        raise SystemExit(f"Port {port} is in use: pick another with --port, or leave it out for a free one.")
    # Vite and whatever it starts get the same Node, and a process group of their own for stop_vite().
    env = {**os.environ, "PATH": f"{Path(node).parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen([node, str(vite), "--host", HOST, "--port", str(port), "--strictPort"], cwd=WEB_DIR,
                                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and proc.poll() is None:
                if listening(port):
                    return proc
                time.sleep(0.2)
        except BaseException:  # Ctrl-C while it starts: the caller has no handle to stop it with yet
            stop_vite(proc)
            raise
        stop_vite(proc)
        log.seek(0)
        raise SystemExit(f"Vite did not start on port {port}:\n{log.read().decode(errors='replace')}")


def interrupt(_signum: int, _frame: object) -> None:
    """End the run as Ctrl-C does, once: a repeat (uv passes on a kill of the group) would cut the cleanup short."""
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    raise KeyboardInterrupt


def stop_vite(proc: subprocess.Popen[bytes]) -> None:
    """Stop Vite and everything it started."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


# --------------------------------------------------------------------------- the shots

class FakeApi:
    """Answers the page's /api calls from the fixtures, and keeps what went wrong for the summary."""

    def __init__(self) -> None:
        self.unhandled: set[str] = set()
        self.problems: list[str] = []
        self.last_call = 0.0

    async def answer(self, route: Route, request: Request) -> None:
        url = urlparse(request.url)
        status, body = handle(request.method, url.path.removeprefix("/api/v1"), url.query)
        if status == 404:
            self.unhandled.add(f"{request.method} {url.path}")
        self.last_call = time.monotonic()
        # The UI offers a reload when an API names another release than its own.
        await route.fulfill(status=status, json=body, headers={"X-Octoprox-Version": VERSION})


LOADING = "img.animate-logo-loader-flow, .animate-pulse, [aria-busy='true']"


async def settle(page: Page, api: FakeApi, linger_ms: int, label: str) -> None:
    """Wait until no loader shows and no API call came for a moment, then ``linger_ms`` for the charts to draw."""
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if time.monotonic() - api.last_call > 0.8 and await page.locator(LOADING).count() == 0:
            break
        await page.wait_for_timeout(200)
    else:
        api.problems.append(f"{label}: still loading after 25 s")
    await page.wait_for_timeout(linger_ms)


def storage_script(screen: Screen, theme: str) -> str:
    """Signs the page in and sets the choices the UI remembers, before the app starts."""
    seed = {
        "octoprox_token": "demo-session-token",
        "octoprox_theme": theme,
        "octoprox_selected_project": P1,
        "octoprox_time_range": screen.time_range,
        "octoprox_sidebar_collapsed": "0",
        "octoprox_connect_snippet": "curl",
    }
    # Not on the about:blank a new page starts on, whose storage is off limits.
    return (f"if (location.protocol.startsWith('http')) "
            f"for (const [k, v] of Object.entries({json.dumps(seed)})) localStorage.setItem(k, v)")


async def capture(browser: Browser, api: FakeApi, base: str, screen: Screen, theme: str) -> bytes:
    """One screen in one theme, as PNG."""
    label = f"{screen.key}-{theme}"
    print(label, flush=True)
    device = PHONE if screen.phone else DESKTOP
    viewport = dict(screen.viewport or device["viewport"])
    problems_before = len(api.problems)

    def on_console(message: ConsoleMessage) -> None:
        if message.type == "error":
            api.problems.append(f"{label}: console error: {message.text}")

    context = await browser.new_context(**{**device, "viewport": viewport}, device_scale_factor=SCALE, locale=LOCALE,
                                        timezone_id=TIMEZONE, color_scheme=theme, reduced_motion="reduce")
    try:
        await context.add_init_script(storage_script(screen, theme))
        await context.route(f"{base}/api/**", api.answer)
        page = await context.new_page()
        page.on("console", on_console)
        page.on("pageerror", lambda error: api.problems.append(f"{label}: page error: {error}"))
        await page.clock.set_fixed_time(NOW)
        await page.goto(base + screen.route, wait_until="domcontentloaded")
        await settle(page, api, 600, label)
        if screen.setup:
            await screen.setup(page)
        await settle(page, api, 1800, label)
        clip = None
        cut = await page.evaluate(screen.cut) if screen.cut else None
        if cut and cut < viewport["height"]:
            if screen.crop:
                cut = min(viewport["height"], await page.evaluate(SNAP_PAST_SIDEBAR, [cut, screen.slack]))
                clip = {"x": 0, "y": 0, "width": viewport["width"], "height": cut}
            else:
                await page.set_viewport_size({**viewport, "height": cut})
                await settle(page, api, 600, label)
        png = await page.screenshot(animations="disabled", caret="hide", clip=clip)
    finally:
        # The browser may be gone already (a kill sent to the whole process group); keep the error that got us here.
        with contextlib.suppress(PlaywrightError):
            await context.close()
    for problem in api.problems[problems_before:]:
        print(f"  ! {problem}")
    return png


def write_webp(png: bytes, path: Path) -> str:
    """Write ``png`` to ``path`` as WebP; what was written, for the summary."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    image.save(path, "WEBP", **WEBP)
    return f"{path.relative_to(ROOT)}: {image.width}x{image.height}, {path.stat().st_size / 1024:.0f} KB"


async def capture_all(base: str, screens: list[Screen], themes: list[str], api: FakeApi) -> None:
    """Take every screen in every theme and write each to OUT_DIR."""
    writes = []
    async with async_playwright() as playwright:
        # Ctrl-C is handled here, closing the browser on the way out; Playwright's own handler would close it first,
        # under the shot being taken, and end the run with a TargetClosedError instead.
        browser = await playwright.chromium.launch(handle_sigint=False)
        try:
            for screen in screens:
                for theme in themes:
                    png = await capture(browser, api, base, screen, theme)
                    # Encoding takes seconds and releases the GIL, so it runs while the next shots are taken.
                    path = OUT_DIR / f"{screen.key}-{theme}.webp"
                    writes.append(asyncio.create_task(asyncio.to_thread(write_webp, png, path)))
        finally:
            with contextlib.suppress(PlaywrightError):
                await browser.close()
    print("Wrote")
    for written in await asyncio.gather(*writes):
        print(f"  {written}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated screens (default: all): " + ", ".join(s.key for s in SCREENS))
    parser.add_argument("--themes", default=",".join(THEMES), help="comma-separated themes (default: light,dark)")
    parser.add_argument("--port", type=port_number, help="the port of the Vite dev server (default: a free one)")
    parser.add_argument("--node", help="the node binary to run Vite with (default: node on PATH)")
    args = parser.parse_args()
    keys = [s.key for s in SCREENS]
    only = args.only.split(",") if args.only else keys
    themes = args.themes.split(",")
    if unknown := set(only) - set(keys):
        parser.error(f"no screen {', '.join(sorted(unknown))}; the screens are {', '.join(keys)}")
    if unknown := set(themes) - set(THEMES):
        parser.error(f"no theme {', '.join(sorted(unknown))}; the themes are {', '.join(THEMES)}")

    node = find_node(args.node)
    port = args.port or free_port()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    api = FakeApi()
    # Vite has a session of its own, which Ctrl-C does not reach, so this script stops it. A kill or a closed
    # terminal would end the script before it could: they raise KeyboardInterrupt like Ctrl-C instead.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)
    vite = start_vite(node, port)
    try:
        print(f"Started Vite on http://{HOST}:{port} with {node}", flush=True)
        asyncio.run(capture_all(f"http://{HOST}:{port}", [s for s in SCREENS if s.key in only], themes, api))
    finally:
        stop_vite(vite)
        print("Stopped Vite")

    if api.unhandled:
        print("API calls the fixtures do not answer (they got a 404):")
        for call in sorted(api.unhandled):
            print(f"  {call}")
    if api.problems:
        print(f"{len(api.problems)} problems, listed under their shots above")
    return 1 if api.unhandled or api.problems else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted")
        raise SystemExit(130) from None
