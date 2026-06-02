use std::{
    collections::{HashMap, VecDeque},
    sync::Arc,
    time::Duration,
};

use centaur_sandbox_core::{
    SandboxBackend, SandboxError, SandboxId, SandboxIoGuard, SandboxRead, SandboxSpec,
    SandboxStatus, SandboxWrite,
};
use centaur_sandbox_manager::SandboxManager;
use centaur_session_core::{
    ExecutionStatus, HarnessType, MessageRole, Session, SessionEvent, SessionExecution,
    SessionMessageInput, ThreadKey,
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
    time::sleep,
    time::{Instant, Interval, MissedTickBehavior, interval_at},
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
    },
    CodexAppServer {
        image: String,
        env: Vec<(String, String)>,
    },
}

#[derive(Debug)]
pub struct ExecuteSessionInput {
    pub metadata: Option<Value>,
    pub input_lines: Vec<String>,
    pub idle_timeout_ms: Option<u64>,
    pub max_duration_ms: Option<u64>,
}

#[derive(Clone)]
struct SessionPipe {
    stdin: Arc<Mutex<SessionInputSink>>,
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
        Self {
            store,
            sandbox_runtime,
            sandbox_pipes: Arc::new(Mutex::new(HashMap::new())),
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
        let message_ids = self.store.append_messages(thread_key, messages).await?;
        self.forward_messages_to_active_execution(thread_key, messages, &message_ids)
            .await;
        Ok(message_ids)
    }

    pub async fn execute_session(
        &self,
        thread_key: &ThreadKey,
        input: ExecuteSessionInput,
    ) -> Result<SessionExecution, SessionRuntimeError> {
        let ExecuteSessionInput {
            metadata,
            input_lines,
            idle_timeout_ms,
            max_duration_ms,
        } = input;
        let session = self.store.get_session(thread_key).await?;
        validate_input_lines(&input_lines)?;
        let (idle_timeout, max_duration) = duration_options(idle_timeout_ms, max_duration_ms)?;

        let execution = self
            .store
            .create_execution(
                thread_key,
                execution_metadata(metadata, idle_timeout_ms, max_duration_ms),
            )
            .await?;
        let execution = self
            .store
            .mark_execution_running(&execution.execution_id)
            .await?;
        self.store
            .append_event(
                thread_key,
                Some(&execution.execution_id),
                "session.execution_started",
                json!({
                    "execution_id": execution.execution_id,
                    "thread_key": thread_key.as_str(),
                    "input_line_count": input_lines.len(),
                    "idle_timeout_ms": idle_timeout_ms,
                    "max_duration_ms": max_duration_ms,
                }),
            )
            .await?;

        let sandbox_id = match self
            .ensure_session_sandbox(
                thread_key,
                session.sandbox_id.as_deref(),
                &execution.execution_id,
            )
            .await
        {
            Ok(sandbox_id) => sandbox_id,
            Err(error) => {
                self.record_execution_failure(thread_key, &execution.execution_id, &error)
                    .await;
                return Err(error);
            }
        };

        let pipe = match self.ensure_session_pipe(thread_key, &sandbox_id).await {
            Ok(pipe) => pipe,
            Err(error) => {
                self.record_execution_failure(thread_key, &execution.execution_id, &error)
                    .await;
                return Err(error);
            }
        };

        if let Err(error) = write_input_lines(&pipe, &input_lines).await {
            self.record_execution_failure(thread_key, &execution.execution_id, &error)
                .await;
            return Err(error);
        }

        if let Some(max_duration) = max_duration {
            spawn_max_duration_failure(
                self.store.clone(),
                self.sandbox_runtime.manager.clone(),
                self.sandbox_pipes.clone(),
                thread_key.clone(),
                execution.execution_id.clone(),
                max_duration,
                idle_timeout,
            );
        }

        Ok(execution)
    }

    async fn record_execution_failure(
        &self,
        thread_key: &ThreadKey,
        execution_id: &str,
        error: &SessionRuntimeError,
    ) {
        let error_message = error.to_string();
        let _ = self
            .store
            .append_event(
                thread_key,
                Some(execution_id),
                "session.execution_failed",
                json!({
                    "execution_id": execution_id,
                    "thread_key": thread_key.as_str(),
                    "error": error_message,
                }),
            )
            .await;
        let _ = self
            .store
            .fail_execution(execution_id, &error_message)
            .await;
    }

