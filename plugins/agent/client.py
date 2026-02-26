"""Agent sandbox — 1 Slack thread = 1 Docker container.

Manages container lifecycle and executes harness CLI commands (amp,
claude-code, codex) inside them. Returns the final result text.
"""

import codecs
import contextlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import docker
import psycopg2
import psycopg2.extras
import structlog
from docker.errors import NotFound

log = structlog.get_logger()

HARNESSES = ("amp", "claude-code", "codex")

# Max seconds to wait for a single exec call before killing it
EXEC_TIMEOUT = int(os.getenv("AGENT_EXEC_TIMEOUT", "600"))

# In-memory session registry: slack_thread_key → session dict
_sessions: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Postgres persistence (best-effort — never breaks Docker operations)
# ---------------------------------------------------------------------------
def _pg_write(sql: str, params: tuple = ()) -> None:
    """Execute a single write against Postgres. Silently skips on failure."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return
    try:
        conn = psycopg2.connect(url, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.debug("pg_write_failed", error=str(exc))


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _persist_session(session: dict[str, Any], key: str) -> None:
    _pg_write(
        """
        INSERT INTO agent_sessions
            (slack_thread_key, container_id, harness, agent_thread_id,
             state, created_at, last_activity)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (slack_thread_key) DO UPDATE SET
            container_id    = EXCLUDED.container_id,
            harness         = EXCLUDED.harness,
            agent_thread_id = EXCLUDED.agent_thread_id,
            state           = EXCLUDED.state,
            last_activity   = EXCLUDED.last_activity
        """,
        (
            key,
            session["container_id"],
            session["harness"],
            session.get("agent_thread_id"),
            session["state"],
            _ts(session["created_at"]),
            _ts(session["last_activity"]),
        ),
    )


def _persist_turn(key: str, turn: dict[str, Any]) -> None:
    events_json = json.dumps(turn.get("events", []), default=str)
    _pg_write(
        """
        INSERT INTO agent_turns
            (slack_thread_key, turn_id, user_message, events, result,
             started_at, finished_at, exit_code, timed_out, duration_s)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (slack_thread_key, turn_id) DO UPDATE SET
            events      = EXCLUDED.events,
            result      = EXCLUDED.result,
            finished_at = EXCLUDED.finished_at,
            exit_code   = EXCLUDED.exit_code,
            timed_out   = EXCLUDED.timed_out,
            duration_s  = EXCLUDED.duration_s
        """,
        (
            key,
            turn["turn_id"],
            turn["user_message"],
            events_json,
            turn["result"],
            _ts(turn["started_at"]),
            _ts(turn["finished_at"]) if turn.get("finished_at") else None,
            turn.get("exit_code"),
            turn.get("timed_out", False),
            turn.get("duration_s", 0),
        ),
    )


def _delete_session(key: str) -> None:
    _pg_write("DELETE FROM agent_sessions WHERE slack_thread_key = %s", (key,))


def _docker_client() -> docker.DockerClient:
    return docker.from_env()


def _image() -> str:
    return os.getenv("AGENT_IMAGE", "agent2:latest")


def _container_env() -> list[str]:
    """Build env vars to forward into the container."""
    keys = [
        "AMP_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
    ]
    env = [
        f"AI_V2_API_URL={os.getenv('AI_V2_API_URL', 'http://localhost:8000')}",
        f"AI_V2_API_KEY={os.getenv('API_SECRET_KEY', '')}",
    ]
    for k in keys:
        v = os.getenv(k, "")
        if v:
            env.append(f"{k}={v}")
    # Codex exec uses CODEX_API_KEY (falls back to OPENAI_API_KEY internally,
    # but setting it explicitly avoids issues with some versions)
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if openai_key and not os.getenv("CODEX_API_KEY"):
        env.append(f"CODEX_API_KEY={openai_key}")
    return env


def _build_command(harness: str, message: str, thread_id: str | None) -> list[str]:
    if harness == "claude-code":
        return [
            "claude",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
            *(["--session-id", thread_id] if thread_id else []),
            "-p",
            message,
        ]
    if harness == "codex":
        return [
            "codex",
            "exec",
            "--json",
            "--full-auto",
            "--skip-git-repo-check",
            *(["resume", thread_id] if thread_id else []),
            message,
        ]
    # Default: amp
    return [
        "amp",
        "--no-ide",
        "--no-notifications",
        "--dangerously-allow-all",
        "--stream-json",
        *(["threads", "continue", thread_id] if thread_id else []),
        "-x",
        message,
    ]


def _extract_result(
    raw_lines: list[str], harness: str, stderr_lines: list[str] | None = None
) -> tuple[str, str | None]:
    """Parse JSON-line output from a harness CLI.

    Returns (result_text, agent_thread_id).
    """
    result_text = ""
    agent_thread_id: str | None = None

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Codex normalization
        if harness == "codex":
            etype = event.get("type", "")
            if etype == "thread.started":
                agent_thread_id = event.get("thread_id")
            elif etype == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    result_text = item.get("text", result_text)
            elif etype == "turn.completed":
                # Some codex versions bundle items in turn.completed
                for item in event.get("items", []):
                    if item.get("type") == "agent_message":
                        result_text = item.get("text", result_text)
            elif etype == "error":
                result_text = f"❌ {event.get('message', 'Unknown error')}"
            continue

        # Amp / claude-code format
        etype = event.get("type", "")
        if etype == "system" and event.get("subtype") == "init":
            agent_thread_id = event.get("session_id")
        elif etype == "result":
            result_text = event.get("result", result_text)
        elif etype == "assistant" and event.get("message", {}).get("content"):
            for part in event["message"]["content"]:
                if part.get("type") == "text" and part.get("text"):
                    result_text = part["text"]
        elif etype == "error":
            result_text = f"❌ {event.get('error', 'Unknown error')}"

    # Fallback: if no structured output found, use last non-empty stderr
    if not result_text and stderr_lines:
        tail = [line for line in stderr_lines[-10:] if line.strip()]
        if tail:
            result_text = "❌ Agent produced no output. Stderr:\n" + "\n".join(tail)

    return result_text, agent_thread_id


class AgentClient:
    """Manage Docker sandbox containers for agent harness execution."""

    def spawn(
        self,
        slack_thread_key: str,
        harness: str = "amp",
        repo: str | None = None,
    ) -> dict[str, Any]:
        """Spawn a new sandbox container for a Slack thread.

        Args:
            slack_thread_key: Unique thread ID (e.g. "C04ABC:1234567890.123456")
            harness: Agent CLI to use — amp, claude-code, or codex
            repo: Optional repo path to set as working directory
        """
        if harness not in HARNESSES:
            raise RuntimeError(f"Unknown harness: {harness}. Use one of {HARNESSES}")

        # Reuse existing container if alive
        existing = _sessions.get(slack_thread_key)
        if existing:
            try:
                client = _docker_client()
                container = client.containers.get(existing["container_id"])
                if container.status == "running":
                    return {
                        "session_id": slack_thread_key,
                        "container_id": existing["container_id"],
                        "status": "already_running",
                        "harness": existing["harness"],
                    }
                container.start()
                existing["state"] = "running"
                return {
                    "session_id": slack_thread_key,
                    "container_id": existing["container_id"],
                    "status": "restarted",
                    "harness": existing["harness"],
                }
            except NotFound:
                del _sessions[slack_thread_key]

        client = _docker_client()
        workdir = f"/home/agent/github/{repo}" if repo else "/home/agent/github"

        container = client.containers.run(
            _image(),
            detach=True,
            stdin_open=True,
            tty=False,
            network_mode="host",
            mem_limit="4g",
            nano_cpus=int(2 * 1e9),
            environment=_container_env(),
            working_dir=workdir,
            labels={
                "tempo.agent": "true",
                "tempo.thread": slack_thread_key,
                "tempo.harness": harness,
            },
            name=f"tempo-agent-{slack_thread_key.replace(':', '-')[:40]}",
        )

        session = {
            "container_id": container.id,
            "harness": harness,
            "agent_thread_id": None,
            "state": "running",
            "created_at": time.time(),
            "last_activity": time.time(),
            "turns": [],
        }
        _sessions[slack_thread_key] = session
        _persist_session(session, slack_thread_key)

        return {
            "session_id": slack_thread_key,
            "container_id": container.id,
            "status": "started",
            "harness": harness,
        }

    def execute(
        self,
        slack_thread_key: str,
        message: str,
    ) -> dict[str, Any]:
        """Execute a message in an existing sandbox and return the result.

        Runs the harness CLI via docker exec, waits for completion,
        and returns the final result text.
        """
        session = _sessions.get(slack_thread_key)
        if not session:
            raise RuntimeError(f"No session for thread '{slack_thread_key}'. Call spawn() first.")

        client = _docker_client()
        try:
            container = client.containers.get(session["container_id"])
        except NotFound:
            del _sessions[slack_thread_key]
            raise RuntimeError("Container is gone. Call spawn() to create a new one.") from None

        cmd = _build_command(session["harness"], message, session["agent_thread_id"])

        session["state"] = "working"
        session["last_activity"] = time.time()
        started_ts = time.time()
        log.info(
            "agent_exec_start",
            thread=slack_thread_key,
            harness=session["harness"],
            cmd=cmd[:5],
        )

        # Use low-level exec API for streaming
        api = client.api
        exec_id = api.exec_create(
            container.id,
            cmd,
            stdout=True,
            stderr=True,
        )["Id"]

        output = api.exec_start(exec_id, stream=True, demux=True)

        # Collect stdout and stderr separately
        stdout_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        stderr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        lines: list[str] = []
        stderr_lines: list[str] = []
        buf = ""
        err_buf = ""
        timed_out = False
        started = time.monotonic()

        for stdout_chunk, stderr_chunk in output:
            if time.monotonic() - started > EXEC_TIMEOUT:
                timed_out = True
                log.warning("agent_exec_timeout", thread=slack_thread_key, timeout=EXEC_TIMEOUT)
                break
            if stdout_chunk:
                buf += stdout_decoder.decode(stdout_chunk)
                while "\n" in buf:
                    idx = buf.index("\n")
                    lines.append(buf[:idx])
                    buf = buf[idx + 1 :]
            if stderr_chunk:
                err_buf += stderr_decoder.decode(stderr_chunk)
                while "\n" in err_buf:
                    idx = err_buf.index("\n")
                    stderr_lines.append(err_buf[:idx])
                    err_buf = err_buf[idx + 1 :]

        # Flush remaining buffers
        if buf.strip():
            lines.append(buf)
        if err_buf.strip():
            stderr_lines.append(err_buf)

        # Capture events for thread viewer
        turn_events = []
        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                turn_events.append(json.loads(stripped))
            except json.JSONDecodeError:
                turn_events.append({"type": "raw", "text": stripped})

        # If timed out, kill the exec process
        if timed_out:
            with contextlib.suppress(Exception):
                container.exec_run(["pkill", "-TERM", "-f", session["harness"]], detach=True)

        # Check exec exit code
        exit_code = api.exec_inspect(exec_id).get("ExitCode")

        result_text, agent_thread_id = _extract_result(lines, session["harness"], stderr_lines)

        if timed_out and not result_text:
            result_text = f"❌ Agent timed out after {EXEC_TIMEOUT}s."
        elif exit_code and exit_code != 0 and not result_text:
            result_text = f"❌ Agent exited with code {exit_code}."
            if stderr_lines:
                tail = "\n".join(stderr_lines[-5:])
                result_text += f"\n```\n{tail}\n```"

        if agent_thread_id:
            session["agent_thread_id"] = agent_thread_id

        # Store turn for thread viewer
        turn = {
            "turn_id": len(session.get("turns", [])) + 1,
            "user_message": message,
            "events": turn_events,
            "result": result_text,
            "started_at": started_ts,
            "finished_at": time.time(),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_s": round(time.time() - started_ts, 1),
        }
        session.setdefault("turns", []).append(turn)
        _persist_turn(slack_thread_key, turn)

        session["state"] = "idle"
        session["last_activity"] = time.time()
        _persist_session(session, slack_thread_key)
        log.info(
            "agent_exec_done",
            thread=slack_thread_key,
            exit_code=exit_code,
            timed_out=timed_out,
            result_len=len(result_text),
        )

        return {
            "session_id": slack_thread_key,
            "result": result_text,
            "agent_thread_id": session["agent_thread_id"],
            "harness": session["harness"],
        }

    def status(self, slack_thread_key: str | None = None) -> dict[str, Any]:
        """Get session status. If no key given, list all sessions."""
        if slack_thread_key:
            session = _sessions.get(slack_thread_key)
            if not session:
                return {"error": f"No session for '{slack_thread_key}'"}
            return {
                "session_id": slack_thread_key,
                **session,
            }

        return {
            "sessions": [{"session_id": k, **v} for k, v in _sessions.items()],
            "count": len(_sessions),
        }

    def stop(self, slack_thread_key: str) -> dict[str, Any]:
        """Stop and remove a sandbox container."""
        session = _sessions.get(slack_thread_key)
        if not session:
            return {"error": f"No session for '{slack_thread_key}'"}

        client = _docker_client()
        try:
            container = client.containers.get(session["container_id"])
            container.stop(timeout=5)
            container.remove()
        except Exception:
            pass

        del _sessions[slack_thread_key]
        _delete_session(slack_thread_key)
        return {"session_id": slack_thread_key, "status": "stopped"}

    def threads(self) -> dict[str, Any]:
        """List all agent threads with summary info for the thread viewer."""
        # Recover any running containers not in _sessions (e.g. after API restart)
        self._recover_docker_sessions()
        result = []
        for key, session in _sessions.items():
            turns = session.get("turns", [])
            result.append(
                {
                    "slack_thread_key": key,
                    "container_id": session["container_id"][:12],
                    "harness": session["harness"],
                    "agent_thread_id": session.get("agent_thread_id"),
                    "state": session["state"],
                    "created_at": session["created_at"],
                    "last_activity": session["last_activity"],
                    "turn_count": len(turns),
                    "last_result": turns[-1]["result"][:200] if turns else "",
                }
            )
        return {"threads": result, "count": len(result)}

    def thread_detail(self, slack_thread_key: str) -> dict[str, Any]:
        """Get full event stream for a specific thread including all turns and tool calls."""
        session = _sessions.get(slack_thread_key)
        if not session:
            return {"error": f"No session for '{slack_thread_key}'"}
        return {
            "slack_thread_key": slack_thread_key,
            "container_id": session["container_id"][:12],
            "harness": session["harness"],
            "agent_thread_id": session.get("agent_thread_id"),
            "state": session["state"],
            "created_at": session["created_at"],
            "last_activity": session["last_activity"],
            "turns": session.get("turns", []),
        }

    def _recover_docker_sessions(self) -> None:
        """Discover running agent containers not yet tracked in _sessions."""
        try:
            client = _docker_client()
            containers = client.containers.list(filters={"label": "tempo.agent=true"})
            for container in containers:
                key = container.labels.get("tempo.thread", "")
                if key and key not in _sessions:
                    _sessions[key] = {
                        "container_id": container.id,
                        "harness": container.labels.get("tempo.harness", "amp"),
                        "agent_thread_id": None,
                        "state": container.status,
                        "created_at": time.time(),
                        "last_activity": time.time(),
                        "turns": [],
                    }
        except Exception:
            pass

    def interrupt(self, slack_thread_key: str) -> dict[str, Any]:
        """Interrupt the currently running command in a sandbox."""
        session = _sessions.get(slack_thread_key)
        if not session:
            return {"error": f"No session for '{slack_thread_key}'"}

        client = _docker_client()
        try:
            container = client.containers.get(session["container_id"])
            harness = session["harness"]
            target = {
                "amp": "amp",
                "claude-code": "claude",
                "codex": "codex",
            }.get(harness, "amp")
            container.exec_run(["pkill", "-INT", "-f", target], detach=True)
        except Exception:
            pass

        return {"session_id": slack_thread_key, "status": "interrupted"}


def _client() -> AgentClient:
    return AgentClient()
