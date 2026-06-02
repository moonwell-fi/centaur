use std::{
    collections::{HashMap, VecDeque},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use centaur_sandbox_core::{
    Mount, SandboxBackend, SandboxError, SandboxId, SandboxIoGuard, SandboxRead, SandboxSpec,
    SandboxStatus, SandboxWrite,
};
use centaur_sandbox_manager::SandboxManager;
use centaur_session_core::{
    HarnessType, Session, SessionEvent, SessionExecution, SessionMessageInput, ThreadKey,
};
use centaur_session_sqlx::{
    PgSessionStore, SessionEventListener, SessionStoreError, default_metadata,
};
use futures_util::{SinkExt, Stream, StreamExt, stream};
use serde_json::{Value, json};
use thiserror::Error;
use tokio::{
    io,
    sync::Mutex,
    time::{Instant, Interval, MissedTickBehavior, interval, interval_at, sleep, timeout},
};
use tokio_util::codec::{FramedRead, FramedWrite, LinesCodec, LinesCodecError};
use tracing::warn;

pub const SESSION_OUTPUT_LINE_EVENT: &str = "session.output.line";

const MAX_SESSION_OUTPUT_LINE_BYTES: usize = 1024 * 1024;
const EVENT_STREAM_SAFETY_POLL_INTERVAL: Duration = Duration::from_secs(30);

type SandboxSpecFactory = Arc<dyn Fn(&ThreadKey, &str) -> SandboxSpec + Send + Sync>;
type SessionInputSink = FramedWrite<SandboxWrite, LinesCodec>;

#[derive(Clone)]
pub struct SessionRuntime {
    store: PgSessionStore,
    sandbox_runtime: SandboxRuntime,
    sandbox_pipes: Arc<Mutex<HashMap<String, SessionPipe>>>,
    pipe_owner_id: Arc<str>,
    options: SessionRuntimeOptions,
}

#[derive(Clone)]
pub struct SandboxRuntime {
    manager: Arc<SandboxManager>,
    spec_factory: SandboxSpecFactory,
}

#[derive(Clone, Debug)]
pub enum SandboxWorkloadMode {
    MockAppServer {
        image: String,
        turns_per_input: u32,
        event_delay_ms: u64,
    },
    CodexAppServer {
        image: String,
        env: Vec<(String, String)>,
        mounts: Vec<Mount>,
    },
}

#[derive(Debug)]
pub struct ExecuteSessionInput {
    pub metadata: Option<Value>,
    pub input_lines: Vec<String>,
    pub idle_timeout_ms: Option<u64>,
    pub max_duration_ms: Option<u64>,
}

#[derive(Clone, Debug)]
pub struct SessionRuntimeOptions {
    pub pipe_lease_ttl: Duration,
    pub pipe_lease_renew_interval: Duration,
}

impl Default for SessionRuntimeOptions {
    fn default() -> Self {
        Self {
            pipe_lease_ttl: Duration::from_secs(45),
            pipe_lease_renew_interval: Duration::from_secs(15),
        }
    }
}

#[derive(Clone)]
struct SessionPipe {
    stdin: Arc<Mutex<SessionInputSink>>,
    lease: Arc<PipeLeaseState>,
    active_inputs: Arc<AtomicUsize>,
}

#[derive(Debug, Default)]
struct PipeLeaseState {
    stop: AtomicBool,
    lost: AtomicBool,
}

impl PipeLeaseState {
    fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }

    fn is_stopped(&self) -> bool {
        self.stop.load(Ordering::Relaxed)
    }

    fn mark_lost(&self) {
        self.lost.store(true, Ordering::Relaxed);
    }

    fn is_lost(&self) -> bool {
        self.lost.load(Ordering::Relaxed)
    }
}

struct EventStreamState {
    store: PgSessionStore,
    thread_key: ThreadKey,
    after_event_id: i64,
    pending: VecDeque<SessionEvent>,
    listener: SessionEventListener,
    safety_tick: Interval,
    done: bool,
}

