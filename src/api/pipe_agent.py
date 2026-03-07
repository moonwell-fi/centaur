"""Stateless pipe agent — spawn containers, pipe stdin/stdout, no Postgres.

Thin wrapper around Docker: one container per thread_key, raw NDJSON streaming.
Sessions are in-memory only and reconstructable from Docker labels on restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import docker
import structlog
from docker.errors import NotFound

from shared.tool_sdk import _sm_read

log = structlog.get_logger()


@dataclass
class PipeSession:
    container_id: str
    thread_key: str
    harness: str
    engine: str
    _stdin_sock: Any = field(default=None, repr=False)
    _stdout_sock: Any = field(default=None, repr=False)
    turn_counter: int = 0
    started_at: float = 0.0
    _active_turn_id: int = field(default=0, repr=False)
    _turn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    _active_queue: queue.SimpleQueue[str | None] | None = field(default=None, repr=False)


# In-memory sessions — reconstructable from Docker labels on restart
_sessions: dict[str, PipeSession] = {}


def _docker_client() -> docker.DockerClient:
    return docker.from_env()


def _image() -> str:
    return os.getenv("AGENT_IMAGE", "agent2:latest")


def _repos_host_dir() -> str:
    return os.getenv("REPOS_HOST_DIR", os.path.expanduser("~/github"))


def _fetch_secret(key: str) -> str:
    return _sm_read(key) or os.getenv(key, "")


def _sm_list_keys() -> list[str]:
    url = os.environ.get("SECRET_MANAGER_URL", "http://secrets:8100")
    try:
        import httpx

        resp = httpx.get(f"{url}/keys", timeout=5.0)
        if resp.status_code == 200:
            return resp.json().get("keys", [])
    except Exception:
        pass
    return []


def _container_env() -> list[str]:
    """Build env vars for sandbox containers."""
    local_dev = os.getenv("AGENT_LOCAL_DEV", "").lower() in ("1", "true")
    env = [
        f"AI_V2_API_URL={os.getenv('AGENT_API_URL', 'http://api:8000')}",
        f"AI_V2_API_KEY={_fetch_secret('API_SECRET_KEY')}",
    ]

    if local_dev:
        for key in _sm_list_keys():
            real = _fetch_secret(key).strip()
            if real:
                env.append(f"{key}={real}")
    else:
        firewall_host = os.getenv("FIREWALL_HOST", "firewall")
        for key in _sm_list_keys():
            env.append(f"{key}={key}")
        env.extend(
            [
                f"HTTPS_PROXY=http://{firewall_host}:8080",
                f"HTTP_PROXY=http://{firewall_host}:8080",
                f"https_proxy=http://{firewall_host}:8080",
                f"http_proxy=http://{firewall_host}:8080",
                "NO_PROXY=api,localhost,127.0.0.1",
                "no_proxy=api,localhost,127.0.0.1",
                "NODE_EXTRA_CA_CERTS=/firewall-certs/ca-cert.pem",
                "REQUESTS_CA_BUNDLE=/firewall-certs/ca-cert.pem",
                "SSL_CERT_FILE=/firewall-certs/ca-cert.pem",
                "GIT_SSL_CAINFO=/firewall-certs/ca-cert.pem",
            ]
        )

    return env


def _container_recent_logs(container: Any, tail: int = 40, max_chars: int = 2000) -> str:
    try:
        raw = container.logs(tail=tail)
    except Exception:
        return ""
    text = (
        raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    )
    text = text.strip()
    return text[-max_chars:] if len(text) > max_chars else text


def _wait_ready(container: Any, timeout: int = 15) -> float:
    """Wait for the entrypoint to signal readiness (touch ~/.ready)."""
    t0 = time.monotonic()
    deadline = t0 + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            container.reload()
        status = str(getattr(container, "status", "") or "")
        if status and status not in {"created", "running"}:
            logs = _container_recent_logs(container)
            detail = f"sandbox exited before ready (status={status})"
            if logs:
                detail += f"; last logs: {logs}"
            raise RuntimeError(detail)
        try:
            exit_code, _ = container.exec_run(["test", "-f", "/home/agent/.ready"], demux=False)
        except Exception:
            time.sleep(0.1)
            continue
        if exit_code == 0:
            return round(time.monotonic() - t0, 3)
        time.sleep(0.1)
    raise TimeoutError(f"sandbox readiness timed out after {timeout}s")


def _attach_sync(session: PipeSession) -> None:
    """Attach to container stdin (for writes) and stdout stream (for reads)."""
    if session._stdin_sock and session._stdout_sock:
        return
    client = _docker_client()
    api = client.api

    # Attach stdin socket for writing NDJSON commands
    stdin_attach = api.attach_socket(
        session.container_id, params={"stdin": True, "stream": True}
    )
    session._stdin_sock = stdin_attach._sock

    # Attach stdout stream — logs=False to skip history, stdout only
    container = client.containers.get(session.container_id)
    session._stdout_sock = container.attach(
        stdout=True, stderr=False, stream=True, logs=False
    )


def _write_stdin(session: PipeSession, obj: dict) -> None:
    """Write an NDJSON line to the container's stdin."""
    payload = json.dumps(obj, separators=(",", ":")) + "\n"
    session._stdin_sock.sendall(payload.encode())