    async fn forward_messages_to_active_execution(
        &self,
        thread_key: &ThreadKey,
        messages: &[SessionMessageInput],
        message_ids: &[String],
    ) {
        let input_lines = steering_input_lines(thread_key, messages, message_ids);
        if input_lines.is_empty() {
            return;
        }

        let Some(execution) = (match self.store.active_execution_for_thread(thread_key).await {
            Ok(execution) => execution,
            Err(error) => {
                warn!(%thread_key, %error, "active execution lookup failed during message append");
                return;
            }
        }) else {
            return;
        };

        let session = match self.store.get_session(thread_key).await {
            Ok(session) => session,
            Err(error) => {
                self.record_steering_failure(
                    thread_key,
                    &execution.execution_id,
                    format!("get session: {error}"),
                )
                .await;
                return;
            }
        };
        let Some(sandbox_id) = session.sandbox_id.as_deref() else {
            self.record_steering_failure(
                thread_key,
                &execution.execution_id,
                "session has no sandbox assigned".to_owned(),
            )
            .await;
            return;
        };

        let pipe = match self.ensure_session_pipe(thread_key, sandbox_id).await {
            Ok(pipe) => pipe,
            Err(error) => {
                self.record_steering_failure(
                    thread_key,
                    &execution.execution_id,
                    error.to_string(),
                )
                .await;
                return;
            }
        };

        if let Err(error) = write_input_lines(&pipe, &input_lines).await {
            self.record_steering_failure(thread_key, &execution.execution_id, error.to_string())
                .await;
            return;
        }

        if let Err(error) = self
            .store
            .append_event(
                thread_key,
                Some(&execution.execution_id),
                "session.steering_delivered",
                json!({
                    "execution_id": execution.execution_id,
                    "thread_key": thread_key.as_str(),
                    "message_ids": message_ids,
                    "input_line_count": input_lines.len(),
                }),
            )
            .await
        {
            warn!(%thread_key, %error, "failed to record steering delivery");
        }
    }

