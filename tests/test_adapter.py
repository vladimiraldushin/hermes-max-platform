import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

HERMES_SRC = Path(os.environ.get("HERMES_AGENT_SRC", "/Users/aldushin/.hermes/hermes-agent"))
if HERMES_SRC.exists():
    sys.path.insert(0, str(HERMES_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402


def test_verify_secret_matches_raw_header_not_hmac():
    assert adapter._verify_secret(b'{"x":1}', "secret-123", "secret-123") is True
    assert adapter._verify_secret(b'{"x":1}', "secret-123", "different") is False
    assert adapter._verify_secret(b'{"x":1}', "", None) is True


def test_env_enablement_seeds_extra(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    monkeypatch.setenv("MAX_WEBHOOK_PORT", "8646")
    monkeypatch.setenv("MAX_WEBHOOK_PATH", "/max/webhook")
    monkeypatch.setenv("MAX_ALLOWED_USERS", "1, 2")
    monkeypatch.setenv("MAX_HOME_CHANNEL", "user:1")

    extra = adapter._env_enablement()

    assert extra["token"] == "tok"
    assert extra["port"] == 8646
    assert extra["path"] == "/max/webhook"
    assert extra["allowed_users"] == ["1", "2"]
    assert extra["home_channel"]["chat_id"] == "user:1"


@pytest.mark.asyncio
async def test_build_message_created_event_dm():
    cfg = PlatformConfig(enabled=True, extra={"token": "tok"})
    max_adapter = adapter.MaxAdapter(cfg)

    event = await max_adapter._build_event(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 42, "name": "Vladimir"},
                "body": {"mid": "mid-1", "text": "Привет"},
            },
        }
    )

    assert event is not None
    assert event.text == "Привет"
    assert event.source.chat_id == "user:42"
    assert event.message_id == "mid-1"


@pytest.mark.asyncio
async def test_build_message_created_event_group():
    cfg = PlatformConfig(enabled=True, extra={"token": "tok"})
    max_adapter = adapter.MaxAdapter(cfg)

    event = await max_adapter._build_event(
        {
            "update_type": "message_created",
            "chat": {"chat_id": 777, "title": "Group"},
            "message": {
                "sender": {"user_id": 42, "name": "Vladimir"},
                "recipient": {"chat_id": 777},
                "body": {"mid": "mid-2", "text": "В группу"},
            },
        }
    )

    assert event is not None
    assert event.source.chat_id == "chat:777"
    assert event.source.chat_type == "group"


def test_register_platform_includes_runtime_hooks():
    class FakeCtx:
        def __init__(self):
            self.kwargs = None
            self.skills = []

        def register_platform(self, **kwargs):
            self.kwargs = kwargs

        def register_skill(self, name, path, description=""):
            self.skills.append((name, Path(path), description))

    ctx = FakeCtx()
    adapter.register(ctx)

    assert ctx.kwargs["name"] == "max"
    assert ctx.kwargs["env_enablement_fn"] is adapter._env_enablement
    assert ctx.kwargs["cron_deliver_env_var"] == "MAX_HOME_CHANNEL"
    assert ctx.kwargs["standalone_sender_fn"] is adapter._standalone_send
    assert ctx.skills and ctx.skills[0][0] == "max-gateway"


@pytest.mark.asyncio
async def test_standalone_send_posts_to_max(monkeypatch):
    calls = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"message_id": "m-1"}}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, params=None, json=None, headers=None):
            calls.update(url=url, params=params, json=json, headers=headers)
            return FakeResponse()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(AsyncClient=FakeClient))

    cfg = PlatformConfig(enabled=True, extra={"token": "tok"})
    result = await adapter._standalone_send(cfg, "user:42", "hello")

    assert result == {"success": True, "message_id": "m-1"}
    assert calls["url"] == "https://platform-api.max.ru/messages"
    assert calls["params"] == {"user_id": "42"}
    assert calls["json"]["text"] == "hello"
    assert calls["json"]["format"] == "markdown"
    assert calls["headers"]["Authorization"] == "tok"