def _create_container(
    client: docker.DockerClient,
    thread_key: str,
    harness: str,
    engine: str,
    *,
    warm: bool = False,
) -> Any:
    """Create and start a labeled agent container. Returns the container object.

    If warm=True, the container is part of the pre-warmed pool (not yet assigned
    to a thread). It gets an ``ai2.warm=true`` label and is excluded from session
    recovery until claimed.
    """
    repos_dir = os.path.abspath(_repos_host_dir())
    env = _container_env()

    container_name = f"pipe-{thread_key.replace(':', '-').replace('.', '-')[:40]}"

    # Remove stale container with same name
    with contextlib.suppress(Exception):
        stale = client.containers.get(container_name)
        stale.remove(force=True)

    labels = {
        "agent2": "true",
        "ai2.pipe": "true",
        "ai2.thread": thread_key,
        "ai2.harness": harness,
        "ai2.engine": engine,
    }
    if warm:
        labels["ai2.warm"] = "true"

    volumes: dict[str, dict[str, str]] = {
        repos_dir: {"bind": "/home/agent/github", "mode": "ro"},
    }
    vol = os.getenv("FIREWALL_CERTS_VOLUME", "firewall-certs")
    volumes[vol] = {"bind": "/firewall-certs", "mode": "ro"}

    container = client.containers.run(
        _image(),
        detach=True,
        stdin_open=True,
        tty=False,
        network_mode=os.getenv("AGENT_NETWORK", "ai_v2_default"),
        mem_limit="4g",
        nano_cpus=int(2 * 1e9),
        environment=env,
        working_dir="/home/agent",
        volumes=volumes,
        labels=labels,
        name=container_name,
    )
    _wait_ready(container)
    return container


def _spawn_sync(thread_key: str, harness: str) -> PipeSession:
    """Synchronous spawn: create container + register session."""
    engines = {"amp": "amp", "claude-code": "claude-code", "codex": "codex"}
    engine = engines.get(harness, "amp")

    client = _docker_client()
    container = _create_container(client, thread_key, harness, engine)

    session = PipeSession(
        container_id=container.id,
        thread_key=thread_key,
        harness=harness,
        engine=engine,
        started_at=time.time(),
    )
    _sessions[thread_key] = session
    log.info("pipe_session_spawned", thread_key=thread_key, container=container.id[:12])
    return session


def _ensure_reader(session: PipeSession) -> None:
    """Start a single background reader thread that dispatches stdout lines to the
    active turn's queue. Only one reader ever exists per session."""
    if session._reader_thread and session._reader_thread.is_alive():
        return

    def _read_loop() -> None:
        buf = ""
        try:
            for chunk in session._stdout_sock:
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    stripped = line.strip()
                    if not stripped:
                        continue
                    q = session._active_queue
                    if q is not None:
                        q.put(stripped)
        except Exception:
            pass
        # EOF — signal the active queue
        q = session._active_queue
        if q is not None:
            q.put(None)

    t = threading.Thread(target=_read_loop, daemon=True)
    t.start()
    session._reader_thread = t


def _send_turn_and_stream(session: PipeSession, message: str):
    """Send a turn via stdin, yield stdout lines until turn.done (blocking generator).

    A single reader thread reads stdout and dispatches lines to the active turn's
    queue. When a new turn starts, the old queue gets a None sentinel so the old
    consumer stops, and the new turn's queue takes over.
    """
    _attach_sync(session)

    # Create a new queue for this turn and swap it in atomically
    turn_queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
    with session._turn_lock:
        session.turn_counter += 1
        turn_id = session.turn_counter
        session._active_turn_id = turn_id
        old_queue = session._active_queue
        session._active_queue = turn_queue

    # Signal old turn to stop
    if old_queue is not None:
        old_queue.put(None)

    _ensure_reader(session)
    _write_stdin(session, {"type": "turn.start", "turn_id": turn_id, "text": message})

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
    """Stop container and remove session. Returns True if stopped."""
    session = _sessions.pop(thread_key, None)
    if not session:
        return False
    # Signal active queue to stop
    q = session._active_queue
    if q is not None:
        q.put(None)
    session._active_queue = None
    # Send interrupt and close sockets
    with contextlib.suppress(Exception):
        _write_stdin(session, {"type": "interrupt"})
    with contextlib.suppress(Exception):
        session._stdin_sock.close()
    session._stdin_sock = None
    session._stdout_sock = None
    client = _docker_client()
    with contextlib.suppress(Exception):
        container = client.containers.get(session.container_id)
        container.stop(timeout=5)
        container.remove()
    log.info("pipe_session_stopped", thread_key=thread_key)
    return True


