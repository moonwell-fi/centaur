use std::{collections::HashMap, sync::Arc};

use centaur_sandbox_core::{SandboxError, SandboxId, SandboxStatus};
use centaur_session_core::{
    HarnessType, Session, SessionEvent, SessionExecution, SessionMessageInput, ThreadKey,
};
use centaur_session_sqlx::{PgSessionStore, default_metadata};
use futures_util::Stream;
use serde_json::{Value, json};
use tokio::sync::Mutex;
use tracing::warn;

use super::input::ExecuteSessionInput;
use super::sandbox::SandboxRuntime;
use crate::SessionRuntimeError;
use crate::event_stream::session_event_stream;
use crate::session_io::{SessionPipe, drain_stderr, run_stdout_pump, write_input_lines};
use crate::validation::{validate_duration_options, validate_input_lines};

#[derive(Clone)]
pub struct SessionRuntime {
    store: PgSessionStore,
    sandbox_runtime: SandboxRuntime,
    sandbox_pipes: Arc<Mutex<HashMap<String, SessionPipe>>>,
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
        Ok(self.store.append_messages(thread_key, messages).await?)
    }

    pub async fn execute_session(
        &self,
        thread_key: &ThreadKey,
        input: ExecuteSessionInput,
    ) -> Result<SessionExecution, SessionRuntimeError> {
        let session = self.store.get_session(thread_key).await?;
        validate_input_lines(&input.input_lines)?;
        validate_duration_options(input.idle_timeout_ms, input.max_duration_ms)?;

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
                &session.harness_type,
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
            self.ensure_session_pipe(thread_key, sandbox_id).await?;
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
        harness_type: &HarnessType,
        existing_sandbox_id: Option<&str>,
        execution_id: &str,
    ) -> Result<String, SessionRuntimeError> {
        if let Some(sandbox_id) = existing_sandbox_id {
            let id = SandboxId::new(sandbox_id);
            match self.sandbox_runtime.status(&id).await {
                Ok(SandboxStatus::Running | SandboxStatus::Created) => {
                    return Ok(sandbox_id.to_owned());
                }
                Ok(_) | Err(SandboxError::NotFound(_)) => {}
                Err(error) => return Err(SessionRuntimeError::Sandbox(error)),
            }
        }

        let spec = self
            .sandbox_runtime
            .spec(thread_key, harness_type, execution_id);
        let handle = self.sandbox_runtime.create_running(spec).await?;
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
        if let Some(pipe) = self.sandbox_pipes.lock().await.get(sandbox_id).cloned() {
            return Ok(pipe);
        }

        let io = self
            .sandbox_runtime
            .open_io(&SandboxId::new(sandbox_id))
            .await?
            .into_parts();
        let pipe = SessionPipe::new(io.stdin);

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

        tokio::spawn(async move {
            let result =
                run_stdout_pump(store.clone(), thread_key.clone(), &pump_key, stdout, guard).await;
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