    async fn record_steering_failure(
        &self,
        thread_key: &ThreadKey,
        execution_id: &str,
        error: String,
    ) {
        warn!(%thread_key, %execution_id, %error, "active steering delivery failed");
        let _ = self
            .store
            .append_event(
                thread_key,
                Some(execution_id),
                "session.steering_failed",
                json!({
                    "execution_id": execution_id,
                    "thread_key": thread_key.as_str(),
                    "error": error,
                }),
            )
            .await;
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
            self.ensure_session_pipe_if_live(thread_key, sandbox_id)
                .await?;
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
                Ok(SandboxStatus::Suspended) => {
                    self.sandbox_pipes.lock().await.remove(sandbox_id);
                    self.sandbox_runtime.manager.resume(&id).await?;
                    self.store
                        .append_event(
                            thread_key,
                            Some(execution_id),
                            "session.sandbox_resumed",
                            json!({
                                "execution_id": execution_id,
                                "thread_key": thread_key.as_str(),
                                "sandbox_id": sandbox_id,
                            }),
                        )
                        .await?;
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

    async fn ensure_session_pipe_if_live(
        &self,
        thread_key: &ThreadKey,
        sandbox_id: &str,
    ) -> Result<(), SessionRuntimeError> {
        let id = SandboxId::new(sandbox_id);
        match self.sandbox_runtime.manager.status(&id).await {
            Ok(SandboxStatus::Running | SandboxStatus::Created) => {
                self.ensure_session_pipe(thread_key, sandbox_id).await?;
            }
            Ok(SandboxStatus::Suspended | SandboxStatus::Stopped | SandboxStatus::Gone) => {}
            Ok(SandboxStatus::Unknown(_)) => {}
            Err(SandboxError::NotFound(_)) => {}
            Err(error) => return Err(SessionRuntimeError::Sandbox(error)),
        }
        Ok(())
    }

    async fn ensure_session_pipe(
        &self,
        thread_key: &ThreadKey,
        sandbox_id: &str,
    ) -> Result<SessionPipe, SessionRuntimeError> {
        if let Some(pipe) = self.sandbox_pipes.lock().await.get(sandbox_id).cloned() {
            return Ok(pipe);
        }

        let io = self
            .sandbox_runtime
            .manager
            .open_io(&SandboxId::new(sandbox_id))
            .await?
            .into_parts();
        let pipe = SessionPipe {
            stdin: Arc::new(Mutex::new(FramedWrite::new(
                io.stdin,
                LinesCodec::new_with_max_length(MAX_SESSION_OUTPUT_LINE_BYTES),
            ))),
        };

        self.sandbox_pipes
            .lock()
            .await
            .insert(sandbox_id.to_owned(), pipe.clone());
        let store = self.store.clone();
        let manager = self.sandbox_runtime.manager.clone();
        let thread_key = thread_key.clone();
        let pump_key = sandbox_id.to_owned();
        let sandbox_pipes = self.sandbox_pipes.clone();
        let stdout = io.stdout;
        let stderr = io.stderr;
        let guard = io.guard;
        let stderr_key = pump_key.clone();

        tokio::spawn(async move {
            let result = run_stdout_pump(
                store.clone(),
                manager,
                sandbox_pipes.clone(),
                thread_key.clone(),
                &pump_key,
                stdout,
                guard,
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
        Self::MockAppServer {
            image: image.into(),
        }
    }

    pub fn codex_app_server(
        image: impl Into<String>,
        env: impl IntoIterator<Item = (String, String)>,
    ) -> Self {
        Self::CodexAppServer {
            image: image.into(),
            env: env.into_iter().collect(),
        }
    }

    fn spec(&self, thread_key: &ThreadKey) -> SandboxSpec {
        match self {
            Self::MockAppServer { image } => SandboxSpec::new(image)
                .command(["/bin/sh", "-lc"])
                .args([mock_app_server_script()]),
            Self::CodexAppServer { image, env } => {
                let mut spec =
                    SandboxSpec::new(image).env("CENTAUR_THREAD_KEY", thread_key.as_str());
                for (name, value) in env {
                    spec = spec.env(name.clone(), value.clone());
                }
                spec
            }
        }
    }
}

fn mock_app_server_script() -> &'static str {
    r#"while IFS= read -r line; do
printf '%s\n' '{"type":"system","subtype":"wrapper_heartbeat","phase":"startup"}'
sleep 0.2
printf '%s\n' '{"type":"system","subtype":"wrapper_heartbeat","phase":"app_server_started"}'
sleep 0.2
printf '%s\n' '{"type":"thread.started","thread_id":"mock-codex-thread"}'
sleep 0.2
turn_index=1
while [ "$turn_index" -le 3 ]; do
  turn_id="mock-turn-$turn_index"
  printf '{"type":"turn.started","turn_id":"%s"}\n' "$turn_id"
  sleep 0.2
  printf '{"type":"item.agentMessage.delta","turnId":"%s","session_id":"mock-codex-thread","delta":"PONG %s"}\n' "$turn_id" "$turn_index"
  sleep 0.2
  printf '{"type":"turn.completed","turn":{"id":"%s"},"usage":{"input_tokens":0,"output_tokens":1}}\n' "$turn_id"
  sleep 0.2
  turn_index=$((turn_index + 1))
done
done"#
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
    manager: Arc<SandboxManager>,
    sandbox_pipes: Arc<Mutex<HashMap<String, SessionPipe>>>,
    thread_key: ThreadKey,
    sandbox_id: &str,
    stdout: SandboxRead,
    _guard: SandboxIoGuard,
) -> Result<(), SessionRuntimeError> {
    let mut stdout = FramedRead::new(
        stdout,
        LinesCodec::new_with_max_length(MAX_SESSION_OUTPUT_LINE_BYTES),
    );
    let mut output_state = StdoutPumpState::default();
    while let Some(line) = stdout.next().await {
        let line = line.map_err(codec_error_to_runtime)?;
        let active_execution = store.active_execution_for_thread(&thread_key).await?;
        let execution_id = active_execution
            .as_ref()
            .map(|execution| execution.execution_id.as_str());
        let Some(output_execution_id) = output_state.execution_for_line(execution_id, &line) else {
            continue;
        };
        append_output_line(&store, &thread_key, Some(&output_execution_id), &line).await?;
        if let Some(execution) = active_execution
            && execution.execution_id == output_execution_id
            && let Some(terminal) = output_state.observe(&output_execution_id, &line)
        {
            record_terminal_output(
                &store,
                manager.clone(),
                sandbox_pipes.clone(),
                &thread_key,
                sandbox_id,
                &output_execution_id,
                terminal,
            )
            .await?;
            output_state.forget(&output_execution_id);
        }
    }
    if let Some(execution) = store.active_execution_for_thread(&thread_key).await? {
        record_terminal_output(
            &store,
            manager,
            sandbox_pipes,
            &thread_key,
            sandbox_id,
            &execution.execution_id,
            TerminalOutput::Failed {
                error: "sandbox stdout closed before terminal output".to_owned(),
            },
        )
        .await?;
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

#[derive(Default)]
struct StdoutPumpState {
    saw_final_answer_by_execution: HashMap<String, bool>,
    turn_execution_by_id: HashMap<String, String>,
    item_execution_by_id: HashMap<String, String>,
}

impl StdoutPumpState {
    fn execution_for_line(
        &mut self,
        active_execution_id: Option<&str>,
        line: &str,
    ) -> Option<String> {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            return active_execution_id.map(ToOwned::to_owned);
        };

        if let Some(known_execution_id) = self.known_execution_for_value(&value) {
            if active_execution_id == Some(known_execution_id.as_str()) {
                self.remember_value_execution(&value, &known_execution_id);
                return Some(known_execution_id);
            }
            if terminal_output(
                &value,
                self.saw_final_answer_by_execution
                    .get(&known_execution_id)
                    .copied()
                    .unwrap_or(false),
            )
            .is_some()
            {
                self.forget(&known_execution_id);
            }
            return None;
        }

        let active_execution_id = active_execution_id?;
        self.remember_value_execution(&value, active_execution_id);
        Some(active_execution_id.to_owned())
    }

    fn observe(&mut self, execution_id: &str, line: &str) -> Option<TerminalOutput> {
        let value: Value = serde_json::from_str(line).ok()?;
        if output_line_carries_final_answer_text(&value) {
            self.saw_final_answer_by_execution
                .insert(execution_id.to_owned(), true);
        }
        terminal_output(
            &value,
            self.saw_final_answer_by_execution
                .get(execution_id)
                .copied()
                .unwrap_or(false),
        )
    }

    fn forget(&mut self, execution_id: &str) {
        self.saw_final_answer_by_execution.remove(execution_id);
        self.turn_execution_by_id
            .retain(|_, mapped_execution_id| mapped_execution_id != execution_id);
        self.item_execution_by_id
            .retain(|_, mapped_execution_id| mapped_execution_id != execution_id);
    }

    fn known_execution_for_value(&self, value: &Value) -> Option<String> {
        for turn_id in turn_ids(value) {
            if let Some(execution_id) = self.turn_execution_by_id.get(&turn_id) {
                return Some(execution_id.clone());
            }
        }
        for item_id in item_ids(value) {
            if let Some(execution_id) = self.item_execution_by_id.get(&item_id) {
                return Some(execution_id.clone());
            }
        }
        None
    }

    fn remember_value_execution(&mut self, value: &Value, execution_id: &str) {
        for turn_id in turn_ids(value) {
            self.turn_execution_by_id
                .insert(turn_id, execution_id.to_owned());
        }
        for item_id in item_ids(value) {
            self.item_execution_by_id
                .insert(item_id, execution_id.to_owned());
        }
    }
}

#[derive(Debug, Eq, PartialEq)]
enum TerminalOutput {
    Completed { reason: &'static str },
    Failed { error: String },
}

async fn record_terminal_output(
    store: &PgSessionStore,
    manager: Arc<SandboxManager>,
    sandbox_pipes: Arc<Mutex<HashMap<String, SessionPipe>>>,
    thread_key: &ThreadKey,
    sandbox_id: &str,
    execution_id: &str,
    terminal: TerminalOutput,
) -> Result<(), SessionRuntimeError> {
    let terminal_execution = match terminal {
        TerminalOutput::Completed { reason } => {
            let Some(execution) = store.complete_execution_if_active(execution_id).await? else {
                return Ok(());
            };
            store
                .append_event(
                    thread_key,
                    Some(execution_id),
                    "session.execution_completed",
                    json!({
                        "execution_id": execution_id,
                        "thread_key": thread_key.as_str(),
                        "completion_reason": reason,
                    }),
                )
                .await?;
            execution
        }
        TerminalOutput::Failed { error } => {
            let Some(execution) = store.fail_execution_if_active(execution_id, &error).await?
            else {
                return Ok(());
            };
            store
                .append_event(
                    thread_key,
                    Some(execution_id),
                    "session.execution_failed",
                    json!({
                        "execution_id": execution_id,
                        "thread_key": thread_key.as_str(),
                        "error": error.as_str(),
                    }),
                )
                .await?;
            execution
        }
    };
    if let Some(idle_timeout) = idle_timeout_from_execution(&terminal_execution) {
        spawn_idle_pause(
            store.clone(),
            manager,
            sandbox_pipes,
            thread_key.clone(),
            terminal_execution.execution_id,
            sandbox_id.to_owned(),
            idle_timeout,
        );
    }
    Ok(())
}

fn spawn_max_duration_failure(
    store: PgSessionStore,
    manager: Arc<SandboxManager>,
    sandbox_pipes: Arc<Mutex<HashMap<String, SessionPipe>>>,
    thread_key: ThreadKey,
    execution_id: String,
    max_duration: Duration,
    idle_timeout: Option<Duration>,
) {
    tokio::spawn(async move {
        sleep(max_duration).await;
        if let Err(error) = record_max_duration_failure(
            &store,
            manager,
            sandbox_pipes,
            &thread_key,
            &execution_id,
            max_duration,
            idle_timeout,
        )
        .await
        {
            warn!(%thread_key, %execution_id, %error, "max duration failure task failed");
        }
    });
}

async fn record_max_duration_failure(
    store: &PgSessionStore,
    manager: Arc<SandboxManager>,
    sandbox_pipes: Arc<Mutex<HashMap<String, SessionPipe>>>,
    thread_key: &ThreadKey,
    execution_id: &str,
    max_duration: Duration,
    idle_timeout: Option<Duration>,
) -> Result<(), SessionRuntimeError> {
    let max_duration_ms = duration_millis_u64(max_duration);
    let error = format!("execution exceeded max_duration_ms={max_duration_ms}");
    let Some(execution) = store.fail_execution_if_active(execution_id, &error).await? else {
        return Ok(());
    };
    store
        .append_event(
            thread_key,
            Some(execution_id),
            "session.execution_failed",
            json!({
                "execution_id": execution_id,
                "thread_key": thread_key.as_str(),
                "error": error,
                "reason": "max_duration_exceeded",
                "max_duration_ms": max_duration_ms,
            }),
        )
        .await?;
    if let Some(idle_timeout) = idle_timeout.or_else(|| idle_timeout_from_execution(&execution))
        && let Some(sandbox_id) = store.get_session(thread_key).await?.sandbox_id
    {
        spawn_idle_pause(
            store.clone(),
            manager,
            sandbox_pipes,
            thread_key.clone(),
            execution_id.to_owned(),
            sandbox_id,
            idle_timeout,
        );
    }
    Ok(())
}

fn spawn_idle_pause(
    store: PgSessionStore,
    manager: Arc<SandboxManager>,
    sandbox_pipes: Arc<Mutex<HashMap<String, SessionPipe>>>,
    thread_key: ThreadKey,
    execution_id: String,
    sandbox_id: String,
    idle_timeout: Duration,
) {
    tokio::spawn(async move {
        sleep(idle_timeout).await;
        if let Err(error) = record_idle_pause(
            &store,
            manager,
            sandbox_pipes,
            &thread_key,
            &execution_id,
            &sandbox_id,
            idle_timeout,
        )
        .await
        {
            warn!(%thread_key, %execution_id, %sandbox_id, %error, "idle pause task failed");
        }
    });
}

async fn record_idle_pause(
    store: &PgSessionStore,
    manager: Arc<SandboxManager>,
    sandbox_pipes: Arc<Mutex<HashMap<String, SessionPipe>>>,
    thread_key: &ThreadKey,
    execution_id: &str,
    sandbox_id: &str,
    idle_timeout: Duration,
) -> Result<(), SessionRuntimeError> {
    let latest_execution = store.latest_execution_for_thread(thread_key).await?;
    let session = store.get_session(thread_key).await?;
    if !should_pause_idle_sandbox(
        &session,
        latest_execution.as_ref(),
        execution_id,
        sandbox_id,
    ) {
        return Ok(());
    }

    let id = SandboxId::new(sandbox_id);
    match manager.status(&id).await {
        Ok(SandboxStatus::Suspended | SandboxStatus::Stopped | SandboxStatus::Gone) => {
            return Ok(());
        }
        Ok(SandboxStatus::Running | SandboxStatus::Created) => {}
        Ok(SandboxStatus::Unknown(_)) => return Ok(()),
        Err(SandboxError::NotFound(_)) => return Ok(()),
        Err(error) => {
            record_idle_pause_failure(
                store,
                thread_key,
                execution_id,
                sandbox_id,
                idle_timeout,
                &error.to_string(),
            )
            .await?;
            return Err(SessionRuntimeError::Sandbox(error));
        }
    }

    sandbox_pipes.lock().await.remove(sandbox_id);
    match manager.pause(&id).await {
        Ok(()) => {
            store
                .append_event(
                    thread_key,
                    Some(execution_id),
                    "session.sandbox_paused",
                    json!({
                        "execution_id": execution_id,
                        "thread_key": thread_key.as_str(),
                        "sandbox_id": sandbox_id,
                        "reason": "idle_timeout",
                        "idle_timeout_ms": duration_millis_u64(idle_timeout),
                    }),
                )
                .await?;
        }
        Err(error) => {
            record_idle_pause_failure(
                store,
                thread_key,
                execution_id,
                sandbox_id,
                idle_timeout,
                &error.to_string(),
            )
            .await?;
            return Err(SessionRuntimeError::Sandbox(error));
        }
    }
    Ok(())
}

async fn record_idle_pause_failure(
    store: &PgSessionStore,
    thread_key: &ThreadKey,
    execution_id: &str,
    sandbox_id: &str,
    idle_timeout: Duration,
    error: &str,
) -> Result<(), SessionRuntimeError> {
    store
        .append_event(
            thread_key,
            Some(execution_id),
            "session.sandbox_pause_failed",
            json!({
                "execution_id": execution_id,
                "thread_key": thread_key.as_str(),
                "sandbox_id": sandbox_id,
                "reason": "idle_timeout",
                "idle_timeout_ms": duration_millis_u64(idle_timeout),
                "error": error,
            }),
        )
        .await?;
    Ok(())
}

fn should_pause_idle_sandbox(
    session: &Session,
    latest_execution: Option<&SessionExecution>,
    execution_id: &str,
    sandbox_id: &str,
) -> bool {
    if session.sandbox_id.as_deref() != Some(sandbox_id) {
        return false;
    }
    let Some(execution) = latest_execution else {
        return false;
    };
    if execution.execution_id != execution_id {
        return false;
    }
    matches!(
        execution.status,
        ExecutionStatus::Completed | ExecutionStatus::Failed | ExecutionStatus::Cancelled
    )
}

fn duration_millis_u64(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}

fn terminal_output(value: &Value, saw_final_answer_text: bool) -> Option<TerminalOutput> {
    let method = value.get("method").and_then(Value::as_str);
    let event_type = value.get("type").and_then(Value::as_str);

    if matches!(method, Some("error" | "turn/failed"))
        || matches!(event_type, Some("error" | "turn.failed"))
    {
        return Some(TerminalOutput::Failed {
            error: terminal_error_text(value),
        });
    }

    if method == Some("turn/completed") {
        return success_terminal_when_answer_seen(value, saw_final_answer_text, "turn_completed");
    }

    match event_type {
        Some("turn.completed") => {
            success_terminal_when_answer_seen(value, saw_final_answer_text, "turn_completed")
        }
        Some("turn.done") => {
            success_terminal_when_answer_seen(value, saw_final_answer_text, "turn_done")
        }
        Some("result") => {
            if result_is_failure(value) {
                Some(TerminalOutput::Failed {
                    error: terminal_error_text(value),
                })
            } else {
                Some(TerminalOutput::Completed { reason: "result" })
            }
        }
        _ => None,
    }
}

fn success_terminal_when_answer_seen(
    value: &Value,
    saw_final_answer_text: bool,
    reason: &'static str,
) -> Option<TerminalOutput> {
    if terminal_payload_text(value).trim().is_empty() && !saw_final_answer_text {
        return None;
    }
    Some(TerminalOutput::Completed { reason })
}

fn output_line_carries_final_answer_text(value: &Value) -> bool {
    let method = value.get("method").and_then(Value::as_str);
    let event_type = value.get("type").and_then(Value::as_str);
    if matches!(method, Some("item/agentMessage/delta"))
        || matches!(event_type, Some("item.agentMessage.delta"))
    {
        return !terminal_payload_text(value).trim().is_empty();
    }
    if event_type == Some("assistant") {
        return !terminal_payload_text(value).trim().is_empty();
    }
    false
}

fn turn_ids(value: &Value) -> Vec<String> {
    [
        &["turn_id"][..],
        &["turnId"][..],
        &["turn", "id"][..],
        &["params", "turnId"][..],
        &["params", "turn", "id"][..],
    ]
    .into_iter()
    .filter_map(|path| string_at_path(value, path))
    .collect()
}

fn item_ids(value: &Value) -> Vec<String> {
    [
        &["item_id"][..],
        &["itemId"][..],
        &["item", "id"][..],
        &["params", "itemId"][..],
        &["params", "item", "id"][..],
    ]
    .into_iter()
    .filter_map(|path| string_at_path(value, path))
    .collect()
}

fn string_at_path(value: &Value, path: &[&str]) -> Option<String> {
    let mut current = value;
    for key in path {
        current = current.get(*key)?;
    }
    let text = current.as_str()?.trim();
    (!text.is_empty()).then(|| text.to_owned())
}

fn result_is_failure(value: &Value) -> bool {
    matches!(
        value.get("subtype").and_then(Value::as_str),
        Some("error" | "failure" | "failed")
    )
}

fn terminal_error_text(value: &Value) -> String {
    for key in ["error", "message", "result", "text"] {
        if let Some(text) = value.get(key).and_then(Value::as_str)
            && !text.trim().is_empty()
        {
            return text.trim().to_owned();
        }
    }
    terminal_payload_text(value)
        .trim()
        .to_owned()
        .if_empty("terminal harness output reported failure")
}

fn terminal_payload_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Array(values) => values
            .iter()
            .map(terminal_payload_text)
            .find(|text| !text.trim().is_empty())
            .unwrap_or_default(),
        Value::Object(object) => {
            for key in [
                "result",
                "result_text",
                "text",
                "final_text",
                "message",
                "delta",
                "content",
                "params",
            ] {
                if let Some(text) = object.get(key).map(terminal_payload_text)
                    && !text.trim().is_empty()
                {
                    return text;
                }
            }
            String::new()
        }
        _ => String::new(),
    }
}

trait StringExt {
    fn if_empty(self, fallback: &str) -> String;
}

impl StringExt for String {
    fn if_empty(self, fallback: &str) -> String {
        if self.is_empty() {
            fallback.to_owned()
        } else {
            self
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use centaur_session_core::{
        ExecutionStatus, HarnessType, MessageRole, Session, SessionExecution, SessionMessageInput,
        SessionStatus, ThreadKey,
    };
    use serde_json::json;
    use time::OffsetDateTime;

    use super::{
        StdoutPumpState, TerminalOutput, duration_millis_u64, execution_metadata,
        idle_timeout_from_execution, output_line_carries_final_answer_text,
        should_pause_idle_sandbox, steering_input_lines, terminal_output, terminal_payload_text,
    };

    #[test]
    fn turn_completed_without_answer_text_is_not_terminal() {
        let event = json!({
            "type": "turn.completed",
            "turn": {"id": "turn-1", "status": "completed"},
        });

        assert_eq!(terminal_output(&event, false), None);
    }

    #[test]
    fn turn_completed_after_answer_text_is_terminal() {
        let delta = json!({
            "method": "item/agentMessage/delta",
            "params": {"turnId": "turn-1", "delta": "Final answer"},
        });
        let terminal = json!({
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "completed"}},
        });

        assert!(output_line_carries_final_answer_text(&delta));
        assert_eq!(
            terminal_output(&terminal, true),
            Some(TerminalOutput::Completed {
                reason: "turn_completed"
            })
        );
    }

    #[test]
    fn terminal_result_completes_even_without_prior_delta() {
        let event = json!({
            "type": "result",
            "result": {"text": "Final answer"},
        });

        assert_eq!(
            terminal_output(&event, false),
            Some(TerminalOutput::Completed { reason: "result" })
        );
    }

    #[test]
    fn turn_failed_is_terminal_failure() {
        let event = json!({
            "type": "turn.failed",
            "error": "sandbox exited",
        });

        assert_eq!(
            terminal_output(&event, false),
            Some(TerminalOutput::Failed {
                error: "sandbox exited".to_owned()
            })
        );
    }

    #[test]
    fn nested_terminal_text_is_normalized() {
        let event = json!({
            "result": {
                "message": {
                    "content": [{"type": "text", "text": "Final answer"}],
                },
            },
        });

        assert_eq!(terminal_payload_text(&event), "Final answer");
    }

    #[test]
    fn timeout_event_uses_millisecond_duration() {
        assert_eq!(duration_millis_u64(Duration::from_millis(3_000)), 3_000);
    }

    #[test]
    fn execution_metadata_preserves_idle_and_max_duration() {
        let metadata =
            execution_metadata(Some(json!({"source": "test"})), Some(2_000), Some(5_000));

        assert_eq!(metadata["source"], "test");
        assert_eq!(metadata["idle_timeout_ms"], 2_000);
        assert_eq!(metadata["max_duration_ms"], 5_000);
    }

    #[test]
    fn idle_timeout_is_read_from_execution_metadata() {
        let execution = session_execution(
            "exe-idle",
            ExecutionStatus::Completed,
            json!({"idle_timeout_ms": 1500}),
        );

        assert_eq!(
            idle_timeout_from_execution(&execution),
            Some(Duration::from_millis(1500))
        );
    }

    #[test]
    fn idle_pause_requires_latest_terminal_execution_and_same_sandbox() {
        let session = session_with_sandbox("asbx-1");
        let completed = session_execution("exe-1", ExecutionStatus::Completed, json!({}));
        let running = session_execution("exe-1", ExecutionStatus::Running, json!({}));
        let newer = session_execution("exe-2", ExecutionStatus::Completed, json!({}));

        assert!(should_pause_idle_sandbox(
            &session,
            Some(&completed),
            "exe-1",
            "asbx-1"
        ));
        assert!(!should_pause_idle_sandbox(
            &session,
            Some(&running),
            "exe-1",
            "asbx-1"
        ));
        assert!(!should_pause_idle_sandbox(
            &session,
            Some(&newer),
            "exe-1",
            "asbx-1"
        ));
        assert!(!should_pause_idle_sandbox(
            &session,
            Some(&completed),
            "exe-1",
            "asbx-other"
        ));
    }

    #[test]
    fn stdout_state_drops_late_output_from_inactive_turn() {
        let mut state = StdoutPumpState::default();
        let started = r#"{"type":"turn.started","turn_id":"turn-old"}"#;
        let delta = r#"{"type":"item.agentMessage.delta","turnId":"turn-old","itemId":"msg-old","delta":"late"}"#;

        assert_eq!(
            state.execution_for_line(Some("exe-old"), started),
            Some("exe-old".to_owned())
        );
        assert_eq!(state.execution_for_line(None, delta), None);
        assert_eq!(state.execution_for_line(Some("exe-new"), delta), None);
    }

    #[test]
    fn steering_input_lines_forward_only_user_messages() {
        let thread_key = ThreadKey::parse("cli:test-steering").unwrap();
        let messages = vec![
            SessionMessageInput {
                role: MessageRole::User,
                parts: vec![json!({"type": "text", "text": "steer now"})],
                metadata: json!({"platform": "test"}),
            },
            SessionMessageInput {
                role: MessageRole::Assistant,
                parts: vec![json!({"type": "text", "text": "do not echo assistant"})],
                metadata: json!({}),
            },
        ];
        let message_ids = vec!["msg-user".to_owned(), "msg-assistant".to_owned()];

        let lines = steering_input_lines(&thread_key, &messages, &message_ids);
        assert_eq!(lines.len(), 1);

        let value: serde_json::Value = serde_json::from_str(&lines[0]).unwrap();
        assert_eq!(value["type"], "user");
        assert_eq!(value["thread_key"], "cli:test-steering");
        assert_eq!(value["trace_metadata"]["action"], "steer_active_execution");
        assert_eq!(value["trace_metadata"]["message_id"], "msg-user");
        assert_eq!(value["message"]["content"][0]["text"], "steer now");
    }

    fn session_with_sandbox(sandbox_id: &str) -> Session {
        let thread_key = ThreadKey::parse("cli:test-idle").unwrap();
        let now = OffsetDateTime::now_utc();
        Session {
            thread_key,
            sandbox_id: Some(sandbox_id.to_owned()),
            harness_type: HarnessType::Codex,
            harness_thread_id: None,
            status: SessionStatus::Idle,
            created_at: now,
            updated_at: now,
        }
    }

    fn session_execution(
        execution_id: &str,
        status: ExecutionStatus,
        metadata: serde_json::Value,
    ) -> SessionExecution {
        let thread_key = ThreadKey::parse("cli:test-idle").unwrap();
        let now = OffsetDateTime::now_utc();
        SessionExecution {
            execution_id: execution_id.to_owned(),
            thread_key,
            status,
            metadata,
            error: None,
            created_at: now,
            updated_at: now,
            started_at: Some(now),
            completed_at: Some(now),
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
        stdin.send(line).await.map_err(codec_error_to_runtime)?;
    }
    Ok(())
}

fn steering_input_lines(
    thread_key: &ThreadKey,
    messages: &[SessionMessageInput],
    message_ids: &[String],
) -> Vec<String> {
    messages
        .iter()
        .zip(message_ids)
        .filter_map(|(message, message_id)| steering_input_line(thread_key, message, message_id))
        .collect()
}

fn steering_input_line(
    thread_key: &ThreadKey,
    message: &SessionMessageInput,
    message_id: &str,
) -> Option<String> {
    if message.role != MessageRole::User {
        return None;
    }
    serde_json::to_string(&json!({
        "type": "user",
        "thread_key": thread_key.as_str(),
        "trace_metadata": {
            "source": "session.append_messages",
            "action": "steer_active_execution",
            "message_id": message_id,
            "metadata": message.metadata.clone(),
        },
        "message": {
            "role": message.role.as_ref(),
            "content": message.parts.clone(),
        },
    }))
    .ok()
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

fn duration_options(
    idle_timeout_ms: Option<u64>,
    max_duration_ms: Option<u64>,
) -> Result<(Option<Duration>, Option<Duration>), SessionRuntimeError> {
    let idle_timeout = idle_timeout_ms.map(nonzero_duration_millis).transpose()?;
    let max_duration = max_duration_ms.map(nonzero_duration_millis).transpose()?;

    if let (Some(idle_timeout), Some(max_duration)) = (idle_timeout, max_duration)
        && idle_timeout > max_duration
    {
        return Err(SessionRuntimeError::BadRequest(
            "idle_timeout_ms must be less than or equal to max_duration_ms".to_owned(),
        ));
    }

    Ok((idle_timeout, max_duration))
}

fn nonzero_duration_millis(value: u64) -> Result<Duration, SessionRuntimeError> {
    if value == 0 {
        return Err(SessionRuntimeError::BadRequest(
            "duration values must be greater than zero".to_owned(),
        ));
    }
    Ok(Duration::from_millis(value))
}

fn execution_metadata(
    metadata: Option<Value>,
    idle_timeout_ms: Option<u64>,
    max_duration_ms: Option<u64>,
) -> Value {
    let mut metadata = default_metadata(metadata);
    if let Value::Object(object) = &mut metadata {
        if let Some(value) = idle_timeout_ms {
            object.insert("idle_timeout_ms".to_owned(), json!(value));
        }
        if let Some(value) = max_duration_ms {
            object.insert("max_duration_ms".to_owned(), json!(value));
        }
    }
    metadata
}

fn idle_timeout_from_execution(execution: &SessionExecution) -> Option<Duration> {
    execution
        .metadata
        .get("idle_timeout_ms")
        .and_then(Value::as_u64)
        .and_then(|value| nonzero_duration_millis(value).ok())
}

#[derive(Debug, Error)]
pub enum SessionRuntimeError {
    #[error("{0}")]
    BadRequest(String),
    #[error(transparent)]
    Store(#[from] SessionStoreError),
    #[error(transparent)]
    Sandbox(#[from] SandboxError),
}
