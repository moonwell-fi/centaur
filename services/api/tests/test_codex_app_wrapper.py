from __future__ import annotations

import importlib.util
from pathlib import Path
import tomllib
from types import ModuleType
import uuid


WRAPPER_PY = Path(__file__).resolve().parents[2] / "sandbox" / "codex-app-wrapper.py"


def _load_wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("codex_app_wrapper", WRAPPER_PY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configure_laminar_otel_writes_startup_config(monkeypatch, tmp_path) -> None:
    wrapper = _load_wrapper()
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        """
model = "gpt-5.5"

[otel]
environment = "old"

[otel.exporter.otlp-http]
endpoint = "http://old/v1/logs"
protocol = "binary"

[projects."/home/agent/workspace"]
trust_level = "trusted"
""".lstrip()
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CENTAUR_TRACE_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("CENTAUR_THREAD_KEY", "warm-placeholder")
    monkeypatch.setenv("LMNR_BASE_URL", "http://laminar:8000")
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "lmnr-key")
    monkeypatch.setenv("CODEX_OTEL_ENVIRONMENT", "staging")

    wrapper.configure_laminar_otel_for_startup(
        "00000000-0000-0000-0000-000000000123",
        "slack:C123:1700000000.000100",
    )

    contents = config_path.read_text()
    parsed = tomllib.loads(contents)
    assert parsed["model"] == "gpt-5.5"
    assert parsed["projects"]["/home/agent/workspace"]["trust_level"] == "trusted"
    assert parsed["otel"]["environment"] == "staging"
    assert "exporter" not in parsed["otel"]
    assert (
        parsed["otel"]["trace_exporter"]["otlp-http"]["endpoint"]
        == "http://laminar:8000/v1/traces"
    )
    assert parsed["otel"]["trace_exporter"]["otlp-http"]["protocol"] == "binary"
    assert parsed["otel"]["trace_exporter"]["otlp-http"]["headers"] == {
        "x-trace-id": "00000000-0000-0000-0000-000000000123",
        "x-centaur-thread-key": "slack:C123:1700000000.000100",
        "authorization": "Bearer lmnr-key",
    }
    assert "v1/logs" not in contents


def test_configure_laminar_otel_sets_w3c_trace_context(monkeypatch, tmp_path) -> None:
    wrapper = _load_wrapper()
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LMNR_BASE_URL", "http://laminar:8000")
    monkeypatch.setattr(
        wrapper.uuid,
        "uuid4",
        lambda: uuid.UUID("11111111-2222-3333-4444-555555555555"),
    )

    wrapper.CURRENT_TRACEPARENT = None
    wrapper.configure_laminar_otel_for_startup(
        "00000000-0000-4000-8000-000000000123",
        "slack:C123:1700000000.000100",
    )

    assert (
        wrapper.CURRENT_TRACEPARENT
        == "00-00000000000040008000000000000123-1111111122223333-01"
    )
    assert (
        wrapper.os.environ["TRACEPARENT"]
        == "00-00000000000040008000000000000123-1111111122223333-01"
    )


def test_configure_trace_context_ignores_invalid_trace_id(monkeypatch) -> None:
    wrapper = _load_wrapper()
    monkeypatch.delenv("TRACEPARENT", raising=False)

    wrapper.CURRENT_TRACEPARENT = None
    wrapper.configure_trace_context("not-a-trace")

    assert wrapper.CURRENT_TRACEPARENT is None
    assert "TRACEPARENT" not in wrapper.os.environ


def test_request_attaches_traceparent(monkeypatch) -> None:
    wrapper = _load_wrapper()
    sent: list[dict] = []
    monkeypatch.setattr(wrapper, "_next_id", lambda: 1)

    def fake_send_raw(payload: dict) -> None:
        sent.append(payload)
        wrapper.RESPONSES[1].put({"id": 1, "result": {"ok": True}})

    monkeypatch.setattr(wrapper, "send_raw", fake_send_raw)

    wrapper.CURRENT_TRACEPARENT = (
        "00-00000000000040008000000000000123-1111111122223333-01"
    )
    result = wrapper.request("thread/start", {"cwd": "/tmp"}, timeout=0.1)

    assert result == {"ok": True}
    assert sent == [
        {
            "id": 1,
            "method": "thread/start",
            "params": {"cwd": "/tmp"},
            "trace": {
                "traceparent": "00-00000000000040008000000000000123-1111111122223333-01"
            },
        }
    ]


