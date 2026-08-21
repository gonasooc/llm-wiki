import asyncio
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".hermes" / "plugins" / "hermes-wiki-router" / "plugin.py"
MANIFEST = PLUGIN.with_name("plugin.yaml")
README = PLUGIN.with_name("README.md")


def _load_module():
    spec = importlib.util.spec_from_file_location("hermes_wiki_router", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(*, text, mentioned=True, chat_type="group"):
    return SimpleNamespace(
        text=text,
        message_id="777",
        metadata={"discord.explicit_bot_mention": mentioned},
        source=SimpleNamespace(
            platform="discord",
            chat_id="555",
            chat_type=chat_type,
            scope_id="guild-1",
            profile="default",
        ),
    )


def test_extract_video_ids_normalizes_supported_urls():
    module = _load_module()

    ids = module.extract_video_ids(
        " ".join(
            [
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&utm_source=x",
                "https://youtu.be/dQw4w9WgXcQ?t=10",
                "https://youtube.com/shorts/dQw4w9WgXcQ",
                "https://youtube.com/live/dQw4w9WgXcQ?feature=share",
                "https://youtube.com/embed/dQw4w9WgXcQ",
            ]
        )
    )

    assert ids == ["dQw4w9WgXcQ"]
    assert module.extract_video_ids("https://youtu.be/not-valid") == []


def test_manifest_and_deployment_prerequisites_are_explicit():
    manifest = MANIFEST.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    assert "gateway.platform_actions" in manifest
    assert "gateway_message_route" in manifest
    assert "parent_channel_id:" in manifest
    assert "parent_channel_id:\n    type: int" in manifest
    assert '$PLUGIN_HOME/plugins/hermes-wiki-router' in readme
    assert "hermes plugins enable hermes-wiki-router" in readme
    assert "hermes config set discord.no_thread_channels" in readme
    assert (
        "hermes config set discord.auto_thread_mentions_in_free_response false"
        in readme
    )


def test_register_without_parent_channel_is_inert_not_fatal(tmp_path):
    module = _load_module()
    registered = {}

    class Context:
        platform_actions = SimpleNamespace(ensure_message_thread=AsyncMock())

        def get_config(self, key, default=None):
            if key == "db_path":
                return str(tmp_path / "threads.db")
            return default

        def register_hook(self, name, callback):
            registered[name] = callback

    module.register(Context())

    assert "gateway_message_route" in registered


def test_default_db_path_honors_profile_home_override(tmp_path, monkeypatch):
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    monkeypatch.delenv("HERMES_HOME", raising=False)
    token = set_hermes_home_override(str(tmp_path))
    try:
        module = _load_module()
        assert module._default_db_path() == (
            tmp_path
            / "plugin-data"
            / "hermes-wiki-router"
            / "youtube_threads.db"
        )
    finally:
        reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_sqlite_initialization_failure_registers_fail_closed_hook(
    tmp_path, monkeypatch
):
    module = _load_module()
    registered = {}
    actions = SimpleNamespace(ensure_message_thread=AsyncMock())

    class Context:
        platform_actions = actions

        def get_config(self, key, default=None):
            return {
                "parent_channel_id": 555,
                "db_path": str(tmp_path / "threads.db"),
            }.get(key, default)

        def register_hook(self, name, callback):
            registered[name] = callback

    def fail_store(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(module, "ThreadStore", fail_store)
    module.register(Context())

    result = await registered["gateway_message_route"](
        _event(text="https://youtu.be/dQw4w9WgXcQ")
    )

    assert result == {"action": "skip", "reason": "mapping-store-unavailable"}
    actions.ensure_message_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmentioned_or_thread_message_is_not_routed(tmp_path):
    module = _load_module()
    actions = SimpleNamespace(ensure_message_thread=AsyncMock())
    router = module.Router("555", module.ThreadStore(tmp_path / "threads.db"), actions)

    assert await router.route(_event(text="https://youtu.be/dQw4w9WgXcQ", mentioned=False)) is None
    assert await router.route(
        _event(text="https://youtu.be/dQw4w9WgXcQ", chat_type="thread")
    ) is None
    actions.ensure_message_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_non_youtube_message_creates_fresh_thread(tmp_path):
    module = _load_module()
    actions = SimpleNamespace(
        ensure_message_thread=AsyncMock(
            return_value={
                "ok": True,
                "thread_id": "889",
                "created": True,
                "route_token": "route-token-889",
            }
        )
    )
    router = module.Router("555", module.ThreadStore(tmp_path / "threads.db"), actions)

    result = await router.route(_event(text="일반 질문입니다"))

    assert result["chat_id"] == "889"
    assert result["reason"] == "explicit-mention-thread"
    actions.ensure_message_thread.assert_awaited_once_with(
        platform="discord",
        parent_chat_id="555",
        message_id="777",
        name="일반 질문입니다",
        expected_guild_id="guild-1",
        existing_thread_id=None,
    )


@pytest.mark.asyncio
async def test_multiple_video_ids_are_left_for_agent_input_gate(tmp_path):
    module = _load_module()
    actions = SimpleNamespace(ensure_message_thread=AsyncMock())
    router = module.Router("555", module.ThreadStore(tmp_path / "threads.db"), actions)

    result = await router.route(
        _event(
            text=(
                "https://youtu.be/dQw4w9WgXcQ "
                "https://youtu.be/afSgBNwmZrQ"
            )
        )
    )

    assert result is None
    actions.ensure_message_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_link_creates_and_persists_thread(tmp_path):
    module = _load_module()
    actions = SimpleNamespace(
        ensure_message_thread=AsyncMock(
            return_value={
                "ok": True,
                "thread_id": "888",
                "created": True,
                "route_token": "route-token-888",
            }
        )
    )
    store = module.ThreadStore(tmp_path / "threads.db")
    router = module.Router("555", store, actions)

    result = await router.route(_event(text="https://youtu.be/dQw4w9WgXcQ"))

    assert result["action"] == "reroute"
    assert result["chat_id"] == "888"
    assert result["thread_id"] == "888"
    assert result["parent_chat_id"] == "555"
    assert result["chat_type"] == "thread"
    assert result["scope_id"] == "guild-1"
    assert result["route_token"] == "route-token-888"
    assert "suppress_reply_anchor" not in result
    assert result["auto_thread_created"] is True
    actions.ensure_message_thread.assert_awaited_once_with(
        platform="discord",
        parent_chat_id="555",
        message_id="777",
        name="YouTube dQw4w9WgXcQ",
        expected_guild_id="guild-1",
        existing_thread_id=None,
    )
    assert store.get("default", "guild-1", "555", "dQw4w9WgXcQ") == "888"


@pytest.mark.asyncio
async def test_duplicate_link_reuses_persisted_thread_and_suppresses_anchor(tmp_path):
    module = _load_module()
    store = module.ThreadStore(tmp_path / "threads.db")
    store.upsert("default", "guild-1", "555", "dQw4w9WgXcQ", "888")
    actions = SimpleNamespace(
        ensure_message_thread=AsyncMock(
            return_value={
                "ok": True,
                "thread_id": "888",
                "created": False,
                "route_token": "route-token-888",
            }
        )
    )
    router = module.Router("555", store, actions)

    result = await router.route(_event(text="https://youtube.com/watch?v=dQw4w9WgXcQ extra"))

    assert result["chat_id"] == "888"
    assert result["scope_id"] == "guild-1"
    assert "suppress_reply_anchor" not in result
    assert result["auto_thread_created"] is False
    actions.ensure_message_thread.assert_awaited_once_with(
        platform="discord",
        parent_chat_id="555",
        message_id="777",
        name="YouTube dQw4w9WgXcQ",
        expected_guild_id="guild-1",
        existing_thread_id="888",
    )


@pytest.mark.asyncio
async def test_stale_mapping_is_replaced_atomically(tmp_path):
    module = _load_module()
    store = module.ThreadStore(tmp_path / "threads.db")
    store.upsert("default", "guild-1", "555", "dQw4w9WgXcQ", "old")
    actions = SimpleNamespace(
        ensure_message_thread=AsyncMock(
            return_value={
                "ok": True,
                "thread_id": "999",
                "created": True,
                "route_token": "route-token-999",
                "replaced_unavailable_thread_id": "old",
            }
        )
    )
    router = module.Router("555", store, actions)

    result = await router.route(_event(text="https://youtu.be/dQw4w9WgXcQ"))

    assert result["chat_id"] == "999"
    assert store.get("default", "guild-1", "555", "dQw4w9WgXcQ") == "999"
    reopened = module.ThreadStore(tmp_path / "threads.db")
    assert reopened.get("default", "guild-1", "555", "dQw4w9WgXcQ") == "999"


@pytest.mark.asyncio
async def test_store_lookup_failure_skips_before_platform_action(tmp_path):
    module = _load_module()

    class BrokenStore:
        def get(self, *args):
            raise sqlite3.OperationalError("read failed")

        def upsert(self, *args):
            raise AssertionError("must not write")

    actions = SimpleNamespace(ensure_message_thread=AsyncMock())
    router = module.Router("555", BrokenStore(), actions)

    result = await router.route(_event(text="https://youtu.be/dQw4w9WgXcQ"))

    assert result == {"action": "skip", "reason": "mapping-read-failed"}
    actions.ensure_message_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_write_failure_still_routes_to_created_thread(tmp_path):
    module = _load_module()

    class BrokenStore:
        def get(self, *args):
            return None

        def upsert(self, *args):
            raise sqlite3.OperationalError("write failed")

    actions = SimpleNamespace(
        ensure_message_thread=AsyncMock(
            return_value={
                "ok": True,
                "thread_id": "888",
                "created": True,
                "route_token": "route-token-888",
            }
        )
    )
    router = module.Router("555", BrokenStore(), actions)

    result = await router.route(_event(text="https://youtu.be/dQw4w9WgXcQ"))

    assert result["action"] == "reroute"
    assert result["chat_id"] == "888"
    assert result["reason"] == "youtube-video-dedupe-persistence-degraded"


@pytest.mark.asyncio
async def test_concurrent_first_mentions_create_once_and_release_lock(tmp_path):
    module = _load_module()
    store = module.ThreadStore(tmp_path / "threads.db")
    created = 0

    async def ensure(**kwargs):
        nonlocal created
        if kwargs["existing_thread_id"] is None:
            created += 1
            await asyncio.sleep(0.01)
            return {
                "ok": True,
                "thread_id": "888",
                "created": True,
                "route_token": "route-token-created",
            }
        return {
            "ok": True,
            "thread_id": "888",
            "created": False,
            "route_token": "route-token-reused",
        }

    actions = SimpleNamespace(ensure_message_thread=AsyncMock(side_effect=ensure))
    router = module.Router("555", store, actions)

    first, second = await asyncio.gather(
        router.route(_event(text="https://youtu.be/dQw4w9WgXcQ")),
        router.route(_event(text="https://youtu.be/dQw4w9WgXcQ")),
    )

    assert first["chat_id"] == second["chat_id"] == "888"
    assert created == 1
    assert router._locks == {}
