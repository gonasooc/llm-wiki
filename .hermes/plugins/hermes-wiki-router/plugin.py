from __future__ import annotations

import asyncio
import logging

import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
logger = logging.getLogger(__name__)


def extract_video_ids(text: str) -> list[str]:
    """Return unique valid YouTube video IDs in first-seen order."""
    found: list[str] = []
    for raw in _URL.findall(text or ""):
        candidate = raw.rstrip(".,;:!?)]}\"'")
        try:
            parsed = urlparse(candidate)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        video_id = None
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/", 1)[0]
        elif host in _YOUTUBE_HOSTS:
            path_parts = [part for part in parsed.path.split("/") if part]
            if parsed.path == "/watch":
                video_id = (parse_qs(parsed.query).get("v") or [None])[0]
            elif len(path_parts) >= 2 and path_parts[0] in {
                "shorts",
                "live",
                "embed",
            }:
                video_id = path_parts[1]
        if isinstance(video_id, str) and _VIDEO_ID.fullmatch(video_id):
            if video_id not in found:
                found.append(video_id)
    return found


class ThreadStore:
    """SQLite mapping from scoped YouTube identity to Discord thread ID."""

    def __init__(self, path: str | Path, *, enforce_permissions: bool = False):
        self.path = Path(path).expanduser()
        self.enforce_permissions = enforce_permissions
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.enforce_permissions:
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS youtube_threads (
                    profile TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    parent_channel_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_validated_at INTEGER NOT NULL,
                    PRIMARY KEY (profile, guild_id, parent_channel_id, video_id)
                )
                """
            )
        if self.enforce_permissions:
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def get(
        self,
        profile: str,
        guild_id: str,
        parent_channel_id: str,
        video_id: str,
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT thread_id
                FROM youtube_threads
                WHERE profile = ? AND guild_id = ?
                  AND parent_channel_id = ? AND video_id = ?
                """,
                (profile, guild_id, parent_channel_id, video_id),
            ).fetchone()
        return str(row[0]) if row else None

    def upsert(
        self,
        profile: str,
        guild_id: str,
        parent_channel_id: str,
        video_id: str,
        thread_id: str,
    ) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO youtube_threads (
                    profile, guild_id, parent_channel_id, video_id,
                    thread_id, created_at, updated_at, last_validated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile, guild_id, parent_channel_id, video_id)
                DO UPDATE SET
                    thread_id = excluded.thread_id,
                    updated_at = excluded.updated_at,
                    last_validated_at = excluded.last_validated_at
                """,
                (
                    profile,
                    guild_id,
                    parent_channel_id,
                    video_id,
                    thread_id,
                    now,
                    now,
                    now,
                ),
            )


class Router:
    def __init__(
        self,
        parent_channel_id: str,
        store: ThreadStore | None,
        actions: Any,
    ):
        self.parent_channel_id = str(parent_channel_id).strip()
        self.store = store
        self.actions = actions
        self._locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
        self._lock_users: dict[tuple[str, str, str, str], int] = {}

    async def route(self, event: Any) -> dict[str, Any] | None:
        source = getattr(event, "source", None)
        platform_value = getattr(source, "platform", None)
        platform = getattr(platform_value, "value", platform_value)
        if platform != "discord":
            return None
        if getattr(source, "chat_type", None) == "thread":
            return None
        if str(getattr(source, "chat_id", "")) != self.parent_channel_id:
            return None
        metadata = dict(getattr(event, "metadata", {}) or {})
        if metadata.get("discord.explicit_bot_mention") is not True:
            return None
        if self.store is None:
            return {"action": "skip", "reason": "mapping-store-unavailable"}

        video_ids = extract_video_ids(getattr(event, "text", "") or "")
        if len(video_ids) > 1:
            return None
        if not getattr(event, "message_id", None):
            return {"action": "skip", "reason": "missing-trigger-message-id"}

        profile = str(getattr(source, "profile", None) or "default")
        guild_id = str(
            getattr(source, "scope_id", None)
            or getattr(source, "guild_id", None)
            or ""
        )
        if not guild_id:
            return {"action": "skip", "reason": "missing-discord-guild-id"}

        if video_ids:
            video_id = video_ids[0]
            key = (profile, guild_id, self.parent_channel_id, video_id)
            lock = self._locks.setdefault(key, asyncio.Lock())
            self._lock_users[key] = self._lock_users.get(key, 0) + 1
            try:
                async with lock:
                    try:
                        existing = await asyncio.to_thread(self.store.get, *key)
                    except Exception:
                        logger.exception("YouTube thread mapping lookup failed")
                        return {"action": "skip", "reason": "mapping-read-failed"}
                    return await self._ensure_and_route(
                        event,
                        name=f"YouTube {video_id}",
                        existing_thread_id=existing,
                        mapping_key=key,
                    )
            finally:
                remaining = self._lock_users.get(key, 1) - 1
                if remaining <= 0:
                    self._lock_users.pop(key, None)
                    self._locks.pop(key, None)
                else:
                    self._lock_users[key] = remaining

        return await self._ensure_and_route(
            event,
            name=(getattr(event, "text", "") or "Hermes")[:80],
            existing_thread_id=None,
            mapping_key=None,
        )

    async def _ensure_and_route(
        self,
        event: Any,
        *,
        name: str,
        existing_thread_id: str | None,
        mapping_key: tuple[str, str, str, str] | None,
    ) -> dict[str, Any]:
        result = await self.actions.ensure_message_thread(
            platform="discord",
            parent_chat_id=self.parent_channel_id,
            message_id=str(event.message_id),
            name=name,
            expected_guild_id=str(event.source.scope_id),
            existing_thread_id=existing_thread_id,
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            return {"action": "skip", "reason": "thread-route-failed"}

        thread_id = str(result.get("thread_id", "")).strip()
        if not thread_id:
            return {"action": "skip", "reason": "empty-thread-route"}
        route_token = result.get("route_token")
        if not isinstance(route_token, str) or not route_token:
            return {"action": "skip", "reason": "missing-route-token"}
        created = result.get("created") is True
        persistence_degraded = False
        if mapping_key is not None:
            try:
                await asyncio.to_thread(self.store.upsert, *mapping_key, thread_id)
            except Exception:
                persistence_degraded = True
                logger.exception(
                    "YouTube thread mapping persistence failed after route creation"
                )

        return {
            "action": "reroute",
            "chat_id": thread_id,
            "thread_id": thread_id,
            "parent_chat_id": self.parent_channel_id,
            "chat_type": "thread",
            "scope_id": str(event.source.scope_id),
            "route_token": route_token,
            "auto_thread_created": created,
            "auto_thread_initial_name": name,
            "reason": (
                "youtube-video-dedupe-persistence-degraded"
                if persistence_degraded
                else "youtube-video-dedupe"
                if mapping_key
                else "explicit-mention-thread"
            ),
        }


def _default_db_path() -> Path:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    return home / "plugin-data" / "hermes-wiki-router" / "youtube_threads.db"


def register(ctx) -> None:
    parent_channel_id = str(ctx.get_config("parent_channel_id", "")).strip()
    if not parent_channel_id.isdigit():
        logger.warning(
            "hermes-wiki-router is inert until settings.parent_channel_id is a numeric Discord channel ID"
        )
        parent_channel_id = ""
    db_path = str(ctx.get_config("db_path", "")).strip()
    try:
        store = ThreadStore(
            db_path or _default_db_path(),
            enforce_permissions=not bool(db_path),
        )
    except Exception:
        logger.exception(
            "hermes-wiki-router mapping store unavailable; routing fails closed"
        )
        store = None
    router = Router(parent_channel_id, store, ctx.platform_actions)

    async def _route(event, **kwargs):
        return await router.route(event)

    ctx.register_hook("gateway_message_route", _route)