impl SessionRuntime {
    pub fn new(store: PgSessionStore, sandbox_runtime: SandboxRuntime) -> Self {
        Self::with_options(store, sandbox_runtime, SessionRuntimeOptions::default())
    }

    pub fn with_options(
        store: PgSessionStore,
        sandbox_runtime: SandboxRuntime,
        options: SessionRuntimeOptions,
    ) -> Self {
        Self {
            store,
            sandbox_runtime,
            sandbox_pipes: Arc::new(Mutex::new(HashMap::new())),
            pipe_owner_id: Arc::from(default_pipe_owner_id()),
            options,
        }
    }

    pub async fn create_or_get_session(
        &self,
        thread_key: &ThreadKey,
        harness_type: &HarnessType,
        metadata: Option<Value>,
    ) -> Result<Session, SessionRuntimeError> {
        Ok(self
            .store
            .create_or_get_session(thread_key, harness_type, default_metadata(metadata))
            .await?)
    }

    pub async fn append_messages(
        &self,
        thread_key: &ThreadKey,
        messages: &[SessionMessageInput],
    ) -> Result<Vec<String>, SessionRuntimeError> {
        if messages.is_empty() {
            return Err(SessionRuntimeError::BadRequest(
                "messages must not be empty".to_owned(),
            ));
        }
        Ok(self.store.append_messages(thread_key, messages).await?)
    }

    pub async fn active_pipe_count(&self) -> usize {
        self.sandbox_pipes.lock().await.len()
    }

    pub async fn active_input_count(&self) -> usize {
        self.sandbox_pipes
            .lock()
            .await
            .values()
            .map(|pipe| pipe.active_inputs.load(Ordering::Relaxed))
            .sum()
    }

    pub async fn wait_for_no_active_inputs(&self, wait_timeout: Duration) -> bool {
        if wait_timeout.is_zero() {
            return self.active_input_count().await == 0;
        }

        timeout(wait_timeout, async {
            loop {
                if self.active_input_count().await == 0 {
                    return;
                }
                sleep(Duration::from_millis(250)).await;
            }
        })
        .await
        .is_ok()
    }

    pub async fn execute_session(
        &self,
        thread_key: &ThreadKey,
        input: ExecuteSessionInput,
    ) -> Result<SessionExecution, SessionRuntimeError> {
        let session = self.store.get_session(thread_key).await?;
        validate_input_lines(&input.input_lines)?;
        validate_duration_options(&input)?;

        let execution = self
            .store
            .create_execution(thread_key, default_metadata(input.metadata))
            .await?;
        let execution = self
            .store
            .mark_execution_running(&execution.execution_id)
            .await?;
        let sandbox_id = self
            .ensure_session_sandbox(
                thread_key,
                session.sandbox_id.as_deref(),
                &execution.execution_id,
            )
            .await?;

        self.store
            .append_event(
                thread_key,
                Some(&execution.execution_id),
                "session.execution_started",
                json!({
                    "execution_id": execution.execution_id,
                    "thread_key": thread_key.as_str(),
                    "input_line_count": input.input_lines.len(),
                }),
            )
            .await?;

        let write_result = match self.ensure_session_pipe(thread_key, &sandbox_id).await {
            Ok(pipe) => write_input_lines(&pipe, &input.input_lines).await,
            Err(error) => Err(error),
        };

        match write_result {
            Ok(()) => {}
            Err(error) => {
                let error_message = error.to_string();
                let _ = self
                    .store
                    .append_event(
                        thread_key,
                        Some(&execution.execution_id),
                        "session.execution_failed",
                        json!({
                            "execution_id": execution.execution_id,
                            "thread_key": thread_key.as_str(),
                            "error": error_message,
                        }),
                    )
                    .await;
                let _ = self
                    .store
                    .fail_execution(&execution.execution_id, &error_message)
                    .await;
                return Err(error);
            }
        }

        self.store
            .append_event(
                thread_key,
                Some(&execution.execution_id),
                "session.execution_completed",
                json!({
                    "execution_id": execution.execution_id,
                    "thread_key": thread_key.as_str(),
                    "completion_reason": "input_accepted",
                }),
            )
            .await?;

        Ok(self
            .store
            .complete_execution(&execution.execution_id)
            .await?)
    }