def _get_status_sync(thread_key: str) -> dict[str, Any]:
    """Check if a session/container is alive."""
    session = _sessions.get(thread_key)
    if not session:
        return {"thread_key": thread_key, "status": "not_found"}
    client = _docker_client()
    try:
        container = client.containers.get(session.container_id)
        return {
            "thread_key": thread_key,
            "status": container.status,
            "container_id": session.container_id[:12],
            "harness": session.harness,
            "engine": session.engine,
            "started_at": session.started_at,
        }
    except NotFound:
        _sessions.pop(thread_key, None)
        return {"thread_key": thread_key, "status": "gone"}


def _recover_sync() -> dict[str, Any]:
    """Scan Docker for containers with ai2.pipe label, rebuild _sessions."""
    client = _docker_client()
    recovered = 0
    try:
        containers = client.containers.list(filters={"label": "ai2.pipe=true"})
    except Exception as exc:
        log.warning("pipe_recover_failed", error=str(exc))
        return {"recovered": 0, "error": str(exc)}

    for container in containers:
        # Skip warm-pool containers — they are managed by warm_pool.py
        if container.labels.get("ai2.warm") == "true":
            continue
        thread_key = container.labels.get("ai2.thread", "")
        if not thread_key or thread_key in _sessions:
            continue
        if container.status != "running":
            continue
        _sessions[thread_key] = PipeSession(
            container_id=container.id,
            thread_key=thread_key,
            harness=container.labels.get("ai2.harness", "amp"),
            engine=container.labels.get("ai2.engine", "amp"),
            started_at=time.time(),
        )
        recovered += 1
        log.info("pipe_session_recovered", thread_key=thread_key)

    return {"recovered": recovered}


# ── Async public API (wraps sync Docker calls in threads) ────────────────


async def get_or_spawn(thread_key: str, harness: str = "amp") -> PipeSession:
    """Get existing session or spawn a new container.

    Tries (in order): existing session → warm pool → cold spawn.
    """
    session = _sessions.get(thread_key)
    if session:
        # Verify container is still alive
        client = await asyncio.to_thread(_docker_client)
        try:
            container = await asyncio.to_thread(client.containers.get, session.container_id)
            if container.status == "running":
                return session
        except NotFound:
            pass
        _sessions.pop(thread_key, None)

    # Try claiming a pre-warmed container (instant, no startup cost)
    from api.warm_pool import claim_container

    claimed = await asyncio.to_thread(claim_container, thread_key, harness)
    if claimed:
        _sessions[thread_key] = claimed
        return claimed

    return await asyncio.to_thread(_spawn_sync, thread_key, harness)


def _reconnect_and_stream(session: PipeSession):
    """Re-attach to a running container's stdout and stream lines.

    Unlike _send_turn_and_stream, this does NOT send a turn.start — the agent
    is assumed to be mid-turn already.  We just hook up a fresh stdout socket
    and relay whatever it produces until turn.done or EOF.
    """
    # Force fresh sockets — attach with logs=True so the client gets all
    # output produced while it was disconnected (it will deduplicate).
    session._stdin_sock = None
    session._stdout_sock = None

    client = _docker_client()
    api = client.api

    stdin_attach = api.attach_socket(
        session.container_id, params={"stdin": True, "stream": True}
    )
    session._stdin_sock = stdin_attach._sock

    container = client.containers.get(session.container_id)
    session._stdout_sock = container.attach(
        stdout=True, stderr=False, stream=True, logs=True
    )

    turn_queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
    with session._turn_lock:
        old_queue = session._active_queue
        session._active_queue = turn_queue

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


async def stream_reconnect(session: PipeSession) -> AsyncIterator[str]:
    """Async wrapper for reconnecting to a running container's stdout."""
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


async def stream_exec(session: PipeSession, message: str) -> AsyncIterator[str]:
    """Run a command in the container and yield raw stdout lines."""
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
