"""Discord alerts for names that came back free.

The webhook is posted to directly, never through the user's proxy list. A
webhook URL is a capability: it authenticates the post *and* names the channel,
so routing one through a pasted proxy would hand whoever runs that proxy the
ability to post into the channel.

Names are digested rather than notified one at a time. A scan can turn up
hundreds of free handles and Discord allows ten embeds per message, so a message
per name would be a rate limit wearing a notification's clothes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

from config import (
    ALERT_AVATAR,
    ALERT_BATCH,
    ALERT_MAX_MESSAGES,
    ALERT_MIN_INTERVAL,
    ALERT_PING,
    ALERT_TEMPLATE,
    ALERT_USERNAME,
    ALERT_WEBHOOK,
    SETTINGS_PATH,
    SSL_CONTEXT,
)

log = logging.getLogger("bahs.alerts")

# Discord refuses a payload carrying no embed title and no description, so the
# template is the embed body and the title is generated.
EMBED_COLORS = {
    "discord": 0x5865F2,
    "roblox": 0xE2231A,
    "minecraft": 0x62B47A,
    "telegram": 0x2AABEE,
    "instagram": 0xE1306C,
    "tiktok": 0x25F4EE,
    "x": 0xE7E9EA,
    "github": 0x8B949E,
    "youtube": 0xFF0000,
    "guns.lol": 0xF97316,
}
DEFAULT_COLOR = 0x22C55E

# The knobs the UI may change. Anything not in here is environment-only.
EDITABLE = ("webhook", "template", "username", "avatar", "ping", "enabled")


class AlertSettings:
    """Discord alert settings: environment defaults, overridden from the UI.

    The environment is the base and a stored value wins only when it is set, so
    the service works from Railway variables alone and the UI is an optional
    layer on top rather than a requirement.
    """

    def __init__(self, path: str = SETTINGS_PATH) -> None:
        self.path = path
        self._stored: dict[str, object] = {}

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (ValueError, OSError) as exc:
            log.warning("could not read %s (%s); using environment only", self.path, exc)
            return
        if isinstance(data, dict):
            self._stored = {k: v for k, v in data.items() if k in EDITABLE}

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        temp = f"{self.path}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self._stored, handle)
        os.replace(temp, self.path)

    def get(self) -> dict[str, object]:
        """The effective settings, with the webhook URL never logged."""
        merged: dict[str, object] = {
            "webhook": ALERT_WEBHOOK,
            "template": ALERT_TEMPLATE,
            "username": ALERT_USERNAME,
            "avatar": ALERT_AVATAR,
            "ping": ALERT_PING,
            "batch": ALERT_BATCH,
            "min_interval": ALERT_MIN_INTERVAL,
            "max_messages": ALERT_MAX_MESSAGES,
        }
        for key in EDITABLE:
            if key in self._stored:
                merged[key] = self._stored[key]
        # A webhook URL is the whole of the configuration, so having one means
        # alerts are on. Storing `enabled: false` is the only way to set a webhook
        # and stay quiet -- reading the default off the environment instead left
        # a webhook pasted into the UI with alerting silently off.
        if "enabled" not in self._stored:
            merged["enabled"] = bool(merged.get("webhook"))
        else:
            merged["enabled"] = bool(merged.get("enabled")) and bool(merged.get("webhook"))
        return merged

    def update(self, patch: dict) -> dict[str, object]:
        for key in EDITABLE:
            if key in patch:
                value = patch[key]
                self._stored[key] = value.strip() if isinstance(value, str) else value
        # A patch that sets a webhook and says nothing about `enabled` is someone
        # configuring alerts to work, not someone asking for a stored-but-silent
        # webhook. Without this, a webhook set after alerts had once been turned
        # off stayed quiet with no visible reason why.
        if patch.get("webhook") and "enabled" not in patch:
            self._stored["enabled"] = True
        self.save()
        return self.get()


class Alerter:
    """Collects free names and posts them to the webhook in batches."""

    def __init__(self, settings: dict[str, object]) -> None:
        self.settings = settings
        self.enabled = bool(settings.get("enabled")) and bool(settings.get("webhook"))
        self._buffer: list[dict[str, str]] = []
        self._messages = 0
        self._last_post = 0.0
        self._client: httpx.AsyncClient | None = None
        self.sent = 0
        self.failed = 0
        self.dropped = 0

    # --- lifecycle --------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # No proxy: see the module docstring.
            self._client = httpx.AsyncClient(timeout=15.0, verify=SSL_CONTEXT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    # --- posting ----------------------------------------------------------
    def offer(self, username: str, platform: str, detail: str = "") -> None:
        """Queue a free name. Cheap and non-blocking; nothing is sent here."""
        if not self.enabled:
            return
        self._buffer.append({"username": username, "platform": platform, "detail": detail})

    @property
    def pending(self) -> int:
        """How many names are queued but not yet posted."""
        return len(self._buffer)

    def _embed(self, item: dict[str, str]) -> dict:
        template = str(self.settings.get("template") or ALERT_TEMPLATE)
        try:
            text = template.format(
                username=item["username"], platform=item["platform"], detail=item.get("detail", "")
            )
        except (KeyError, IndexError, ValueError):
            # A template the user broke must not take the run down with it.
            text = f"{item['platform']} username available: `{item['username']}`"
        embed: dict = {
            "description": text,
            "color": EMBED_COLORS.get(item["platform"], DEFAULT_COLOR),
        }
        if item.get("detail"):
            embed["footer"] = {"text": item["detail"][:200]}
        return embed

    async def flush(self, force: bool = False) -> None:
        """Post whatever is buffered, in batches, respecting the interval."""
        while self._buffer:
            if not force and self._messages >= int(self.settings.get("max_messages") or 1):
                self.dropped += len(self._buffer)
                self._buffer.clear()
                return
            batch = self._buffer[: int(self.settings.get("batch") or 10)]
            del self._buffer[: len(batch)]
            await self._post(batch)

    async def _post(self, batch: list[dict[str, str]]) -> None:
        url = str(self.settings.get("webhook") or "")
        if not url:
            return
        gap = float(self.settings.get("min_interval") or 0.0)
        wait = gap - (time.monotonic() - self._last_post)
        if wait > 0:
            await asyncio.sleep(wait)
        payload: dict = {"embeds": [self._embed(item) for item in batch]}
        if self.settings.get("username"):
            payload["username"] = self.settings["username"]
        if self.settings.get("avatar"):
            payload["avatar_url"] = self.settings["avatar"]
        ping = str(self.settings.get("ping") or "")
        if ping and self._messages == 0:
            # Discord only renders a ping that is in `content`, never in an embed.
            payload["content"] = ping
        for attempt in range(2):
            try:
                resp = await self._http().post(url, json=payload)
            except Exception as exc:
                self.failed += 1
                log.warning("webhook post failed: %s", type(exc).__name__)
                return
            self._last_post = time.monotonic()
            if resp.status_code in (200, 204):
                self._messages += 1
                self.sent += len(batch)
                return
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(_retry_after(resp) or 1.0)
                continue
            self.failed += 1
            log.warning("webhook returned %d: %s", resp.status_code, resp.text[:200])
            return


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        value = resp.json().get("retry_after")
    except ValueError:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 300:
        return float(value)
    header = resp.headers.get("retry-after")
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    return None


async def post_test(settings: dict[str, object]) -> dict:
    """Send one message so the user can see the webhook actually land."""
    alerter = Alerter({**settings, "enabled": True})
    if not alerter.enabled:
        return {"ok": False, "error": "no webhook url set"}
    alerter.offer("example", "discord", "test alert from bahs")
    try:
        await alerter.flush(force=True)
    finally:
        await alerter.aclose()
    return {
        "ok": alerter.failed == 0 and alerter.sent > 0,
        "sent": alerter.sent,
        "failed": alerter.failed,
    }
