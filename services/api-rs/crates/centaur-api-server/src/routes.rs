use std::{convert::Infallible, convert::TryFrom, future::Future, time::Duration};

use axum::{
    Json, Router,
    extract::{Path, Query, State},
    response::{
        Sse,
        sse::{Event, KeepAlive},
    },
    routing::{get, post},
};
use centaur_session_core::{Session, ThreadKey};
use centaur_session_runtime::{
    ExecuteSessionInput, SandboxRuntime, SessionRuntime, SessionRuntimeError,
};
use centaur_session_sqlx::PgSessionStore;
use futures_util::{Stream, StreamExt};
use serde_json::{Value, json};
use tokio::time::timeout;

use crate::{
    ApiError,
    types::{
        AppendMessagesRequest, AppendMessagesResponse, CreateSessionRequest, EventsQuery,
        ExecuteSessionRequest, ExecuteSessionResponse, SessionSseEvent, stream_error_sse,
    },
};

#[derive(Clone)]
pub struct AppState {
    runtime: SessionRuntime,
    session_operation_timeout: Duration,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SessionRouteConfig {
    pub session_operation_timeout: Duration,
}

impl Default for SessionRouteConfig {
    fn default() -> Self {
        Self {
            session_operation_timeout: Duration::from_secs(25),
        }
    }
}

pub fn build_router_with_runtime(store: PgSessionStore, sandbox_runtime: SandboxRuntime) -> Router {
    build_router_with_runtime_config(store, sandbox_runtime, SessionRouteConfig::default())
}

pub fn build_router_with_runtime_config(
    store: PgSessionStore,
    sandbox_runtime: SandboxRuntime,
    config: SessionRouteConfig,
) -> Router {
    build_router_with_session_runtime_config(SessionRuntime::new(store, sandbox_runtime), config)
}

pub fn build_router_with_session_runtime(runtime: SessionRuntime) -> Router {
    build_router_with_session_runtime_config(runtime, SessionRouteConfig::default())
}

pub fn build_router_with_session_runtime_config(
    runtime: SessionRuntime,
    config: SessionRouteConfig,
) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/api/session/{thread_key}", post(create_or_get_session))
        .route("/api/session/{thread_key}/messages", post(append_messages))
        .route("/api/session/{thread_key}/execute", post(execute_session))
        .route("/api/session/{thread_key}/events", get(stream_events))
        .with_state(AppState {
            runtime,
            session_operation_timeout: config.session_operation_timeout,
        })
}

async fn healthz() -> Json<Value> {
    Json(json!({"ok": true}))
}

async fn create_or_get_session(
    State(state): State<AppState>,
    Path(raw_thread_key): Path<String>,
    Json(request): Json<CreateSessionRequest>,
) -> Result<Json<Session>, ApiError> {
    let thread_key = ThreadKey::try_from(raw_thread_key)?;
    let session = session_operation(
        "create or get session",
        state.session_operation_timeout,
        state
            .runtime
            .create_or_get_session(&thread_key, &request.harness_type, request.metadata),
    )
    .await?;
    Ok(Json(session))
}

async fn append_messages(
    State(state): State<AppState>,
    Path(raw_thread_key): Path<String>,
    Json(request): Json<AppendMessagesRequest>,
) -> Result<Json<AppendMessagesResponse>, ApiError> {
    let thread_key = ThreadKey::try_from(raw_thread_key)?;
    let message_ids = session_operation(
        "append messages",
        state.session_operation_timeout,
        state
            .runtime
            .append_messages(&thread_key, &request.messages),
    )
    .await?;
    Ok(Json(AppendMessagesResponse {
        ok: true,
        message_ids,
    }))
}

async fn execute_session(
    State(state): State<AppState>,
    Path(raw_thread_key): Path<String>,
    Json(request): Json<ExecuteSessionRequest>,
) -> Result<Json<ExecuteSessionResponse>, ApiError> {
    let thread_key = ThreadKey::try_from(raw_thread_key)?;
    let execution = session_operation(
        "execute session",
        state.session_operation_timeout,
        state.runtime.execute_session(
            &thread_key,
            ExecuteSessionInput {
                metadata: request.metadata,
                input_lines: request.input_lines,
                idle_timeout_ms: request.idle_timeout_ms,
                max_duration_ms: request.max_duration_ms,
            },
        ),
    )
    .await?;
    Ok(Json(ExecuteSessionResponse {
        ok: true,
        execution_id: execution.execution_id,
        thread_key: execution.thread_key,
        status: execution.status.to_string(),
    }))
}

async fn stream_events(
    State(state): State<AppState>,
    Path(raw_thread_key): Path<String>,
    Query(query): Query<EventsQuery>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    let thread_key = ThreadKey::try_from(raw_thread_key)?;
    let events = session_operation(
        "stream events",
        state.session_operation_timeout,
        state
            .runtime
            .stream_events(&thread_key, query.after_event_id.unwrap_or(0)),
    )
    .await?;
    let stream = events.map(|result| {
        let sse = match result {
            Ok(event) => SessionSseEvent::try_from(event)
                .map(Event::from)
                .unwrap_or_else(|error| stream_error_sse(error.to_string())),
            Err(error) => stream_error_sse(error.to_string()),
        };
        Ok(sse)
    });
    Ok(Sse::new(stream).keep_alive(KeepAlive::default()))
}

async fn session_operation<T>(
    action: &'static str,
    timeout_after: Duration,
    operation: impl Future<Output = Result<T, SessionRuntimeError>>,
) -> Result<T, ApiError> {
    match timeout(timeout_after, operation).await {
        Ok(result) => result.map_err(ApiError::from),
        Err(_) => Err(ApiError::Timeout {
            action,
            timeout_ms: timeout_after.as_millis(),
        }),
    }
}