    pub async fn stream_events(
        &self,
        thread_key: &ThreadKey,
        after_event_id: i64,
    ) -> Result<
        impl Stream<Item = Result<SessionEvent, SessionRuntimeError>> + use<>,
        SessionRuntimeError,
    > {
        let session = self.store.get_session(thread_key).await?;
        if let Some(sandbox_id) = session.sandbox_id.as_deref() {
            match self.ensure_session_pipe(thread_key, sandbox_id).await {
                Ok(_) => {}
                Err(SessionRuntimeError::PipeLeaseUnavailable { sandbox_id }) => {
                    warn!(%sandbox_id, "session pipe is owned by another api replica");
                    let runtime = self.clone();
                    let thread_key = thread_key.clone();
                    let reclaim_delay = self.options.pipe_lease_ttl;
                    tokio::spawn(async move {
                        sleep(reclaim_delay).await;
                        match runtime.ensure_session_pipe(&thread_key, &sandbox_id).await {
                            Ok(_) | Err(SessionRuntimeError::PipeLeaseUnavailable { .. }) => {}
                            Err(error) => {
                                warn!(%sandbox_id, %error, "delayed session pipe reclaim failed");
                            }
                        }
                    });
                }
                Err(error) => return Err(error),
            }
        }

        let listener = self.store.listen_session_events().await?;

        Ok(session_event_stream(
            self.store.clone(),
            thread_key.clone(),
            after_event_id,
            listener,
        ))
    }

    async fn ensure_session_sandbox(
        &self,
        thread_key: &ThreadKey,
        existing_sandbox_id: Option<&str>,
        execution_id: &str,
    ) -> Result<String, SessionRuntimeError> {
        if let Some(sandbox_id) = existing_sandbox_id {
            let id = SandboxId::new(sandbox_id);
            match self.sandbox_runtime.manager.status(&id).await {
                Ok(SandboxStatus::Running | SandboxStatus::Created) => {
                    return Ok(sandbox_id.to_owned());
                }
                Ok(_) | Err(SandboxError::NotFound(_)) => {}
                Err(error) => return Err(SessionRuntimeError::Sandbox(error)),
            }
        }

        let spec = (self.sandbox_runtime.spec_factory)(thread_key, execution_id);
        let handle = self.sandbox_runtime.manager.create_running(spec).await?;
        self.store
            .update_sandbox_id(thread_key, Some(handle.id.as_str()))
            .await?;
        Ok(handle.id.into_string())
    }

