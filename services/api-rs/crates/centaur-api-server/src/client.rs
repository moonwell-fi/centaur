use std::{pin::Pin, time::Duration};

use centaur_session_core::{Session, ThreadKey};
use eventsource_stream::Eventsource;
use futures_util::{Stream, StreamExt};
use reqwest::{Client as HttpClient, StatusCode};
use thiserror::Error;

use crate::types::{
    AppendMessagesRequest, AppendMessagesResponse, CreateSessionRequest, ExecuteSessionRequest,
    ExecuteSessionResponse,
};

const DEFAULT_HTTP_TIMEOUT: Duration = Duration::from_secs(30);
const DEFAULT_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone, Debug)]
pub struct CentaurClient {
    client: HttpClient,
    base_url: String,
}

impl CentaurClient {
    pub fn new(base_url: impl Into<String>) -> Self {
        let client = HttpClient::builder()
            .connect_timeout(DEFAULT_CONNECT_TIMEOUT)
            .timeout(DEFAULT_HTTP_TIMEOUT)
            .build()
            .expect("failed to build Centaur HTTP client with request timeouts");
        Self::with_client(client, base_url)
    }

    pub fn with_client(client: HttpClient, base_url: impl Into<String>) -> Self {
        Self {
            client,
            base_url: base_url.into().trim_end_matches('/').to_owned(),
        }
    }

    pub async fn create_session(
        &self,
        thread_key: &ThreadKey,
        request: CreateSessionRequest,
    ) -> Result<Session, ClientError> {
        self.post_json(&self.session_url(thread_key), &request)
            .await
    }

    pub async fn append_messages(
        &self,
        thread_key: &ThreadKey,
        request: AppendMessagesRequest,
    ) -> Result<AppendMessagesResponse, ClientError> {
        self.post_json(
            &format!("{}/messages", self.session_url(thread_key)),
            &request,
        )
        .await
    }

    pub async fn execute_session(
        &self,
        thread_key: &ThreadKey,
        request: ExecuteSessionRequest,
    ) -> Result<ExecuteSessionResponse, ClientError> {
        self.post_json(
            &format!("{}/execute", self.session_url(thread_key)),
            &request,
        )
        .await
    }

    pub async fn stream_events(
        &self,
        thread_key: &ThreadKey,
        after_event_id: i64,
    ) -> Result<SseEventStream, ClientError> {
        let events_url = format!(
            "{}/events?after_event_id={after_event_id}",
            self.session_url(thread_key)
        );
        let response = self.client.get(&events_url).send().await?;
        let response = ensure_response_success(response).await?;
        let stream = response
            .bytes_stream()
            .eventsource()
            .map(|event| event.map_err(|error| ClientError::EventStream(error.to_string())));
        Ok(Box::pin(stream))
    }

    async fn post_json<T, R>(&self, url: &str, payload: &T) -> Result<R, ClientError>
    where
        T: serde::Serialize + ?Sized,
        R: serde::de::DeserializeOwned,
    {
        let response = self.client.post(url).json(payload).send().await?;
        let response = ensure_response_success(response).await?;
        Ok(response.json().await?)
    }

    fn session_url(&self, thread_key: &ThreadKey) -> String {
        format!(
            "{}/api/session/{}",
            self.base_url,
            urlencoding::encode(thread_key.as_str())
        )
    }
}

pub type SseEventStream = Pin<Box<dyn Stream<Item = Result<SseEvent, ClientError>> + Send>>;
pub type SseEvent = eventsource_stream::Event;

async fn ensure_response_success(
    response: reqwest::Response,
) -> Result<reqwest::Response, ClientError> {
    let status = response.status();
    if status.is_success() {
        return Ok(response);
    }
    let body = response.text().await?;
    Err(ClientError::Api { status, body })
}

#[derive(Debug, Error)]
pub enum ClientError {
    #[error(transparent)]
    Http(#[from] reqwest::Error),
    #[error("HTTP {status}: {body}")]
    Api { status: StatusCode, body: String },
    #[error("event stream parse failed: {0}")]
    EventStream(String),
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use centaur_session_core::{HarnessType, ThreadKey};
    use serde_json::json;
    use tokio::net::TcpListener;

    use super::{CentaurClient, ClientError};
    use crate::types::CreateSessionRequest;

    #[tokio::test]
    async fn post_requests_honor_reqwest_timeout() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let _server = tokio::spawn(async move {
            let Ok((_socket, _addr)) = listener.accept().await else {
                return;
            };
            std::future::pending::<()>().await;
        });

        let http = reqwest::Client::builder()
            .timeout(Duration::from_millis(25))
            .build()
            .unwrap();
        let client = CentaurClient::with_client(http, format!("http://{addr}"));
        let thread_key = ThreadKey::parse("cli:timeout-test").unwrap();

        let error = client
            .create_session(
                &thread_key,
                CreateSessionRequest {
                    harness_type: HarnessType::Codex,
                    metadata: Some(json!({"source": "test"})),
                },
            )
            .await
            .unwrap_err();

        assert!(matches!(error, ClientError::Http(error) if error.is_timeout()));
    }
}
