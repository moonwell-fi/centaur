use serde_json::Value;

#[derive(Debug)]
pub struct ExecuteSessionInput {
    pub metadata: Option<Value>,
    pub input_lines: Vec<String>,
    pub idle_timeout_ms: Option<u64>,
    pub max_duration_ms: Option<u64>,
}