    async fn ensure_session_pipe(
        &self,
        thread_key: &ThreadKey,
        sandbox_id: &str,
    ) -> Result<SessionPipe, SessionRuntimeError> {
        {
            let mut pipes = self.sandbox_pipes.lock().await;
            if let Some(pipe) = pipes.get(sandbox_id).cloned() {
                if !pipe.lease.is_lost() {
                    return Ok(pipe);
                }
                pipes.remove(sandbox_id);
            }
        }

        let owner_id = self.pipe_owner_id.to_string();
        let claimed = self
            .store
            .try_claim_pipe_lease(
                thread_key,
                sandbox_id,
                &owner_id,
                self.options.pipe_lease_ttl,
            )
            .await?;
        if !claimed {
            return Err(SessionRuntimeError::PipeLeaseUnavailable {
                sandbox_id: sandbox_id.to_owned(),
            });
        }

        let io = match self
            .sandbox_runtime
            .manager
            .open_io(&SandboxId::new(sandbox_id))
            .await
        {
            Ok(io) => io.into_parts(),
            Err(error) => {
                let _ = self
                    .store
                    .release_pipe_lease(thread_key, sandbox_id, &owner_id)
                    .await;
                return Err(error.into());
            }
        };
        let lease = Arc::new(PipeLeaseState::default());
        let active_inputs = Arc::new(AtomicUsize::new(0));
        let pipe = SessionPipe {
            stdin: Arc::new(Mutex::new(FramedWrite::new(
                io.stdin,
                LinesCodec::new_with_max_length(MAX_SESSION_OUTPUT_LINE_BYTES),
            ))),
            lease: lease.clone(),
            active_inputs: active_inputs.clone(),
        };

        self.sandbox_pipes
            .lock()
            .await
            .insert(sandbox_id.to_owned(), pipe.clone());
        let store = self.store.clone();
        let thread_key = thread_key.clone();
        let pump_key = sandbox_id.to_owned();
        let sandbox_pipes = self.sandbox_pipes.clone();
        let stdout = io.stdout;
        let stderr = io.stderr;
        let guard = io.guard;
        let stderr_key = pump_key.clone();
        let renew_store = store.clone();
        let renew_thread_key = thread_key.clone();
        let renew_key = pump_key.clone();
        let renew_owner_id = owner_id.clone();
        let renew_lease = lease.clone();
        let lease_ttl = self.options.pipe_lease_ttl;
        let renew_interval = self.options.pipe_lease_renew_interval;

        tokio::spawn(async move {
            renew_pipe_lease_until_stopped(
                renew_store,
                renew_thread_key,
                renew_key,
                renew_owner_id,
                renew_lease,
                lease_ttl,
                renew_interval,
            )
            .await;
        });

        tokio::spawn(async move {
            let result = run_stdout_pump(
                store.clone(),
                thread_key.clone(),
                &pump_key,
                stdout,
                guard,
                lease.clone(),
                active_inputs,
            )
            .await;
            if let Err(error) = result {
                warn!(%pump_key, %error, "session stdout pump failed");
                let _ = store
                    .append_event(
                        &thread_key,
                        None,
                        "session.stdout_pump_failed",
                        json!({
                            "sandbox_id": pump_key.as_str(),
                            "error": error.to_string(),
                        }),
                    )
                    .await;
            }
            lease.stop();
            let _ = store
                .release_pipe_lease(&thread_key, &pump_key, &owner_id)
                .await;
            sandbox_pipes.lock().await.remove(&pump_key);
        });

        tokio::spawn(async move {
            if let Err(error) = drain_stderr(stderr).await {
                warn!(%stderr_key, %error, "session stderr drain failed");
            }
        });

        Ok(pipe)
    }
}

impl SandboxRuntime {
    pub fn backend(backend: Arc<dyn SandboxBackend>, spec: SandboxSpec) -> Self {
        let spec_factory = move |_thread_key: &ThreadKey, _execution_id: &str| spec.clone();
        Self::backend_with_spec_factory(backend, spec_factory)
    }

    pub fn backend_with_workload(
        backend: Arc<dyn SandboxBackend>,
        workload: SandboxWorkloadMode,
    ) -> Self {
        Self::backend_with_spec_factory(backend, move |thread_key, _execution_id| {
            workload.spec(thread_key)
        })
    }

    pub fn backend_with_spec_factory<F>(backend: Arc<dyn SandboxBackend>, spec_factory: F) -> Self
    where
        F: Fn(&ThreadKey, &str) -> SandboxSpec + Send + Sync + 'static,
    {
        Self {
            manager: Arc::new(SandboxManager::new(backend)),
            spec_factory: Arc::new(spec_factory),
        }
    }
}

impl SandboxWorkloadMode {
    pub fn mock_app_server(image: impl Into<String>) -> Self {
        Self::mock_app_server_with_options(image, 3, 200)
    }

