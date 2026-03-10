"""Stateless pipe agent — spawn sandboxes, pipe stdin/stdout, no Postgres.

Thin orchestration layer: one sandbox per thread_key, raw NDJSON streaming.
Sessions are in-memory only and reconstructable from the backend on restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import threading
from collections.abc import AsyncIterator
from typing import Any

import structlog

from api.sandbox.base import SandboxSession
from api.sandbox.registry import get_backend

log = structlog.get_logger()


# In-memory sessions — reconstructable from backend on restart
_sessions: dict[str, SandboxSession] = {}


def _ensure_reader(session: SandboxSession, *, force: bool = False) -> None:
    """Start a single background reader thread that dispatches stdout lines to the
    active turn's queue. Only one reader ever exists per session.

    Pass force=True after a stream reconnect to start a new reader even if the
    old one is still alive (it will be invalidated via the generation counter).
    """
    reader = session.metadata.get("_reader_thread")
    if not force and reader and reader.is_alive():
        return

    backend = get_backend()
    gen = session.metadata.get("_reader_gen", 0) + 1
    session.metadata["_reader_gen"] = gen

    def _read_loop(my_gen: int = gen) -> None:
        try:
            for line in backend.stream_stdout(session):
                if session.metadata.get("_reader_gen") != my_gen:
                    return
                q = session.metadata.get("_active_queue")
                if q is not None:
                    q.put(line)
        except Exception:
            pass
        # EOF — signal the active queue only if this reader is still current
        if session.metadata.get("_reader_gen") == my_gen:
            q = session.metadata.get("_active_queue")
            if q is not None:
                q.put(None)

    t = threading.Thread(target=_read_loop, daemon=True)
    t.start()
    session.metadata["_reader_thread"] = t


def _spawn_sync(thread_key: str, harness: str) -> SandboxSession:
    """Synchronous spawn: create sandbox + register session."""
    engines = {"amp": "amp", "claude-code": "claude-code", "codex": "codex"}
    engine = engines.get(harness, "amp")

    backend = get_backend()
    session = backend.create(thread_key, harness, engine)

    # Initialize orchestration metadata
    session.metadata["turn_counter"] = 0
    session.metadata["_active_turn_id"] = 0
    session.metadata["_turn_lock"] = threading.Lock()
    session.metadata["_active_queue"] = None

    _sessions[thread_key] = session
    log.info("pipe_session_spawned", thread_key=thread_key, sandbox=session.sandbox_id[:12])
    return session


def _send_turn_and_stream(session: SandboxSession, message: str):
    """Send a turn via stdin, yield stdout lines until turn.done (blocking generator).

    A single reader thread reads stdout and dispatches lines to the active turn's
    queue. When a new turn starts, the old queue gets a None sentinel so the old
    consumer stops, and the new turn's queue takes over.
    """
    backend = get_backend()
    backend.attach(session)

    # Create a new queue for this turn and swap it in atomically
    turn_queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
    turn_lock = session.metadata["_turn_lock"]
    with turn_lock:
        session.metadata["turn_counter"] = session.metadata.get("turn_counter", 0) + 1
        turn_id = session.metadata["turn_counter"]
        session.metadata["_active_turn_id"] = turn_id
        old_queue = session.metadata.get("_active_queue")
        session.metadata["_active_queue"] = turn_queue

    # Signal old turn to stop
    if old_queue is not None:
        old_queue.put(None)

    _ensure_reader(session)

    try:
        backend.write_stdin(session, {"type": "turn.start", "turn_id": turn_id, "text": message})
    except (BrokenPipeError, OSError) as exc:
        log.warning("stdin_broken_pipe", sandbox=session.sandbox_id[:12], error=str(exc))
        # Verify the container is still alive — no point retrying a dead sandbox.
        st = backend.status(session)
        if st != "running":
            raise RuntimeError(f"sandbox exited (status={st})") from exc
        # Stale sockets — close, re-attach, restart reader, and retry.
        # Swap in a fresh queue so the stale reader (invalidated via generation
        # counter) cannot enqueue a spurious None sentinel.
        backend.close_streams(session)
        backend.attach(session)
        turn_queue = queue.SimpleQueue()
        with turn_lock:
            session.metadata["_active_queue"] = turn_queue
        _ensure_reader(session, force=True)
        backend.write_stdin(session, {"type": "turn.start", "turn_id": turn_id, "text": message})

    while True:
        line = turn_queue.get()
        if line is None:
            return
        yield line
        try:
            evt = json.loads(line)
            if evt.get("type") == "turn.done" and evt.get("turn_id") == turn_id:
                return
        except (json.JSONDecodeError, TypeError):
            pass


def _stop_sync(thread_key: str) -> bool:
    """Stop sandbox and remove session. Returns True if stopped."""
    session = _sessions.pop(thread_key, None)
    if not session:
        return False
    # Signal active queue to stop
    q = session.metadata.get("_active_queue")
    if q is not None:
        q.put(None)
    session.metadata["_active_queue"] = None

    backend = get_backend()
    backend.stop(session)
    log.info("pipe_session_stopped", thread_key=thread_key)
    return True


def _get_status_sync(thread_key: str) -> dict[str, Any]:
    """Check if a session/sandbox is alive."""
    session = _sessions.get(thread_key)
    if not session:
        return {"thread_key": thread_key, "status": "not_found"}
    backend = get_backend()
    st = backend.status(session)
    if st == "gone":
        _sessions.pop(thread_key, None)
        return {"thread_key": thread_key, "status": "gone"}
    return {
        "thread_key": thread_key,
        "status": st,
        "sandbox_id": session.sandbox_id[:12],
        "harness": session.harness,
        "engine": session.engine,
        "started_at": session.started_at,
    }


def _recover_sync() -> dict[str, Any]:
    """Scan backend for running sandboxes, rebuild _sessions."""
    backend = get_backend()
    recovered = 0
    try:
        sessions = backend.recover()
    except Exception as exc:
        log.warning("pipe_recover_failed", error=str(exc))
        return {"recovered": 0, "error": str(exc)}

    for session in sessions:
        if not session.thread_key or session.thread_key in _sessions:
            continue
        # Initialize orchestration metadata
        session.metadata["turn_counter"] = 0
        session.metadata["_active_turn_id"] = 0
        session.metadata["_turn_lock"] = threading.Lock()
        session.metadata["_active_queue"] = None
        _sessions[session.thread_key] = session
        recovered += 1
        log.info("pipe_session_recovered", thread_key=session.thread_key)

    return {"recovered": recovered}


# ── Async public API (wraps sync backend calls in threads) ────────────────


async def get_or_spawn(thread_key: str, harness: str = "amp") -> SandboxSession:
    """Get existing session or spawn a new sandbox.

    Tries (in order): existing session → warm pool → cold spawn.
    """
    session = _sessions.get(thread_key)
    if session:
        # Verify sandbox is still alive
        backend = get_backend()
        st = await asyncio.to_thread(backend.status, session)
        if st == "running":
            return session
        _sessions.pop(thread_key, None)

    # Try claiming a pre-warmed container (instant, no startup cost)
    from api.warm_pool import claim_container

    claimed = await asyncio.to_thread(claim_container, thread_key, harness)
    if claimed:
        _sessions[thread_key] = claimed
        return claimed

    return await asyncio.to_thread(_spawn_sync, thread_key, harness)


def _reconnect_and_stream(session: SandboxSession):
    """Re-attach to a running sandbox's stdout and stream lines.

    Unlike _send_turn_and_stream, this does NOT send a turn.start — the agent
    is assumed to be mid-turn already.  We just hook up a fresh stdout socket
    and relay whatever it produces until turn.done or EOF.
    """
    backend = get_backend()
    # Force fresh sockets
    backend.close_streams(session)
    backend.attach(session, logs=True)

    turn_queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
    turn_lock = session.metadata["_turn_lock"]
    with turn_lock:
        old_queue = session.metadata.get("_active_queue")
        session.metadata["_active_queue"] = turn_queue

    if old_queue is not None:
        old_queue.put(None)

    _ensure_reader(session)

    while True:
        line = turn_queue.get()
        if line is None:
            return
        yield line
        try:
            evt = json.loads(line)
            if evt.get("type") == "turn.done":
                return
        except (json.JSONDecodeError, TypeError):
            pass


async def stream_reconnect(session: SandboxSession) -> AsyncIterator[str]:
    """Async wrapper for reconnecting to a running sandbox's stdout."""
    loop = asyncio.get_event_loop()
    q: asyncio.Queue[str | None] = asyncio.Queue()

    def _run() -> None:
        try:
            for line in _reconnect_and_stream(session):
                loop.call_soon_threadsafe(q.put_nowait, line)
        except Exception as exc:
            loop.call_soon_threadsafe(
                q.put_nowait,
                json.dumps({"type": "error", "message": str(exc)}),
            )
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    thread = asyncio.to_thread(_run)
    task = asyncio.ensure_future(thread)

    try:
        while True:
            item = await q.get()
            if item is None:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def stream_exec(session: SandboxSession, message: str) -> AsyncIterator[str]:
    """Run a command in the sandbox and yield raw stdout lines."""
    loop = asyncio.get_event_loop()
    q: asyncio.Queue[str | None] = asyncio.Queue()

    def _run() -> None:
        try:
            for line in _send_turn_and_stream(session, message):
                loop.call_soon_threadsafe(q.put_nowait, line)
        except Exception as exc:
            loop.call_soon_threadsafe(
                q.put_nowait,
                json.dumps({"type": "error", "message": str(exc)}),
            )
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    thread = asyncio.to_thread(_run)
    task = asyncio.ensure_future(thread)

    try:
        while True:
            item = await q.get()
            if item is None:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def stop_session(thread_key: str) -> bool:
    return await asyncio.to_thread(_stop_sync, thread_key)


async def get_status(thread_key: str) -> dict[str, Any]:
    return await asyncio.to_thread(_get_status_sync, thread_key)


async def recover_sessions() -> dict[str, Any]:
    return await asyncio.to_thread(_recover_sync)