def test_main_lazy_starts_app_server_after_input(monkeypatch) -> None:
    wrapper = _load_wrapper()
    requests: list[tuple[str, dict]] = []
    popen_args: list[str] = []
    emitted: list[dict] = []

    class FakeProcess:
        stdin = object()
        stdout = object()
        stderr = object()

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    class FakeThread:
        def __init__(self, *args, **kwargs) -> None:
            self.target = kwargs.get("target")

        def start(self) -> None:
            if self.target == wrapper.api_stdin_reader:
                wrapper.INPUTS.put(
                    {
                        "type": "user",
                        "trace_id": "00000000-0000-0000-0000-000000000123",
                        "thread_key": "slack:C123:1700000000.000100",
                        "message": {"content": [{"type": "text", "text": "/goal ship"}]},
                    }
                )
                wrapper.INPUTS.put(None)

    def fake_request(method: str, params: dict, timeout: float = 30.0) -> dict:
        requests.append((method, params))
        if method == "initialize":
            return {"codexHome": "/tmp/.codex"}
        if method == "thread/start":
            return {"thread": {"id": "thread-123"}}
        return {}

    def fake_emit(msg: dict) -> None:
        emitted.append(msg)
        if msg.get("type") == "turn.completed":
            wrapper.SHUTTING_DOWN = True

    def fake_popen(args: list[str], *other_args, **kwargs) -> FakeProcess:
        popen_args.extend(args)
        return FakeProcess()

    monkeypatch.setattr(wrapper.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wrapper.threading, "Thread", FakeThread)
    monkeypatch.setattr(wrapper, "request", fake_request)
    monkeypatch.setattr(wrapper, "notify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrapper, "emit", fake_emit)
    monkeypatch.setattr(
        wrapper, "configure_laminar_otel_for_startup", lambda *_args, **_kwargs: None
    )
    wrapper.SHUTTING_DOWN = False
    wrapper.APP = None
    wrapper.APP_INITIALIZED = False
    wrapper.THREAD_ID = None
    while not wrapper.INPUTS.empty():
        wrapper.INPUTS.get_nowait()

    wrapper.main()

    assert popen_args == ["codex", "app-server", "--listen", "stdio://"]
    assert requests[0] == (
        "initialize",
        {
            "clientInfo": {
                "name": "centaur",
                "title": "Centaur",
                "version": "0.1.0",
            },
            "capabilities": {"experimentalApi": True},
        },
    )
    assert requests[1][0] == "thread/start"
    assert requests[2] == (
        "thread/goal/set",
        {"threadId": "thread-123", "objective": "ship"},
    )
    assert {"type": "thread.started", "thread_id": "thread-123"} in emitted
    assert {"type": "turn.completed"} in emitted


def test_text_from_blocks_strips_inline_binary_blobs() -> None:
    wrapper = _load_wrapper()
    big_pdf_b64 = "A" * 2_000_000
    blocks = [
        {"type": "text", "text": "Summarize this report:"},
        {
            "type": "document",
            "name": "report.pdf",
            "mime_type": "application/pdf",
            "size": 1_400_000,
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": big_pdf_b64,
            },
        },
        {
            "type": "image",
            "name": "diagram.png",
            "source": {"type": "base64", "media_type": "image/png", "data": "AAA"},
        },
        {
            "type": "attachment_ref",
            "attachment_id": "att_42",
            "name": "deck.pdf",
        },
    ]
    text = wrapper.text_from_blocks(blocks)
    assert "Summarize this report" in text
    assert big_pdf_b64 not in text
    assert "report.pdf" in text
    assert "diagram.png" in text
    assert "att_42" in text
    assert "/home/agent/uploads/" in text
    assert len(text) < 4_000


def test_text_from_blocks_keeps_small_unknown_blocks_intact() -> None:
    wrapper = _load_wrapper()
    blocks = [
        {"type": "text", "text": "Use this tool result:"},
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
    ]
    text = wrapper.text_from_blocks(blocks)
    assert '"tool_use_id": "tu_1"' in text


def test_text_from_blocks_collapses_oversize_non_binary_blocks_with_omitted_message() -> None:
    wrapper = _load_wrapper()
    huge = {"type": "tool_result", "content": "x" * 100_000}
    text = wrapper.text_from_blocks([{"type": "text", "text": "go"}, huge])
    assert "x" * 1000 not in text
    # Oversize non-binary blocks should NOT say "binary data stripped"; that
    # phrasing is reserved for image/document/file blocks.
    assert "binary data stripped" not in text
    assert "exceeds inline byte budget" in text


def test_text_from_blocks_basenames_attachment_paths_for_path_safety() -> None:
    wrapper = _load_wrapper()
    blocks = [
        {
            "type": "image",
            "name": "../../etc/passwd",
            "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
        },
        {
            "type": "attachment_ref",
            "attachment_id": "att_99",
            "name": "/etc/shadow",
        },
    ]
    text = wrapper.text_from_blocks(blocks)
    assert "../../etc/passwd" not in text
    assert "/etc/shadow" not in text
    assert "/home/agent/uploads/passwd" in text
    assert "/home/agent/uploads/shadow" in text


def test_text_from_blocks_prefers_attachment_id_over_stray_id() -> None:
    wrapper = _load_wrapper()
    blocks = [
        {
            "type": "attachment_ref",
            "id": "msg_42",
            "attachment_id": "att_42",
            "name": "deck.pdf",
        }
    ]
    text = wrapper.text_from_blocks(blocks)
    assert "id=att_42" in text
    assert "msg_42" not in text


def test_text_from_blocks_ignores_non_dict_entries() -> None:
    wrapper = _load_wrapper()
    blocks = [{"type": "text", "text": "go"}, None, "stray", 7]
    text = wrapper.text_from_blocks(blocks)
    assert text == "go"