    pub fn mock_app_server_with_options(
        image: impl Into<String>,
        turns_per_input: u32,
        event_delay_ms: u64,
    ) -> Self {
        Self::MockAppServer {
            image: image.into(),
            turns_per_input,
            event_delay_ms,
        }
    }

    pub fn codex_app_server(
        image: impl Into<String>,
        env: impl IntoIterator<Item = (String, String)>,
    ) -> Self {
        Self::CodexAppServer {
            image: image.into(),
            env: env.into_iter().collect(),
            mounts: Vec::new(),
        }
    }

    pub fn mount(mut self, mount: Mount) -> Self {
        match &mut self {
            Self::MockAppServer { .. } => {}
            Self::CodexAppServer { mounts, .. } => mounts.push(mount),
        }
        self
    }

    fn spec(&self, thread_key: &ThreadKey) -> SandboxSpec {
        match self {
            Self::MockAppServer {
                image,
                turns_per_input,
                event_delay_ms,
            } => SandboxSpec::new(image)
                .command(["/bin/sh", "-lc"])
                .args([mock_app_server_script(*turns_per_input, *event_delay_ms)]),
            Self::CodexAppServer { image, env, mounts } => {
                let mut spec =
                    SandboxSpec::new(image).env("CENTAUR_THREAD_KEY", thread_key.as_str());
                for mount in mounts {
                    spec = spec.mount(mount.clone());
                }
                for (name, value) in env {
                    spec = spec.env(name.clone(), value.clone());
                }
                spec
            }
        }
    }
}

fn mock_app_server_script(turns_per_input: u32, event_delay_ms: u64) -> String {
    let delay_seconds = format!("{}.{:03}", event_delay_ms / 1_000, event_delay_ms % 1_000);
    format!(
        r#"while IFS= read -r line; do
printf '%s\n' '{{"type":"system","subtype":"wrapper_heartbeat","phase":"startup"}}'
sleep {delay_seconds}
printf '%s\n' '{{"type":"system","subtype":"wrapper_heartbeat","phase":"app_server_started"}}'
sleep {delay_seconds}
printf '%s\n' '{{"type":"thread.started","thread_id":"mock-codex-thread"}}'
sleep {delay_seconds}
turn_id="mock-turn-1"
printf '{{"type":"turn.started","turn_id":"%s"}}\n' "$turn_id"
sleep {delay_seconds}
delta_index=1
while [ "$delta_index" -le {turns_per_input} ]; do
  printf '{{"type":"item.agentMessage.delta","turnId":"%s","session_id":"mock-codex-thread","delta":"PONG %s"}}\n' "$turn_id" "$delta_index"
  sleep {delay_seconds}
  delta_index=$((delta_index + 1))
done
printf '{{"type":"turn.completed","turn":{{"id":"%s"}},"usage":{{"input_tokens":0,"output_tokens":1}}}}\n' "$turn_id"
sleep {delay_seconds}
done"#
    )
}

fn session_event_stream(
    store: PgSessionStore,
    thread_key: ThreadKey,
    after_event_id: i64,
    listener: SessionEventListener,
) -> impl Stream<Item = Result<SessionEvent, SessionRuntimeError>> {
    stream::unfold(
        EventStreamState {
            store,
            thread_key,
            after_event_id,
            pending: VecDeque::new(),
            listener,
            safety_tick: {
                let mut tick = interval_at(
                    Instant::now() + EVENT_STREAM_SAFETY_POLL_INTERVAL,
                    EVENT_STREAM_SAFETY_POLL_INTERVAL,
                );
                tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
                tick
            },
            done: false,
        },
        |mut state| async move {
            loop {
                if let Some(event) = state.pending.pop_front() {
                    state.after_event_id = event.event_id;
                    return Some((Ok(event), state));
                }
                if state.done {
                    return None;
                }
                match state
                    .store
                    .list_events_after(&state.thread_key, state.after_event_id, 100)
                    .await
                {
                    Ok(events) if events.is_empty() => loop {
                        tokio::select! {
                            notification = state.listener.recv() => {
                                match notification {
                                    Ok(notification)
                                        if notification.thread_key == state.thread_key.as_str()
                                            && notification.event_id > state.after_event_id =>
                                    {
                                        break;
                                    }
                                    Ok(_) => {}
                                    Err(error) => {
                                        state.done = true;
                                        return Some((Err(SessionRuntimeError::Store(error)), state));
                                    }
                                }
                            }
                            _ = state.safety_tick.tick() => break,
                        }
                    },
                    Ok(events) => state.pending = events.into(),
                    Err(error) => {
                        state.done = true;
                        return Some((Err(SessionRuntimeError::Store(error)), state));
                    }
                }
            }
        },
    )
}

