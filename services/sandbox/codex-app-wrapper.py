#!/usr/bin/env python3
"""Centaur NDJSON bridge for Codex through the Python app-server SDK."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel


class EmptyResponse(BaseModel):
    pass


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _payload_dict(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        dumped = payload.model_dump(by_alias=True, exclude_none=True, mode="json")
        return dumped if isinstance(dumped, dict) else {}
    if dataclasses.is_dataclass(payload):
        dumped = dataclasses.asdict(payload)
        if set(dumped) == {"params"} and isinstance(dumped["params"], dict):
            return dumped["params"]
        return dumped
    return payload if isinstance(payload, dict) else {}


def _notification_parts(notification: Any) -> tuple[str, dict[str, Any]]:
    return str(getattr(notification, "method", "") or ""), _payload_dict(
        getattr(notification, "payload", {})
    )


def text_from_blocks(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text") or ""))
        elif btype == "image":
            parts.append(
                "[User sent an image attachment; if needed, ask them to upload it as a file reference.]"
            )
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(part for part in parts if part).strip()


def input_items(turn_input: dict[str, Any]) -> list[Any]:
    blocks = turn_input.get("message", {}).get("content") or []
    if not isinstance(blocks, list):
        blocks = []
    text = text_from_blocks(blocks)
    return [{"type": "text", "text": text or "continue"}]


def split_goal(items: list[Any]) -> tuple[str | None, list[Any]]:
    if len(items) != 1:
        return None, items
    item = items[0]
    if not isinstance(item, dict) or item.get("type") != "text":
        return None, items
    text = str(item.get("text") or "").strip()
    if not text.startswith("/goal"):
        return None, items
    goal = text[len("/goal") :].strip()
    return goal or None, []


def _laminar_otel_endpoint() -> str:
    endpoint = (os.environ.get("CODEX_OTEL_LAMINAR_ENDPOINT") or "").strip()
    if endpoint:
        return endpoint
    base = (
        os.environ.get("CODEX_OTEL_LAMINAR_BASE_URL")
        or os.environ.get("LMNR_BASE_URL")
        or ""
    ).strip()
    if not base:
        return ""
    base = base.rstrip("/")
    if base.endswith("/v1/traces"):
        return base
    return f"{base}/v1/traces"


def laminar_otel_writes(
    trace_id: str | None,
    thread_key: str | None,
) -> list[tuple[str, Any]]:
    endpoint = _laminar_otel_endpoint()
    trace_id = (trace_id or os.environ.get("CENTAUR_TRACE_ID") or "").strip()
    if not endpoint or not trace_id:
        return []

    headers = {"x-trace-id": trace_id}
    thread_key = (thread_key or os.environ.get("CENTAUR_THREAD_KEY") or "").strip()
    if thread_key:
        headers["x-centaur-thread-key"] = thread_key
    api_key = (os.environ.get("LMNR_PROJECT_API_KEY") or "").strip()
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    environment = (
        os.environ.get("CODEX_OTEL_ENVIRONMENT")
        or os.environ.get("DEPLOY_ENV")
        or os.environ.get("ENVIRONMENT")
        or "dev"
    ).strip() or "dev"

    return [
        ("otel.environment", environment),
        ("otel.log_user_prompt", False),
        ("otel.trace_exporter.otlp-http.endpoint", endpoint),
        ("otel.trace_exporter.otlp-http.protocol", "binary"),
        ("otel.trace_exporter.otlp-http.headers", headers),
    ]


class CodexBridge:
    def __init__(self) -> None:
        self.inputs: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.codex: Any | None = None
        self.thread: Any | None = None
        self.thread_id: str | None = None
        self.active_turn: Any | None = None
        self.active_turn_id: str | None = None
        self.configured_otel_trace_id: str | None = None
        self.shutting_down = False

    async def raw_request(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.codex is not None
        await self.codex._client._call_sync(
            self.codex._client._sync._request_raw,
            method,
            params or {},
        )

    async def configure_laminar_otel(
        self,
        trace_id: str | None,
        thread_key: str | None,
    ) -> None:
        trace_id = (trace_id or os.environ.get("CENTAUR_TRACE_ID") or "").strip()
        if not trace_id or self.configured_otel_trace_id == trace_id:
            return
        writes = laminar_otel_writes(trace_id, thread_key)
        if not writes:
            return
        for key_path, value in writes:
            await self.raw_request(
                "config/value/write",
                {"keyPath": key_path, "value": value, "mergeStrategy": "upsert"},
            )
        self.configured_otel_trace_id = trace_id

    async def start_or_resume_thread(self) -> str:
        if self.thread is not None and self.thread_id:
            return self.thread_id

        assert self.codex is not None
        from openai_codex import ApprovalMode

        resume = (
            os.environ.get("CODEX_CONTINUE_THREAD_ID")
            or os.environ.get("AMP_CONTINUE_THREAD_ID")
            or ""
        ).strip()
        if resume:
            self.thread = await self.codex.thread_resume(
                resume,
                approval_mode=ApprovalMode.deny_all,
                cwd=os.getcwd(),
            )
        else:
            self.thread = await self.codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=os.getcwd(),
            )
        self.thread_id = str(getattr(self.thread, "id", None) or resume or uuid.uuid4())
        emit({"type": "thread.started", "thread_id": self.thread_id})
        return self.thread_id

    def emit_notification(self, notification: Any) -> bool:
        method, params = _notification_parts(notification)

        if method == "thread/started":
            thread = params.get("thread") or {}
            tid = thread.get("id") or params.get("threadId")
            if tid:
                self.thread_id = str(tid)
                emit({"type": "thread.started", "thread_id": self.thread_id})
            return False

        if method == "turn/started":
            turn = params.get("turn") or {}
            self.active_turn_id = (
                str(turn.get("id") or params.get("turnId") or "") or self.active_turn_id
            )
            emit({"type": "turn.started", "turn_id": self.active_turn_id or ""})
            return False

        if method in {"item/started", "item/updated", "item/completed"}:
            emit({"type": method.replace("/", "."), "item": params.get("item") or params})
            return False

        if method in {
            "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
            "item/fileChange/patchUpdated",
            "item/plan/delta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/summaryPartAdded",
            "item/reasoning/textDelta",
        }:
            emit({"type": method.replace("/", "."), **params})
            return False

        if method == "turn/plan/updated":
            emit({"type": method.replace("/", "."), **params})
            return False

        if method == "item/agentMessage/delta":
            payload = {"type": method.replace("/", "."), **params}
            if self.thread_id and "session_id" not in payload and "thread_id" not in payload:
                payload["session_id"] = self.thread_id
            emit(payload)
            return False

        if method == "turn/completed":
            turn = params.get("turn") or {}
            emit(
                {
                    "type": "turn.completed",
                    "turn": turn,
                    "usage": params.get("usage") or turn.get("usage"),
                }
            )
            self.active_turn = None
            self.active_turn_id = None
            return True

        if method in {"turn/failed", "error"}:
            emit({"type": "turn.failed", "error": params.get("error") or params})
            self.active_turn = None
            self.active_turn_id = None
            return True

        if method in {"thread/goal/updated", "thread/goal/cleared"}:
            emit({"type": method.replace("/", "."), **params})
            return False

        return False

    async def drain_until_turn_done(self, turn: Any) -> None:
        stream = turn.stream()
        notification_task = asyncio.create_task(stream.__anext__())
        input_task = asyncio.create_task(self.inputs.get())
        try:
            while not self.shutting_down:
                done, _pending = await asyncio.wait(
                    {notification_task, input_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if notification_task in done:
                    try:
                        notification = notification_task.result()
                    except StopAsyncIteration:
                        return
                    if self.emit_notification(notification):
                        return
                    notification_task = asyncio.create_task(stream.__anext__())

                if input_task in done:
                    incoming = input_task.result()
                    if incoming is None:
                        self.shutting_down = True
                        return
                    await self.handle_input(incoming)
                    if self.active_turn is None:
                        return
                    input_task = asyncio.create_task(self.inputs.get())
        finally:
            notification_task.cancel()
            input_task.cancel()
            await asyncio.gather(notification_task, input_task, return_exceptions=True)
            await stream.aclose()

    async def handle_input(self, turn_input: dict[str, Any]) -> None:
        if turn_input.get("type") == "interrupt":
            await self.interrupt_active_turn()
            return
        if turn_input.get("type") != "user":
            return

        await self.configure_laminar_otel(
            turn_input.get("trace_id"),
            turn_input.get("thread_key"),
        )
        thread_id = await self.start_or_resume_thread()
        items = input_items(turn_input)
        goal, items = split_goal(items)
        if goal is not None:
            await self.raw_request(
                "thread/goal/set",
                {"threadId": thread_id, "objective": goal},
            )
            emit(
                {
                    "type": "assistant",
                    "session_id": thread_id,
                    "message": {"content": [{"type": "text", "text": "Goal set."}]},
                }
            )
            emit({"type": "turn.completed"})
            return

        if self.active_turn is not None or turn_input.get("steer"):
            try:
                if self.active_turn is None:
                    raise RuntimeError("no active turn to steer")
                await self.active_turn.steer(items)
                return
            except Exception:
                await self.interrupt_active_turn()

        assert self.thread is not None
        from openai_codex import ApprovalMode

        turn = await self.thread.turn(items, approval_mode=ApprovalMode.deny_all)
        self.active_turn = turn
        self.active_turn_id = str(getattr(turn, "id", "") or "") or None
        await self.drain_until_turn_done(turn)

    async def interrupt_active_turn(self) -> None:
        if self.active_turn is not None:
            try:
                await self.active_turn.interrupt()
            except Exception as exc:
                emit({"type": "error", "message": f"interrupt failed: {exc}"})
        self.active_turn = None
        self.active_turn_id = None

    def stop(self) -> None:
        self.shutting_down = True
        self.inputs.put_nowait(None)

    async def run(self) -> None:
        from openai_codex import AppServerConfig, AsyncCodex

        codex_bin = (
            os.environ.get("CODEX_APP_SERVER_BIN")
            or os.environ.get("CODEX_BIN")
            or shutil.which("codex")
        )
        config = AppServerConfig(
            codex_bin=codex_bin,
            config_overrides=(
                'sandbox_mode="danger-full-access"',
                'approval_policy="never"',
            ),
            cwd=os.getcwd(),
            client_name="centaur",
            client_title="Centaur",
            client_version="0.1.0",
            experimental_api=True,
        )

        async with AsyncCodex(config=config) as codex:
            self.codex = codex
            await self.raw_request(
                "config/value/write",
                {"keyPath": "features.goals", "value": True, "mergeStrategy": "upsert"},
            )
            emit({"type": "system", "subtype": "wrapper_heartbeat", "phase": "startup"})

            stdin_task = asyncio.create_task(read_api_stdin(self.inputs))
            try:
                while not self.shutting_down:
                    item = await self.inputs.get()
                    if item is None:
                        break
                    try:
                        await self.handle_input(item)
                    except Exception as exc:
                        emit({"type": "error", "message": str(exc)})
                        emit({"type": "turn.failed", "error": {"message": str(exc)}})
            finally:
                stdin_task.cancel()
                await asyncio.gather(stdin_task, return_exceptions=True)
                await self.interrupt_active_turn()


async def read_api_stdin(inputs: asyncio.Queue[dict[str, Any] | None]) -> None:
    while True:
        raw = await asyncio.to_thread(sys.stdin.readline)
        if raw == "":
            break
        line = raw.strip()
        if not line:
            continue
        try:
            await inputs.put(json.loads(line))
        except json.JSONDecodeError:
            emit({"type": "error", "message": "invalid stdin JSON"})
    await inputs.put(None)


def install_signal_handlers(
    bridge: CodexBridge,
    create_task: Callable[[Awaitable[None]], asyncio.Task[None]],
) -> None:
    loop = asyncio.get_running_loop()

    def stop() -> None:
        bridge.stop()

    def interrupt() -> None:
        create_task(bridge.interrupt_active_turn())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: stop())
    try:
        loop.add_signal_handler(signal.SIGUSR1, interrupt)
    except (AttributeError, NotImplementedError):
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, lambda *_args: interrupt())


async def async_main() -> None:
    bridge = CodexBridge()
    install_signal_handlers(bridge, asyncio.create_task)
    await bridge.run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
