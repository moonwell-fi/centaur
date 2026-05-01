import pytest
from slack_sdk.errors import SlackApiError

from slack.client import SlackClient


class _FakeSlackResponse(dict):
    def __init__(self, *, error: str = "ratelimited", headers: dict | None = None, status_code: int = 429) -> None:
        super().__init__(error=error)
        self.headers = headers or {}
        self.status_code = status_code


class _FakeWebClient:
    def __init__(self) -> None:
        self.last_kwargs = None
        self.history_calls: list[dict] = []
        self.history_pages: list[dict] = []
        self.api_calls: list[tuple[str, dict]] = []

    def chat_postMessage(self, **kwargs):  # noqa: N802
        self.last_kwargs = kwargs
        return {"ts": "123.456"}

    def conversations_history(self, **kwargs):  # noqa: N802
        self.history_calls.append(kwargs)
        return self.history_pages.pop(0)

    def api_call(self, method: str, *, params: dict):
        self.api_calls.append((method, params))
        return {"ok": True, "messages": {"matches": []}}


def _make_client() -> tuple[SlackClient, _FakeWebClient]:
    client = SlackClient.__new__(SlackClient)
    fake_web_client = _FakeWebClient()
    client._client = fake_web_client
    client._search_client = fake_web_client
    client._user_cache = {}
    client._ratelimit_deadlines = {}
    client._resolve_channel = lambda channel: "C123"  # type: ignore[method-assign]
    client._format_requester_attribution = lambda: ""  # type: ignore[method-assign]
    client.list_bot_channels = lambda **_: [{"id": "C123", "name": "paradigm-pulse"}]  # type: ignore[method-assign]
    return client, fake_web_client


def test_send_message_forwards_unfurl_flags() -> None:
    client, fake_web_client = _make_client()

    client.send_message(
        "paradigm-pulse",
        "hello",
        unfurl_links=False,
        unfurl_media=False,
    )

    assert fake_web_client.last_kwargs is not None
    assert fake_web_client.last_kwargs["unfurl_links"] is False
    assert fake_web_client.last_kwargs["unfurl_media"] is False


def test_send_message_omits_unfurl_flags_by_default() -> None:
    client, fake_web_client = _make_client()

    client.send_message("paradigm-pulse", "hello")

    assert fake_web_client.last_kwargs is not None
    assert "unfurl_links" not in fake_web_client.last_kwargs
    assert "unfurl_media" not in fake_web_client.last_kwargs


def test_retry_on_ratelimit_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_client()
    now = {"value": 100.0}
    sleeps: list[float] = []

    monkeypatch.setattr("slack.client.time.time", lambda: now["value"])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds

    monkeypatch.setattr("slack.client.time.sleep", fake_sleep)

    attempts = {"count": 0}

    def flaky_call() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise SlackApiError(
                message="rate limited",
                response=_FakeSlackResponse(headers={"Retry-After": "7"}),
            )
        return "ok"

    assert client._retry_on_ratelimit(flaky_call, method_key="conversations.history") == "ok"
    assert attempts["count"] == 2
    assert sleeps == [7.25]


def test_get_channel_history_page_paginates_with_date_window() -> None:
    client, fake_web_client = _make_client()
    client._get_user_cache = lambda: {"U1": "alice", "U2": "bob"}  # type: ignore[method-assign]
    fake_web_client.history_pages = [
        {
            "messages": [
                {"user": "U1", "text": "first", "ts": "200.000000"},
                {
                    "user": "U2",
                    "text": "hi <@U1>",
                    "ts": "190.000000",
                    "thread_ts": "190.000000",
                    "reply_count": 1,
                },
            ],
            "response_metadata": {"next_cursor": "cursor-2"},
        },
        {
            "messages": [
                {"user": "U1", "text": "third", "ts": "180.000000"},
            ],
            "response_metadata": {"next_cursor": ""},
        },
    ]

    result = client.get_channel_history_page(
        "paradigm-pulse",
        limit=3,
        oldest="2026-01-01",
        latest="2026-01-02",
        inclusive=True,
    )

    assert len(fake_web_client.history_calls) == 2
    assert fake_web_client.history_calls[0]["oldest"] == client._normalize_ts("2026-01-01")
    assert fake_web_client.history_calls[0]["latest"] == client._normalize_ts("2026-01-02")
    assert fake_web_client.history_calls[0]["inclusive"] is True
    assert fake_web_client.history_calls[1]["cursor"] == "cursor-2"
    assert result["count"] == 3
    assert result["has_more"] is False
    assert result["messages"][1]["text"] == "hi @alice"


def test_native_search_uses_dedicated_search_client() -> None:
    client, fake_bot_client = _make_client()
    fake_search_client = _FakeWebClient()
    fake_search_client.api_call = lambda method, *, params: {  # type: ignore[method-assign]
        "ok": True,
        "messages": {
            "matches": [
                {
                    "user": "U1",
                    "text": "deploy <@U2>",
                    "ts": "200.000000",
                    "permalink": "https://slack.com/archives/C123/p200000000",
                    "channel": {"id": "C123", "name": "paradigm-pulse"},
                    "thread_ts": "200.000000",
                    "reply_count": 2,
                }
            ]
        },
    }
    client._search_client = fake_search_client
    client._get_user_cache = lambda: {"U1": "alice", "U2": "bob"}  # type: ignore[method-assign]

    result = client._search_messages_native("deploy", max_results=5)

    assert result == [
        {
            "channel": "paradigm-pulse",
            "channel_id": "C123",
            "user": "alice",
            "user_id": "U1",
            "text": "deploy @bob",
            "timestamp": "200.000000",
            "permalink": "https://slack.com/archives/C123/p200000000",
            "thread_ts": "200.000000",
            "reply_count": 2,
        }
    ]
    assert fake_bot_client.api_calls == []


def test_sync_channel_history_uses_watermark_lookback() -> None:
    client, _ = _make_client()
    captured: dict = {}

    def fake_get_channel_history_page(**kwargs):
        captured.update(kwargs)
        return {
            "channel": "paradigm-pulse",
            "channel_id": "C123",
            "messages": [{"timestamp": "3000100.000000"}],
            "count": 1,
            "has_more": False,
            "next_cursor": None,
            "window": {
                "oldest": kwargs["oldest"],
                "latest": kwargs["latest"],
                "inclusive": kwargs["inclusive"],
            },
            "order": "desc",
        }

    client.get_channel_history_page = fake_get_channel_history_page  # type: ignore[method-assign]

    result = client.sync_channel_history(
        "paradigm-pulse",
        state={"watermark": "3000000.000000"},
        lookback_days=30,
        limit=100,
    )

    assert captured["oldest"] == "408000.000000"
    assert captured["inclusive"] is True
    assert result["sync_state"]["cursor"] is None
    assert result["sync_state"]["watermark"] == "3000100.000000"
