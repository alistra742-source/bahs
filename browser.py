"""Small isolated browser surface for the Kanha page.

Only the allow-listed actions below are exposed. This module never evaluates model supplied
JavaScript or shell commands and the browser context has downloads disabled.
"""
from __future__ import annotations

import asyncio
import base64
import re
import threading
import uuid
from typing import Optional

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
except ImportError:  # pragma: no cover - deployment may intentionally omit browser support
    async_playwright = None
    Browser = BrowserContext = Page = object


_URL_RE = re.compile(r"^https?://[^\s<>]+$", re.I)
_SELECTOR_MAX = 240
_MAX_SESSIONS = 16


class BrowserUnavailable(RuntimeError):
    pass


class BrowserManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}

    def _ensure_available(self) -> None:
        if async_playwright is None:
            raise BrowserUnavailable("browser support is not installed; install Playwright and Chromium")

    def _get(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("unknown or expired browser session")
        return session

    def _run(self, session_id: str, operation):
        session = self._get(session_id)
        return session["loop"].run_until_complete(operation(session["page"]))

    def start(self, url: str = "about:blank") -> dict:
        self._ensure_available()
        if url != "about:blank" and not _URL_RE.fullmatch(url):
            raise ValueError("Start accepts only an http(s) URL")
        with self._lock:
            if len(self._sessions) >= _MAX_SESSIONS:
                raise RuntimeError("browser session limit reached")
        loop = asyncio.new_event_loop()
        playwright = loop.run_until_complete(async_playwright().start())
        browser = loop.run_until_complete(playwright.chromium.launch(headless=True, args=["--no-sandbox"]))
        context = loop.run_until_complete(browser.new_context(accept_downloads=False))
        page = loop.run_until_complete(context.new_page())
        if url != "about:blank":
            loop.run_until_complete(page.goto(url, wait_until="domcontentloaded", timeout=20000))
        session_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._sessions[session_id] = {"loop": loop, "playwright": playwright,
                                          "browser": browser, "context": context, "page": page}
        return self.status(session_id)

    def status(self, session_id: str) -> dict:
        session = self._get(session_id)
        page = session["page"]
        return {"session": session_id, "url": page.url, "title": self._run(session_id, lambda p: p.title())}

    def enter(self, session_id: str, url: str) -> dict:
        if not _URL_RE.fullmatch(url):
            raise ValueError("Enter accepts only an http(s) URL")
        return self._run(session_id, lambda page: page.goto(url, wait_until="domcontentloaded", timeout=20000) and self.status(session_id))

    def click(self, session_id: str, selector: str) -> dict:
        selector = str(selector or "").strip()
        if not selector or len(selector) > _SELECTOR_MAX or "javascript:" in selector.lower():
            raise ValueError("Click needs a normal CSS selector")
        async def action(page):
            await page.locator(selector).first.click(timeout=10000)
            await page.wait_for_timeout(250)
        self._run(session_id, action)
        return self.status(session_id)

    def screenshot(self, session_id: str) -> dict:
        data = self._run(session_id, lambda page: page.screenshot(type="png", full_page=False))
        return {**self.status(session_id), "mime": "image/png", "data": base64.b64encode(data).decode("ascii")}

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            session["loop"].run_until_complete(session["context"].close())
            session["loop"].run_until_complete(session["browser"].close())
            session["loop"].run_until_complete(session["playwright"].stop())
            session["loop"].close()


browser_manager = BrowserManager()
