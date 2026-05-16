from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
from types import ModuleType


WRAPPER_PY = Path(__file__).resolve().parents[2] / "sandbox" / "codex-app-wrapper.py"


def _load_wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("codex_app_wrapper", WRAPPER_PY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_laminar_otel_writes_use_turn_trace_id(monkeypatch) -> None:
    wrapper = _load_wrapper()

    monkeypatch.setenv("CENTAUR_TRACE_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("CENTAUR_THREAD_KEY", "warm-placeholder")
    monkeypatch.setenv("LMNR_BASE_URL", "http://laminar:8000")
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "lmnr-key")
    monkeypatch.setenv("CODEX_OTEL_ENVIRONMENT", "staging")

    writes = wrapper.laminar_otel_writes(
        "00000000-0000-0000-0000-000000000123",
        "slack:C123:1700000000.000100",
    )

    indexed = dict(writes)
    assert indexed["otel.environment"] == "staging"
    assert (
        indexed["otel.trace_exporter.otlp-http.endpoint"]
        == "http://laminar:8000/v1/traces"
    )
    assert indexed["otel.trace_exporter.otlp-http.protocol"] == "binary"
    assert indexed["otel.trace_exporter.otlp-http.headers"] == {
        "x-trace-id": "00000000-0000-0000-0000-000000000123",
        "x-centaur-thread-key": "slack:C123:1700000000.000100",
        "authorization": "Bearer lmnr-key",
    }


def test_notification_parts_dump_pydantic_aliases() -> None:
    wrapper = _load_wrapper()

    class Payload(wrapper.BaseModel):
        turn_id: str

        model_config = {"alias_generator": lambda value: "turnId", "populate_by_name": True}

    @dataclass
    class Notification:
        method: str
        payload: Payload

    method, params = wrapper._notification_parts(
        Notification("item/agentMessage/delta", Payload(turn_id="turn-1"))
    )

    assert method == "item/agentMessage/delta"
    assert params == {"turnId": "turn-1"}