async fn run_stdout_pump(
    store: PgSessionStore,
    thread_key: ThreadKey,
    sandbox_id: &str,
    stdout: SandboxRead,
    _guard: SandboxIoGuard,
    lease: Arc<PipeLeaseState>,
    active_inputs: Arc<AtomicUsize>,
) -> Result<(), SessionRuntimeError> {
    let mut stdout = FramedRead::new(
        stdout,
        LinesCodec::new_with_max_length(MAX_SESSION_OUTPUT_LINE_BYTES),
    );
    while let Some(line) = stdout.next().await {
        let line = line.map_err(codec_error_to_runtime)?;
        if lease.is_lost() {
            break;
        }
        append_output_line(&store, &thread_key, None, &line).await?;
        if is_terminal_output_line(&line) {
            mark_one_input_complete(&active_inputs);
        }
    }
    store
        .append_event(
            &thread_key,
            None,
            "session.stdout_eof",
            json!({
                "sandbox_id": sandbox_id,
            }),
        )
        .await?;
    Ok(())
}

async fn renew_pipe_lease_until_stopped(
    store: PgSessionStore,
    thread_key: ThreadKey,
    sandbox_id: String,
    owner_id: String,
    lease: Arc<PipeLeaseState>,
    lease_ttl: Duration,
    renew_interval: Duration,
) {
    let mut tick = interval(renew_interval);
    tick.set_missed_tick_behavior(MissedTickBehavior::Delay);

    loop {
        tick.tick().await;
        if lease.is_stopped() {
            return;
        }
        match store
            .renew_pipe_lease(&thread_key, &sandbox_id, &owner_id, lease_ttl)
            .await
        {
            Ok(true) => {}
            Ok(false) => {
                lease.mark_lost();
                warn!(%sandbox_id, "session pipe lease was lost to another api replica");
                return;
            }
            Err(error) => {
                lease.mark_lost();
                warn!(%sandbox_id, %error, "failed to renew session pipe lease");
                return;
            }
        }
    }
}

async fn drain_stderr(mut stderr: SandboxRead) -> Result<(), SessionRuntimeError> {
    io::copy(&mut stderr, &mut io::sink())
        .await
        .map_err(|err| {
            SessionRuntimeError::Sandbox(SandboxError::Io(format!("drain stderr: {err}")))
        })?;
    Ok(())
}

async fn write_input_lines(
    pipe: &SessionPipe,
    input_lines: &[String],
) -> Result<(), SessionRuntimeError> {
    let mut stdin = pipe.stdin.lock().await;
    for line in input_lines {
        pipe.active_inputs.fetch_add(1, Ordering::Relaxed);
        if let Err(error) = stdin.send(line).await {
            mark_one_input_complete(&pipe.active_inputs);
            return Err(codec_error_to_runtime(error));
        }
    }
    Ok(())
}

fn mark_one_input_complete(active_inputs: &AtomicUsize) {
    let _ = active_inputs.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |count| {
        count.checked_sub(1)
    });
}

fn is_terminal_output_line(line: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(line) else {
        return false;
    };
    matches!(
        value.get("type").and_then(Value::as_str),
        Some("turn.completed" | "turn.done" | "result")
    )
}

