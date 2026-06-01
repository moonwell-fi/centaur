use std::{
    convert::{Infallible, TryFrom},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};

use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::StatusCode,
    response::{
        IntoResponse, Response, Sse,
        sse::{Event, KeepAlive},
    },
    routing::{get, post},
};
use centaur_session_core::{Session, ThreadKey};
use centaur_session_runtime::{ExecuteSessionInput, SandboxRuntime, SessionRuntime};
use centaur_session_sqlx::PgSessionStore;
use futures_util::{Stream, StreamExt};
use serde_json::{Value, json};

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
    lifecycle: ServerLifecycle,
}

#[derive(Clone, Debug)]
pub struct ServerLifecycle {
    ready: Arc<AtomicBool>,
}

impl ServerLifecycle {
    pub fn new_ready() -> Self {
        Self {
            ready: Arc::new(AtomicBool::new(true)),
        }
    }

    pub fn mark_draining(&self) {
        self.ready.store(false, Ordering::SeqCst);
    }

    pub fn is_ready(&self) -> bool {
        self.ready.load(Ordering::SeqCst)
    }
}

impl Default for ServerLifecycle {
    fn default() -> Self {
        Self::new_ready()
    }
}

pub fn build_router_with_runtime(store: PgSessionStore, sandbox_runtime: SandboxRuntime) -> Router {
    build_router_with_runtime_and_lifecycle(store, sandbox_runtime, ServerLifecycle::new_ready())
}

pub fn build_router_with_runtime_and_lifecycle(
    store: PgSessionStore,
    sandbox_runtime: SandboxRuntime,
    lifecycle: ServerLifecycle,
) -> Router {
    build_router_with_session_runtime_and_lifecycle(
        SessionRuntime::new(store, sandbox_runtime),
        lifecycle,
    )
}

pub fn build_router_with_session_runtime(runtime: SessionRuntime) -> Router {
    build_router_with_session_runtime_and_lifecycle(runtime, ServerLifecycle::new_ready())
}

pub fn build_router_with_session_runtime_and_lifecycle(
    runtime: SessionRuntime,
    lifecycle: ServerLifecycle,
) -> Router {
    Router::new()
        .route("/health", get(healthz))
        .route("/healthz", get(healthz))
        .route("/health/ready", get(readyz))
        .route("/readyz", get(readyz))
        .route("/api/session/{thread_key}", post(create_or_get_session))
        .route("/api/session/{thread_key}/messages", post(append_messages))
        .route("/api/session/{thread_key}/execute", post(execute_session))
        .route("/api/session/{thread_key}/events", get(stream_events))
        .with_state(AppState { runtime, lifecycle })
}

async fn healthz() -> Json<Value> {
    Json(json!({"ok": true}))
}

async fn readyz(State(state): State<AppState>) -> impl IntoResponse {
    readiness_response(&state.lifecycle)
}

fn readiness_response(lifecycle: &ServerLifecycle) -> Response {
    if lifecycle.is_ready() {
        return (StatusCode::OK, Json(json!({"ok": true}))).into_response();
    }
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"ok": false, "status": "draining"})),
    )
        .into_response()
}

async fn create_or_get_session(
    State(state): State<AppState>,
    Path(raw_thread_key): Path<String>,
    Json(request): Json<CreateSessionRequest>,
) -> Result<Json<Session>, ApiError> {
    let thread_key = ThreadKey::try_from(raw_thread_key)?;
    let session = state
        .runtime
        .create_or_get_session(&thread_key, &request.harness_type, request.metadata)
        .await?;
    Ok(Json(session))
}

async fn append_messages(
    State(state): State<AppState>,
    Path(raw_thread_key): Path<String>,
    Json(request): Json<AppendMessagesRequest>,
) -> Result<Json<AppendMessagesResponse>, ApiError> {
    let thread_key = ThreadKey::try_from(raw_thread_key)?;
    let message_ids = state
        .runtime
        .append_messages(&thread_key, &request.messages)
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
    let execution = state
        .runtime
        .execute_session(
            &thread_key,
            ExecuteSessionInput {
                metadata: request.metadata,
                input_lines: request.input_lines,
                idle_timeout_ms: request.idle_timeout_ms,
                max_duration_ms: request.max_duration_ms,
            },
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
    let events = state
        .runtime
        .stream_events(&thread_key, query.after_event_id.unwrap_or(0))
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

#[cfg(test)]
mod tests {
    use super::{ServerLifecycle, readiness_response};
    use axum::http::StatusCode;

    #[test]
    fn readiness_response_reflects_draining_state() {
        let lifecycle = ServerLifecycle::new_ready();
        assert_eq!(readiness_response(&lifecycle).status(), StatusCode::OK);

        lifecycle.mark_draining();

        assert_eq!(
            readiness_response(&lifecycle).status(),
            StatusCode::SERVICE_UNAVAILABLE
        );
    }
}