async fn append_output_line(
    store: &PgSessionStore,
    thread_key: &ThreadKey,
    execution_id: Option<&str>,
    line: &str,
) -> Result<(), SessionRuntimeError> {
    store
        .append_event(
            thread_key,
            execution_id,
            SESSION_OUTPUT_LINE_EVENT,
            Value::String(line.to_owned()),
        )
        .await?;
    Ok(())
}

fn validate_input_lines(lines: &[String]) -> Result<(), SessionRuntimeError> {
    for (index, line) in lines.iter().enumerate() {
        if line.contains('\n') || line.contains('\r') {
            return Err(SessionRuntimeError::BadRequest(format!(
                "input_lines[{index}] must be one line"
            )));
        }
    }
    Ok(())
}

fn codec_error_to_runtime(error: LinesCodecError) -> SessionRuntimeError {
    SessionRuntimeError::Sandbox(SandboxError::Io(error.to_string()))
}

fn validate_duration_options(input: &ExecuteSessionInput) -> Result<(), SessionRuntimeError> {
    let idle_timeout = input
        .idle_timeout_ms
        .map(nonzero_duration_millis)
        .transpose()?;
    let max_duration = input
        .max_duration_ms
        .map(nonzero_duration_millis)
        .transpose()?;

    if let (Some(idle_timeout), Some(max_duration)) = (idle_timeout, max_duration)
        && idle_timeout > max_duration
    {
        return Err(SessionRuntimeError::BadRequest(
            "idle_timeout_ms must be less than or equal to max_duration_ms".to_owned(),
        ));
    }

    Ok(())
}

fn nonzero_duration_millis(value: u64) -> Result<Duration, SessionRuntimeError> {
    if value == 0 {
        return Err(SessionRuntimeError::BadRequest(
            "duration values must be greater than zero".to_owned(),
        ));
    }
    Ok(Duration::from_millis(value))
}

fn default_pipe_owner_id() -> String {
    let host = std::env::var("HOSTNAME").unwrap_or_else(|_| "localhost".to_owned());
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{host}-{}-{nanos}", std::process::id())
}

#[derive(Debug, Error)]
pub enum SessionRuntimeError {
    #[error("{0}")]
    BadRequest(String),
    #[error("sandbox pipe {sandbox_id} is owned by another api replica; retry shortly")]
    PipeLeaseUnavailable { sandbox_id: String },
    #[error(transparent)]
    Store(#[from] SessionStoreError),
    #[error(transparent)]
    Sandbox(#[from] SandboxError),
}

#[cfg(test)]
mod tests {
    use super::*;
    use centaur_sandbox_core::MountKind;

    #[test]
    fn codex_workload_applies_mounts_and_env() {
        let workload = SandboxWorkloadMode::codex_app_server(
            "centaur-agent:latest",
            [("CENTAUR_API_URL".to_owned(), "http://api:8000".to_owned())],
        )
        .mount(
            Mount::new(
                MountKind::Bind {
                    source_path: "/host/github".to_owned(),
                },
                "/home/agent/github",
            )
            .read_only(),
        );
        let thread_key = ThreadKey::parse("test:thread").unwrap();

        let spec = workload.spec(&thread_key);

        assert_eq!(spec.mounts.len(), 1);
        assert_eq!(spec.mounts[0].target_path, "/home/agent/github");
        assert!(spec.mounts[0].read_only);
        assert_eq!(
            spec.mounts[0].kind,
            MountKind::Bind {
                source_path: "/host/github".to_owned(),
            }
        );
        assert!(
            spec.env
                .iter()
                .any(|env| env.name == "CENTAUR_THREAD_KEY" && env.value == "test:thread")
        );
        assert!(
            spec.env
                .iter()
                .any(|env| env.name == "CENTAUR_API_URL" && env.value == "http://api:8000")
        );
    }
}
